"""
Brand B — Step 2: Scrape each product page from the parsing_queue.

DIFF ENGINE INTEGRATED:
- Before writing product_db: compute hash, compare with stored hash
- Before writing variant_db: compute hash per SKU, compare with stored hash
- Result: second run with no changes = 0 DB writes

Variant discovery: products can have up to 3 option axes:
Color, Base Installation, Drain Position, Seat Position, Seat Type,
Seat or Footrest. All available combinations are enumerated via
itertools.product and clicked through.
"""

import sys
import os
import time
import re
import json
import itertools
import subprocess
import psycopg2
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException, TimeoutException
from bs4 import BeautifulSoup as bs
from tqdm import tqdm

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from utils.logger import get_logger, run_with_crash_alert, notify_telegram
from diff_engine import compute_product_hash, compute_variant_hash, should_update

# --- CONFIGURATION ---
BRAND_NAME = os.getenv("BRAND_B_NAME", "BRAND_B")
logger = get_logger(BRAND_NAME, "scrape")

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("POSTGRES_DB", "plumbing_products")
DB_USER = os.getenv("POSTGRES_USER", "plumbing_app")
DB_PASS = os.getenv("POSTGRES_PASSWORD", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")

# CSS selectors
NAME_SEL     = "h1[class='name']"
SKU_SEL      = ".code.variant-modelnumber"
DESC_SEL     = "section[class='section information secondary-bg']"
IMAGE_SEL    = "div.slick-current"
CERT_SEL     = ".section.specs.primary-bg"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_db_connection():
    if DATABASE_URL:
        return psycopg2.connect(DATABASE_URL)
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT,
        database=DB_NAME, user=DB_USER, password=DB_PASS,
    )


def slugify(text):
    if not text:
        return ""
    text = text.lower().strip()
    text = re.sub(r'[\s/]+', '-', text)
    text = re.sub(r'[^\w\-]', '', text)
    return text


