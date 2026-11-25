"""
Brand C — Step 1: Collect product URLs from category pages into parsing_queue.

Category URLs are paginated via ?currentPage=N (0-based)
until no products are found.
"""

import sys
import os
import time
import subprocess
import psycopg2
from bs4 import BeautifulSoup as bs
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from utils.logger import get_logger, run_with_crash_alert

# --- CONFIGURATION ---
BRAND_NAME = os.getenv("BRAND_C_NAME", "BRAND_C")
logger = get_logger(BRAND_NAME, "collect")

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("POSTGRES_DB", "plumbing_products")
DB_USER = os.getenv("POSTGRES_USER", "plumbing_app")
DB_PASS = os.getenv("POSTGRES_PASSWORD", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Site root and category listing pages come from the environment
# (see .env.example), so the scraper code carries no site-specific URLs.
BASE_URL = os.getenv("BRAND_C_BASE_URL", "").rstrip("/")
CAT_URLS = [u.strip() for u in os.getenv("BRAND_C_CATEGORY_URLS", "").split(",") if u.strip()]

ITEM = "fh-product-grid-item[class='p-item ng-star-inserted']"


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


def force_kill_chrome():
    try:
        subprocess.run(['pkill', '-f', 'chrome'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['pkill', '-f', 'chromedriver'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info("Killed lingering Chrome processes.")
    except Exception as e:
        logger.warning(f"Could not kill Chrome: {e}")


def save_to_db(products):
    if not products:
        return
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        insert_query = """
            INSERT INTO parsing_queue (vendor, link, category, status)
            VALUES (%s, %s, %s, 'pending')
            ON CONFLICT (link) DO UPDATE SET
                status = 'pending',
                updated_at = CURRENT_TIMESTAMP;
        """
        data = [(BRAND_NAME, p['url'], p['cat']) for p in products]
        cur.executemany(insert_query, data)
        conn.commit()
        logger.info(f"DB: pushed {len(data)} links to parsing_queue.")
        cur.close()
    except Exception as e:
        logger.error(f"DATABASE ERROR: {e}", exc_info=True)
    finally:
        if conn:
            conn.close()


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


def fetch_cat_page(url, driver):
    selector = "section[class='plp plp-grid v-grid']"
    for attempt in range(2):
        try:
            if attempt == 0:
                driver.get(url)
            else:
                logger.warning(f"Timeout on attempt 1, refreshing page: {url}")
                time.sleep(3)
                driver.refresh()
            WebDriverWait(driver, 35).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, selector))
            )
            return driver.page_source
        except TimeoutException:
            if attempt == 0:
                continue
            logger.warning(f"Timeout waiting for element on {url} (after refresh)")
            return None
        except WebDriverException as e:
            logger.warning(f"WebDriver error on {url}: {e}")
            return None
        except Exception as e:
            logger.warning(f"Unexpected error on {url}: {e}")
            return None


def get_product_urls(driver):
    total = 0

    for cat_url in CAT_URLS:
        logger.info(f"Processing: {cat_url}")
        page = 0
        while True:
            page_url = f"{cat_url}?currentPage={page}"
            logger.info(f"  Page {page}: {page_url}")
            html = fetch_cat_page(page_url, driver)
            time.sleep(2)
            if not html:
                break

            soup = bs(html, "lxml")
            prod_el = soup.select(ITEM)
            if not prod_el:
                logger.info(f"  No products on page {page} — stopping.")
                break

            batch = []
            for el in prod_el:
                a = el.select_one('a')
                if a and a.get('href'):
                    batch.append({'cat': cat_url, 'url': f"{BASE_URL}{a.get('href')}"})

            save_to_db(batch)
            logger.info(f"  Found {len(batch)} products on page {page}.")
            total += len(batch)
            page += 1

        logger.info(f"  Category total: {total}")

    return total


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def collect_urls():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE parsing_queue SET status = 'pending' WHERE vendor = %s AND status = 'failed'",
            (BRAND_NAME,)
        )
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Failed to reset failed items: {e}")
    finally:
        if conn:
            conn.close()

    force_kill_chrome()
    driver = None
    try:
        driver = setup_driver()
        logger.info("--- STARTING COLLECT JOB ---")

        total = get_product_urls(driver)

        if total > 0:
            logger.info(f"JOB SUCCESS: Total links pushed: {total}")
        else:
            logger.warning("JOB WARNING: Zero products found.")

    except Exception as e:
        logger.critical(f"CRITICAL COLLECT ERROR: {e}", exc_info=True)
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        logger.info("--- FINISHED COLLECT JOB ---")
        force_kill_chrome()


if __name__ == "__main__":
    run_with_crash_alert(BRAND_NAME, "collect", collect_urls)
