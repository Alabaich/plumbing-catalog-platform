"""
Brand C — Step 2: Scrape each product page from the parsing_queue.

DIFF ENGINE INTEGRATED:
- Before writing product_db: compute hash, compare with stored hash
- Before writing variant_db: compute hash per SKU, compare with stored hash
- Result: second run with no changes = 0 DB writes

Variant system: colour finish links via a[class="plmr-c-finishes-nav__finish-button"].
Each finish is a separate page — fetched individually to get per-finish SKU and image.
Products without finish links are stored as a single SKU with no Option1 Value.
"""

import sys
import os
import time
import re
import json
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
BRAND_NAME = os.getenv("BRAND_C_NAME", "BRAND_C")
logger = get_logger(BRAND_NAME, "scrape")

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("POSTGRES_DB", "plumbing_products")
DB_USER = os.getenv("POSTGRES_USER", "plumbing_app")
DB_PASS = os.getenv("POSTGRES_PASSWORD", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")

BASE_URL = os.getenv("BRAND_C_BASE_URL", "").rstrip("/")

# CSS selectors
NAME      = "h1[id='productTitle']"
SKU       = "p[class='c-product-intro__model']"
DESC_1    = "p[class='c-product-summary__content']"
DESC_2    = "div[class='plmr-c-featured-product-specs plmr-c-featured-product-specs--condensed']"
DESC_3    = "div[class='plmr-c-additional-product-specs__content plmr-c-general-content']"
IMAGE     = "div[class='owl-stage-outer']"
VAR_IMAGE = "div[class='product-images-container']"
CERT_BLOCK = "a[aria-label='Download PDF File']"


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
    options.add_argument(
        '--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
    )
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
        WebDriverWait(driver, 35).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, SKU))
        )
        return driver.page_source
    except TimeoutException:
        logger.warning(f"Timeout on {url}")
        return None
    except WebDriverException as e:
        logger.warning(f"WebDriver error on {url}: {e}")
        return None
    except Exception as e:
        logger.warning(f"Unexpected error on {url}: {e}")
        return None


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def parse_product(prod_soup, driver):
    name_el = prod_soup.select_one(NAME)
    if not name_el:
        return None
    name = name_el.text.strip()

    sku_el = prod_soup.select_one(SKU)
    if not sku_el:
        return None
    sku_code = sku_el.text.strip().replace("Model: ", "")

    # Images — scroll gallery into view to trigger lazy loading
    image_url = []
    gallery_el = prod_soup.select_one(IMAGE)
    if gallery_el:
        try:
            gallery = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, IMAGE))
            )
            driver.execute_script(
                "arguments[0].scrollIntoView({behavior: 'auto', block: 'center'});", gallery
            )
            time.sleep(3)
            img_wrap = driver.find_element(By.CSS_SELECTOR, IMAGE)
            for img in img_wrap.find_elements(By.TAG_NAME, "img"):
                src = img.get_attribute('src')
                if src and 'blank' not in src:
                    image_url.append(src)
        except Exception:
            pass

    # Description
    desc_parts = []
    for sel in (DESC_1, DESC_2, DESC_3):
        el = prod_soup.select_one(sel)
        if el:
            desc_parts.append(el.prettify())
    desc = "\n".join(desc_parts) if desc_parts else ""

    # Certifications
    cert_list = []
    for a in prod_soup.select(CERT_BLOCK):
        link_url = a.get("href")
        if not link_url:
            continue
        if "specifications" in link_url:
            link_text = "Spec Sheet"
        elif "instruction-sheets" in link_url:
            link_text = "Installation Manual"
        else:
            link_text = "Technical Drawing"
        cert_list.append({"text": link_text, "url": link_url})

    handle = f"{BRAND_NAME.lower()}_{slugify(name)}_{sku_code.lower()}"

    return {
        "name": name,
        "SKU": sku_code,
        "image_url": image_url,
        "desc": desc,
        "cert": cert_list,                                              # list — for hashing
        "certifications": json.dumps(cert_list) if cert_list else "",  # JSON — for DB
        "opt_1": "",
        "opt_1_val": "",
        "handle": handle,
        "vendor": BRAND_NAME,
    }


def parse_variant(driver, soup):
    """Fetch each finish page and collect per-finish SKU + image."""
    variants_data = []
    colors = soup.select('a[class="plmr-c-finishes-nav__finish-button"]')

    for a in colors:
        get_url = a.get("href")
        if not get_url:
            continue
        url = f"{BASE_URL}{get_url}"
        page = fetch_page(url, driver)
        if not page:
            continue
        var_soup = bs(page, "lxml")

        sku_el = var_soup.select_one(SKU)
        if not sku_el:
            continue
        var_sku_code = sku_el.text.strip().replace("Model: ", "")
        var_name = (a.get("title") or "").strip()

        var_img_list = []
        img_block = var_soup.select_one(VAR_IMAGE)
        if img_block:
            img = img_block.select_one("img")
            if img and img.get("src"):
                var_img_list.append(img.get("src"))

        variants_data.append({
            "name": "",
            "SKU": var_sku_code,
            "image_url": var_img_list,
            "desc": "",
            "cert": [],
            "certifications": "",
            "opt_1": "",
            "opt_1_val": var_name,
            "handle": "",
            "vendor": BRAND_NAME,
        })

    return variants_data


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
                "Option1 Name", all_image_srcs, "Status", useful_links, data_hash
            ) VALUES (%s, %s, %s, %s, %s, %s, 'active', %s, %s)
            ON CONFLICT (handle) DO UPDATE SET
                "Title" = EXCLUDED."Title",
                "Body (HTML)" = EXCLUDED."Body (HTML)",
                "Vendor" = EXCLUDED."Vendor",
                "Option1 Name" = EXCLUDED."Option1 Name",
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
            base_data.get('opt_1'),
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
                handle, "Variant SKU", "Option1 Value", variant_image_src, data_hash
            ) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT ("Variant SKU") DO UPDATE SET
                handle = EXCLUDED.handle,
                "Option1 Value" = EXCLUDED."Option1 Value",
                variant_image_src = EXCLUDED.variant_image_src,
                data_hash = EXCLUDED.data_hash,
                "updated_at" = CURRENT_TIMESTAMP;
        """, (handle, sku, vdata.get('opt_1_val'), variant_image, new_variant_hash))

        if is_new:
            stats["variants_new"] += 1
            logger.info(f"  NEW variant: {sku} ({vdata.get('opt_1_val')})")
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
                    notify_telegram(f"[{BRAND_NAME}] No pending items to scrape.")
                    return

                consecutive_fails = 0

                for pid, link, cat_url in tqdm(rows, desc=f"Scraping {BRAND_NAME}"):
                    try:
                        prod_html = fetch_page(link, driver)
                        if not prod_html:
                            raise ValueError("Page returned no HTML")

                        prod_soup = bs(prod_html, "lxml")
                        product = parse_product(prod_soup, driver)
                        if not product:
                            raise ValueError("Could not parse product data")

                        colors = prod_soup.select('a[class="plmr-c-finishes-nav__finish-button"]')
                        if colors:
                            product['SKU'] = ''
                            product['opt_1'] = 'Color'
                            variants = parse_variant(driver, prod_soup)
                            if not variants:
                                raise ValueError("No variant data found")
                            product_to_save = [product] + variants
                        else:
                            product_to_save = [product]

                        stats = save_with_diff(cur, product_to_save)
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
                        if consecutive_fails == 5:
                            notify_telegram(
                                f"🔴 <b>{BRAND_NAME}</b>: 5 failures in a row\n"
                                f"Last: {link}"
                            )


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