def force_kill_chrome():
    try:
        subprocess.run(['pkill', '-f', 'chrome'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['pkill', '-f', 'chromedriver'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info("Killed lingering Chrome processes.")
    except Exception as e:
        logger.warning(f"Could not kill Chrome: {e}")


def setup_driver():
    options = webdriver.ChromeOptions()
    options.add_argument('--headless')
    options.add_argument('--disable-gpu')
    options.add_argument('--no-sandbox')
    options.add_argument('--window-size=1920,1080')
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_argument(
        '--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    )
    prefs = {
        "profile.managed_default_content_settings.popups": 2,
        "profile.managed_default_content_settings.geolocation": 2,
        "profile.managed_default_content_settings.media_stream": 2,
    }
    options.add_experimental_option("prefs", prefs)
    try:
        return webdriver.Chrome(options=options)
    except WebDriverException as e:
        logger.critical(f"DRIVER SETUP FAILED: {e}")
        raise


def update_queue_status(cur, item_id, status):
    cur.execute("UPDATE parsing_queue SET status = %s WHERE id = %s", (status, item_id))


# ---------------------------------------------------------------------------
# DB helpers for diff engine
# ---------------------------------------------------------------------------
def get_stored_product_hash(cur, handle):
    cur.execute("SELECT data_hash FROM product_db WHERE handle = %s", (handle,))
    row = cur.fetchone()
    if row and row[0]:
        return row[0].strip()
    return None


def get_stored_variant_hash(cur, sku):
    cur.execute('SELECT data_hash FROM variant_db WHERE "Variant SKU" = %s', (sku,))
    row = cur.fetchone()
    if row and row[0]:
        return row[0].strip()
    return None


def get_existing_variant_skus(cur, handle):
    cur.execute('SELECT "Variant SKU" FROM variant_db WHERE handle = %s', (handle,))
    return {row[0] for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# Page fetcher
# ---------------------------------------------------------------------------
def fetch_page(url, driver):
    try:
        driver.get(url)
        cookie_sel = "button[id='onetrust-accept-btn-handler']"
        try:
            WebDriverWait(driver, 2).until(EC.presence_of_element_located((By.CSS_SELECTOR, cookie_sel)))
            btn = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.CSS_SELECTOR, cookie_sel)))
            btn.click()
            WebDriverWait(driver, 10).until(EC.invisibility_of_element_located((By.CSS_SELECTOR, cookie_sel)))
        except TimeoutException:
            pass
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'footer.site-footer'))
        )
        return driver.page_source
    except Exception as e:
        logger.warning(f"Error fetching {url}: {e}")
        return None


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def parse_product(prod_soup, page_url):
    if not prod_soup.select(NAME_SEL):
        return None

    name = prod_soup.select_one(NAME_SEL).text.strip()
    sku_el = prod_soup.select_one(SKU_SEL)
    sku_code = sku_el.text.strip() if sku_el else "N/A"
    sku_code = re.sub(r'^\s*Model number\s*:\s*', '', sku_code)

    # Images
    img_list = []
    image_el = prod_soup.select_one(IMAGE_SEL)
    if image_el:
        img = image_el.select_one("img")
        if img and img.get('src'):
            img_list.append(img.get('src'))
    slider = prod_soup.select_one(".slider-wrapper")
    if slider:
        for img in slider.select("img"):
            src = img.get('src')
            if src:
                img_list.append(src)

    # Description
    desc_parts = prod_soup.select(DESC_SEL)
    desc = "\n".join(tag.text for tag in desc_parts) if desc_parts else ""

    # Certifications
    cert_list = []
    cert_block = prod_soup.select_one(CERT_SEL)
    if cert_block:
        for el in cert_block.select('div.col.col-12'):
            h3 = el.select_one('h3')
            if h3 and ('Technical Drawings' in h3.text or 'Installation Guides' in h3.text):
                for a in el.select("a"):
                    link_url = a.get('href')
                    link_text = a.text.strip()
                    if link_url and link_text:
                        cert_list.append({"text": link_text, "url": link_url})

    handle = f"{BRAND_NAME.lower()}_{slugify(name)}_{sku_code.lower()}"

    return {
        "handle": handle,
        "name": name,
        "SKU": sku_code,
        "image_url": img_list,
        "desc": desc,
        "cert": cert_list,                                          # list — for hashing
        "certifications": json.dumps(cert_list) if cert_list else "",   # JSON — for DB
        "vendor": BRAND_NAME,
        "opt_1": "", "opt_1_val": "",
        "opt_2": "", "opt_2_val": "",
        "opt_3": "", "opt_3_val": "",
    }


