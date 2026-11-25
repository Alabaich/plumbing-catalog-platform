"""
Brand A — Step 2: Scrape each product page from the parsing_queue.

DIFF ENGINE INTEGRATED:
- Before writing product_db: compute hash, compare with stored hash
- Before writing variant_db: compute hash per SKU, compare with stored hash
- Result: second run with no changes = 0 DB writes

Variant discovery: products with a finish selector (div#finish-type-list)
have colour/finish variants accessed by clicking each swatch.
Cert PDFs are obtained by clicking accordion sections and capturing the
new-tab URL.
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
from selenium.common.exceptions import WebDriverException, TimeoutException, NoSuchElementException
from bs4 import BeautifulSoup as bs
from tqdm import tqdm

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from utils.logger import get_logger, run_with_crash_alert, notify_telegram
from diff_engine import compute_product_hash, compute_variant_hash, should_update

# --- CONFIGURATION ---
BRAND_NAME = os.getenv("BRAND_A_NAME", "BRAND_A")
logger = get_logger(BRAND_NAME, "scrape")

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("POSTGRES_DB", "plumbing_products")
DB_USER = os.getenv("POSTGRES_USER", "plumbing_app")
DB_PASS = os.getenv("POSTGRES_PASSWORD", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")

# CSS selectors
NAME_SEL  = "pproduct-detail-page__description"   # intentionally kept as original
NAME_ALT  = "span.product-detail-page__title"
SKU_SEL   = "span.product-detail-page__sku-id"
DESC_SEL  = "p.product-detail-page__narrative-description--show-more"
DESC_NONE = "p.product-detail-page__narrative-description--none"
INFO_SEL  = "a.pdp-features-technologies__view-more.gbh-data-layer"
CERT_SEL  = "div[class='pdp-technical-tile__files --specs-info']"
MAN_SEL   = "div[class='pdp-support-files__install']"


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
        driver.execute_script("document.body.style.zoom='50%'")
        WebDriverWait(driver, 35).until(
            EC.presence_of_element_located((By.ID, "global-footer"))
        )
        if driver.find_elements(By.CSS_SELECTOR, "div[aria-label='Cookie Preferences']"):
            try:
                btn = driver.find_element(By.CSS_SELECTOR, "button[id='onetrust-accept-btn-handler']")
                driver.execute_script("arguments[0].click();", btn)
                logger.info("Dismissed cookie banner.")
            except Exception:
                pass
        time.sleep(5)
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


def get_pdf_url_from_new_tab(driver, element):
    """Click element, capture new-tab PDF URL, close tab."""
    original_handle = driver.current_window_handle
    pdf_url = None
    try:
        element.click()
        WebDriverWait(driver, 10).until(EC.number_of_windows_to_be(2))
        for handle in driver.window_handles:
            if handle != original_handle:
                driver.switch_to.window(handle)
                WebDriverWait(driver, 15).until(EC.url_contains(".pdf"))
                pdf_url = driver.current_url
                driver.close()
                break
    except Exception:
        pass
    finally:
        driver.switch_to.window(original_handle)
    return pdf_url


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def parse_product(prod_soup, driver):
    name_el = prod_soup.select_one(NAME_SEL) or prod_soup.select_one(NAME_ALT)
    if not name_el:
        return None
    name = name_el.text.strip()

    sku_el = prod_soup.select_one(SKU_SEL)
    if not sku_el:
        return None
    sku_code = sku_el.text.strip()

    # Images
    image_url = []
    img_bl = prod_soup.select_one('.pdp-image-reel__image-stack-view')
    if img_bl:
        img = img_bl.select_one('img')
        if img and img.get('src'):
            image_url.append(img.get('src'))
    thumb_wrap = prod_soup.select_one('.pdp-thumbnail-vertical')
    if thumb_wrap:
        for img in thumb_wrap.select('img'):
            src = img.get('src')
            if src:
                image_url.append(src)

    # Description
    desc_parts = []
    if driver.find_elements(By.CSS_SELECTOR, DESC_NONE):
        try:
            el = driver.find_element(By.CSS_SELECTOR, DESC_NONE)
            desc_parts.append(el.get_attribute("textContent"))
        except Exception:
            pass

    if driver.find_elements(By.CSS_SELECTOR, DESC_SEL):
        try:
            desc_bl = driver.find_element(By.CSS_SELECTOR, DESC_SEL)
            try:
                btn = desc_bl.find_element(By.CSS_SELECTOR, 'a')
                driver.execute_script("window.scrollBy(0, 100);")
                time.sleep(1)
                btn.click()
                time.sleep(1)
                try:
                    el = WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, DESC_NONE))
                    )
                    desc_parts.append(el.get_attribute("textContent"))
                except TimeoutException:
                    pass
            except NoSuchElementException:
                pass
        except Exception:
            pass

    if prod_soup.select(INFO_SEL):
        try:
            info_bl = driver.find_element(By.CSS_SELECTOR, ".pdp-support-files__container.product-specifications")
            info_bl.find_element(By.CSS_SELECTOR, 'div.Collapsible').click()
            time.sleep(1)
            info_but = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, INFO_SEL))
            )
            driver.execute_script("window.scrollBy(0, 100);")
            time.sleep(1)
            info_but.click()
            time.sleep(1)
            feat_list = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'ul.pdp-features-technologies__feature-list'))
            )
            desc_parts.append(feat_list.get_attribute("textContent"))
        except Exception:
            pass

    desc = '\n'.join(desc_parts) if desc_parts else ""

    # Certifications
    cert_list = []
    max_retries = 3

    # Spec sheets
    retry = 0
    while retry < max_retries:
        try:
            if not driver.find_elements(By.CSS_SELECTOR, CERT_SEL):
                break
            driver.refresh()
            driver.execute_script("document.body.style.zoom='50%'")
            time.sleep(1)
            spec_el = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//span[text()='Specifications']"))
            )
            spec_el.click()
            time.sleep(1)
            for tile in driver.find_elements(By.CSS_SELECTOR, "div.pdp-technical-tile__file"):
                pdf_url = get_pdf_url_from_new_tab(driver, tile)
                title = tile.find_element(By.TAG_NAME, "span").text.strip()
                if pdf_url:
                    cert_list.append({"text": title or "Specifications Sheet", "url": pdf_url})
            break
        except Exception:
            retry += 1
            time.sleep(2)

    # Manuals
    retry = 0
    while retry < max_retries:
        try:
            if not driver.find_elements(By.CSS_SELECTOR, MAN_SEL):
                break
            man_btn = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//span[text()='Installation & Service Parts']"))
            )
            man_btn.click()
            time.sleep(1)
            for tile in driver.find_elements(By.CSS_SELECTOR, "div.pdp-technical-tile__file"):
                pdf_url = get_pdf_url_from_new_tab(driver, tile)
                title = tile.get_attribute("textContent").strip()
                if pdf_url:
                    cert_list.append({"text": title or "Manual", "url": pdf_url})
            break
        except Exception:
            retry += 1
            time.sleep(2)

    handle = f"{BRAND_NAME.lower()}_{slugify(name)}_{sku_code.lower()}"

    return {
        "name": name,
        "SKU": sku_code,
        "image_url": image_url,
        "desc": desc,
        "cert": cert_list,                                          # list — for hashing
        "certifications": json.dumps(cert_list) if cert_list else "",   # JSON — for DB
        "opt_1": "",
        "opt_1_val": "",
        "handle": handle,
        "vendor": BRAND_NAME,
    }


def parse_variant(driver):
    """Click through colour/finish swatches and collect per-variant SKU + image."""
    variants_data = []

    var_bl = driver.find_element(By.CSS_SELECTOR, 'div[id="finish-type-list"]')
    variants = var_bl.find_elements(By.CSS_SELECTOR, 'div.color-swatch')
    logger.info(f"Found {len(variants)} finish variants.")

    for i in range(len(variants)):
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(1)
        retry_count = 0
        while retry_count < 3:
            try:
                variants = var_bl.find_elements(By.CSS_SELECTOR, 'div.color-swatch')
                variants[i].click()
                break
            except (TimeoutException, NoSuchElementException) as e:
                logger.warning(f"Swatch click attempt {retry_count + 1} failed: {e}")
                retry_count += 1
                time.sleep(2)

        time.sleep(1)
        var_soup = bs(driver.page_source, "lxml")

        var_sku_el = var_soup.select_one(SKU_SEL)
        var_sku = var_sku_el.text.strip() if var_sku_el else "N/A"

        var_img = None
        img_bl = var_soup.select_one('.pdp-image-reel__image-stack-view')
        if img_bl:
            img = img_bl.select_one('img')
            if img and img.get('src'):
                var_img = img.get('src')

        var_name_el = var_soup.select_one('.product-detail-page__finish_value')
        var_name = var_name_el.text.strip() if var_name_el else ""

        variants_data.append({
            "name": "",
            "SKU": var_sku,
            "image_url": [var_img] if var_img else [],
            "desc": "",
            "cert": [],
            "certifications": "",
            "opt_1": "",
            "opt_1_val": var_name,
            "handle": "",
            "vendor": "",
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
        "opt_1": base_data.get('opt_1'),
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
            base_data.get('opt_1') or None,
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
                        product = parse_product(prod_soup, driver)
                        if not product:
                            raise ValueError("Could not parse product data")

                        var_block = prod_soup.select_one('div[id="finish-type-list"]')
                        var_inputs = var_block.select('input[name="product-color"]') if var_block else []

                        if var_inputs and len(var_inputs) > 1:
                            product['SKU'] = ''
                            product['opt_1'] = 'Colour/Finish'
                            variants = parse_variant(driver)
                            if not variants:
                                raise ValueError("No variant data returned from finish swatches")
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
                        update_queue_status(cur, pid, 'crashed' if isinstance(e, WebDriverException) or 'no such window' in str(e).lower() or 'session' in str(e).lower() or not getattr(driver, 'session_id', None) else 'failed')
                        conn.commit()
                        totals["failed"] += 1
                        consecutive_fails += 1
                        items_since_restart += 1
                        if consecutive_fails == 3:
                            notify_telegram(
                                f"🔴 <b>{BRAND_NAME}</b>: 3 failures in a row\n"
                                f"Last: {link}"
                            )
                            logger.warning("Restarting Chrome driver after 3 consecutive failures...")
                            try:
                                driver.quit()
                            except Exception:
                                pass
                            force_kill_chrome()
                            subprocess.run(['sh', '-c', 'rm -rf /tmp/.org.chromium* /tmp/chrome_*'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            driver = setup_driver()
                            consecutive_fails = 0


        # ── Crash retry loop ─────────────────────────────────────────────────
        stall_count = 0
        prev_crashed = None
        while True:
            cur.execute(
                "SELECT id, link, category FROM parsing_queue "
                "WHERE vendor = %s AND status = 'crashed'",
                (BRAND_NAME,)
            )

            crashed_rows = cur.fetchall()
            if not crashed_rows:
                break
            current_crashed = len(crashed_rows)
            if current_crashed == prev_crashed:
                stall_count += 1
            else:
                stall_count = 0
            prev_crashed = current_crashed
            if stall_count >= 2:
                logger.warning(f"Crashed items not recovering after 2 retries — marking {current_crashed} as failed")
                cur.executemany(
                    "UPDATE parsing_queue SET status='failed' WHERE id=%s",
                    [(r[0],) for r in crashed_rows]
                )
                conn.commit()
                totals["failed"] += current_crashed
                break
            logger.info(f"Retrying {current_crashed} crashed items (stall_count={stall_count})...")
            try:
                driver.quit()
            except Exception:
                pass
            force_kill_chrome()
            subprocess.run(['sh', '-c', 'rm -rf /tmp/.org.chromium* /tmp/chrome_*'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            driver = setup_driver()
            for pid, link, cat_url in tqdm(crashed_rows, desc=f"Retry crashed {BRAND_NAME}"):
                try:
                    prod_html = fetch_page(link, driver)
                    stats = save_with_diff(cur, product_to_save)
                    update_queue_status(cur, pid, 'processed')
                    conn.commit()
                    consecutive_fails = 0
                    totals["products_processed"] += 1
                except Exception as e:
                    logger.error(f"RETRY FAILURE on item {pid} ({link}): {e}")
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    update_queue_status(cur, pid, 'crashed')
                    conn.commit()
        # ─────────────────────────────────────────────────────────────────────
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