def parse_variant(url, driver):
    """Enumerate all option combinations and collect per-variant data."""
    variant_list = []
    prod_changes = []

    driver.get(url)
    try:
        WebDriverWait(driver, 3).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.overview"))
        )
    except TimeoutException:
        return None, None

    option_inputs = {
        'Color': [], 'Base Installation': [], 'Drain Position': [],
        'Seat Position': [], 'Seat Type': [], 'Seat or Footrest': [],
    }

    var_block = driver.find_element(By.CSS_SELECTOR, 'div[class="overview"]')

    color_block = var_block.find_elements(By.CSS_SELECTOR, "div.group[data-variant-property='ColorCode']")
    if color_block:
        color_inputs = color_block[0].find_elements(By.CSS_SELECTOR, "button.swatch.bigger")
        if color_inputs:
            option_inputs['Color'] = color_inputs

    for option_el in var_block.find_elements(By.CSS_SELECTOR, "div[data-variant-property='Position']"):
        legends = option_el.find_elements(By.CSS_SELECTOR, "label[class='label']")
        if legends:
            text = legends[0].text
            btns = option_el.find_elements(By.CSS_SELECTOR, "button[type='button']")
            if "Base Installation: " in text:
                option_inputs['Base Installation'] = btns
            elif "Drain Position: " in text:
                option_inputs['Drain Position'] = btns
            elif "Seat Position: " in text:
                option_inputs['Seat Position'] = btns
            elif "Seat Type: " in text:
                option_inputs['Seat Type'] = btns
            elif "Seat or Footrest: " in text:
                option_inputs['Seat or Footrest'] = btns

    available_options = {k: v for k, v in option_inputs.items() if v}
    if not available_options:
        logger.info("No variant options found.")
        return None, None

    option_types = list(available_options.keys())
    option_indices = [range(len(available_options[k])) for k in option_types]
    combinations = list(itertools.product(*option_indices))
    logger.info(f"Found {len(combinations)} combinations across {len(option_types)} option type(s).")

    for combo in combinations:
        variant_data = {
            "handle": "", "name": "", "SKU": "", "image_url": [],
            "desc": "", "cert": [], "certifications": "", "vendor": BRAND_NAME,
            "opt_1": "", "opt_1_val": "", "opt_2": "", "opt_2_val": "",
            "opt_3": "", "opt_3_val": "",
        }
        extracted_options = {}

        for i, option_type in enumerate(option_types):
            idx = combo[i]
            el = option_inputs[option_type][idx]
            opt_val = el.get_attribute('title') if option_type == 'Color' else el.text.strip()
            driver.execute_script("arguments[0].click();", el)
            time.sleep(2.5)
            extracted_options[f'opt_{i+1}'] = option_type
            extracted_options[f'opt_{i+1}_val'] = opt_val

        var_soup = bs(driver.page_source, 'html.parser')

        sku_el = var_soup.select_one(SKU_SEL)
        var_sku = sku_el.text.strip() if sku_el else ''
        var_sku = re.sub(r'^\s*Model number\s*:\s*', '', var_sku)
        variant_data['SKU'] = var_sku

        image_el = var_soup.select_one(IMAGE_SEL)
        if image_el:
            img = image_el.select_one("img")
            if img and img.get('src'):
                variant_data['image_url'] = [img.get('src')]

        for i in range(1, 4):
            variant_data[f'opt_{i}'] = extracted_options.get(f'opt_{i}', '')
            variant_data[f'opt_{i}_val'] = extracted_options.get(f'opt_{i}_val', '')

        prod_changes.append({
            'opt_1': variant_data['opt_1'],
            'opt_2': variant_data['opt_2'],
            'opt_3': variant_data['opt_3'],
        })
        variant_list.append(variant_data)

    return variant_list, prod_changes


# ---------------------------------------------------------------------------
# Save with diff engine
# ---------------------------------------------------------------------------
def save_with_diff(cur, variants_list):
    stats = {
        "product_skipped": 0,
        "product_updated": 0,
        "variants_skipped": 0,
        "variants_updated": 0,
        "variants_new": 0,
    }

    base_data = variants_list[0]
    handle = base_data['handle']
    useful_links = base_data.get('certifications') or None

    all_imgs = list({img for v in variants_list for img in v.get('image_url', [])})

    # --- Compute product hash ---
    scraped_skus_list = sorted([v.get('SKU') for v in variants_list if v.get('SKU')])
    product_for_hash = {
        "name": base_data.get('name'),
        "desc": base_data.get('desc'),
        "cert": sorted([c.get('url', '') for c in (base_data.get('cert') or [])]),
        "opt_1": base_data.get('opt_1'),
        "opt_2": base_data.get('opt_2'),
        "opt_3": base_data.get('opt_3'),
        "image_url": all_imgs,
        "variant_skus": scraped_skus_list,
    }
    new_product_hash = compute_product_hash(product_for_hash)
    stored_product_hash = get_stored_product_hash(cur, handle)

    existing_skus = get_existing_variant_skus(cur, handle)
    scraped_skus = {v.get('SKU') for v in variants_list if v.get('SKU')}
    new_skus = scraped_skus - existing_skus
    has_new_variants = len(new_skus) > 0

    product_changed = should_update(new_product_hash, stored_product_hash)

    if product_changed or has_new_variants:
        product_sql = """
            INSERT INTO product_db (
                handle, "Title", "Body (HTML)", "Vendor",
                "Option1 Name", "Option2 Name", "Option3 Name",
                all_image_srcs, "Status", useful_links, data_hash
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, %s)
            ON CONFLICT (handle) DO UPDATE SET
                "Title" = EXCLUDED."Title",
                "Body (HTML)" = EXCLUDED."Body (HTML)",
                "Vendor" = EXCLUDED."Vendor",
                "Option1 Name" = EXCLUDED."Option1 Name",
                "Option2 Name" = EXCLUDED."Option2 Name",
                "Option3 Name" = EXCLUDED."Option3 Name",
                "all_image_srcs" = EXCLUDED."all_image_srcs",
                "useful_links" = EXCLUDED."useful_links",
                "Status" = 'active',
                data_hash = EXCLUDED.data_hash,
                "updated_at" = CURRENT_TIMESTAMP;
        """
        cur.execute(product_sql, (
            handle,
            base_data.get('name'),
            base_data.get('desc'),
            base_data.get('vendor'),
            base_data.get('opt_1') or None,
            base_data.get('opt_2') or None,
            base_data.get('opt_3') or None,
            all_imgs,
            useful_links,
            new_product_hash,
        ))
        stats["product_updated"] = 1

        if product_changed and has_new_variants:
            logger.info(f"Product {handle}: data changed + {len(new_skus)} new variant(s)")
        elif product_changed:
            logger.info(f"Product {handle}: data changed")
        else:
            logger.info(f"Product {handle}: {len(new_skus)} new variant(s) → updating images")
    else:
        stats["product_skipped"] = 1
        logger.info(f"Product {handle}: no changes, skipping")

    # --- Process each variant ---
    for vdata in variants_list:
        sku = vdata.get('SKU')
        if not sku:
            logger.warning(f"No SKU for variant of {handle}, skipping.")
            continue

        imgs = vdata.get('image_url', [])
        variant_image = imgs[0] if imgs else None

        variant_for_hash = {
            "opt_1_val": vdata.get('opt_1_val'),
            "opt_2_val": vdata.get('opt_2_val'),
            "opt_3_val": vdata.get('opt_3_val'),
            "image_url": [variant_image] if variant_image else [],
        }
        new_variant_hash = compute_variant_hash(variant_for_hash)
        stored_variant_hash = get_stored_variant_hash(cur, sku)

        if not should_update(new_variant_hash, stored_variant_hash):
            stats["variants_skipped"] += 1
            continue

        is_new = stored_variant_hash is None
        cur.execute("""
            INSERT INTO variant_db (
                handle, "Variant SKU", "Option1 Value", "Option2 Value", "Option3 Value",
                variant_image_src, data_hash
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT ("Variant SKU") DO UPDATE SET
                handle = EXCLUDED.handle,
                "Option1 Value" = EXCLUDED."Option1 Value",
                "Option2 Value" = EXCLUDED."Option2 Value",
                "Option3 Value" = EXCLUDED."Option3 Value",
                variant_image_src = EXCLUDED.variant_image_src,
                data_hash = EXCLUDED.data_hash,
                "updated_at" = CURRENT_TIMESTAMP;
        """, (
            handle, sku,
            vdata.get('opt_1_val'), vdata.get('opt_2_val'), vdata.get('opt_3_val'),
            variant_image, new_variant_hash
        ))

        if is_new:
            stats["variants_new"] += 1
            logger.info(f"  NEW variant: {sku} ({vdata.get('opt_1_val')}, {vdata.get('opt_2_val')}, {vdata.get('opt_3_val')})")
        else:
            stats["variants_updated"] += 1
            logger.info(f"  Updated variant: {sku}")

    return stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    driver = None
    force_kill_chrome()

    totals = {
        "products_processed": 0,
        "products_skipped": 0,
        "products_updated": 0,
        "variants_skipped": 0,
        "variants_updated": 0,
        "variants_new": 0,
        "failed": 0,
    }

    try:
        driver = setup_driver()
        conn = get_db_connection()
        conn.autocommit = False

        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, link, category FROM parsing_queue WHERE vendor = %s AND status = 'pending'",
                    (BRAND_NAME,)
                )
                rows = cur.fetchall()
                logger.info(f"Found {len(rows)} pending items for {BRAND_NAME}")

                if not rows:
                    logger.info("No items to scrape. Exiting.")
                    return

                consecutive_fails = 0
                items_since_restart = 0

                for pid, link, cat_url in tqdm(rows, desc=f"Scraping {BRAND_NAME}"):
                    if items_since_restart >= 1000:
                        logger.info(f"Restarting Chrome driver after {items_since_restart} items (memory management)...")
                        try:
                            driver.quit()
                        except Exception:
                            pass
                        force_kill_chrome()
                        driver = setup_driver()
                        items_since_restart = 0
                    try:
                        prod_html = fetch_page(link, driver)
                        if not prod_html:
                            raise ValueError("Page returned no HTML")

                        prod_soup = bs(prod_html, "lxml")
                        product = parse_product(prod_soup, link)
                        if not product:
                            raise ValueError("Could not parse product data")

                        variants, prod_changes = parse_variant(link, driver)
                        if variants:
                            last_opts = prod_changes[-1]
                            product["opt_1"] = last_opts.get("opt_1", "")
                            product["opt_2"] = last_opts.get("opt_2", "")
                            product["opt_3"] = last_opts.get("opt_3", "")
                            product["SKU"] = ""
                            for v in variants:
                                v["handle"] = product["handle"]
                            variants_list = [product] + variants
                        else:
                            variants_list = [product]

                        stats = save_with_diff(cur, variants_list)
                        update_queue_status(cur, pid, 'processed')
                        conn.commit()

                        consecutive_fails = 0
                        totals["products_processed"] += 1
                        totals["products_skipped"] += stats["product_skipped"]
                        totals["products_updated"] += stats["product_updated"]
                        totals["variants_skipped"] += stats["variants_skipped"]
                        totals["variants_updated"] += stats["variants_updated"]
                        totals["variants_new"] += stats["variants_new"]

                    except Exception as e:
                        logger.error(f"FAILURE on item {pid} ({link}): {e}", exc_info=True)
                        conn.rollback()
                        update_queue_status(cur, pid, 'failed')
                        conn.commit()
                        totals["failed"] += 1
                        consecutive_fails += 1
                        items_since_restart += 1
                        if consecutive_fails == 5:
                            notify_telegram(
                                f"🔴 <b>{BRAND_NAME}</b>: 5 failures in a row\n"
                                f"Last: {link}"
                            )
                            logger.warning("Restarting Chrome driver after 5 consecutive failures...")
                            try:
                                driver.quit()
                            except Exception:
                                pass
                            force_kill_chrome()
                            subprocess.run(['sh', '-c', 'rm -rf /tmp/.org.chromium* /tmp/chrome_*'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            driver = setup_driver()
                            consecutive_fails = 0


        logger.info("=" * 50)
        logger.info("SCRAPE SUMMARY:")
        logger.info(f"  Products processed: {totals['products_processed']}")
        logger.info(f"  Products updated:   {totals['products_updated']}")
        logger.info(f"  Products skipped:   {totals['products_skipped']} (no changes)")
        logger.info(f"  Variants new:       {totals['variants_new']}")
        logger.info(f"  Variants updated:   {totals['variants_updated']}")
        logger.info(f"  Variants skipped:   {totals['variants_skipped']} (no changes)")
        logger.info(f"  Failed:             {totals['failed']}")
        logger.info("=" * 50)
        notify_telegram(
            f"[{BRAND_NAME}] Scrape done\n"
            f"Processed: {totals['products_processed']} | "
            f"Updated: {totals['products_updated']} | "
            f"Skipped: {totals['products_skipped']} | "
            f"Failed: {totals['failed']}"
        )

    except psycopg2.Error as e:
        logger.critical(f"DATABASE ERROR: {e}", exc_info=True)
    except Exception as e:
        logger.critical(f"Script failed: {e}", exc_info=True)
    finally:
        if driver:
            driver.quit()
        force_kill_chrome()
        logger.info("--- SCRAPING JOB FINISHED ---")


if __name__ == "__main__":
    run_with_crash_alert(BRAND_NAME, "scrape", main)
