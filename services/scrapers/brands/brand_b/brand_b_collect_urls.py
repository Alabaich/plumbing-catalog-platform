"""
Brand B — Step 1: Collect product URLs from category pages into parsing_queue.

Category pages use a "Load More" button until all products are shown.
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
BRAND_NAME = os.getenv("BRAND_B_NAME", "BRAND_B")
logger = get_logger(BRAND_NAME, "collect")

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("POSTGRES_DB", "plumbing_products")
DB_USER = os.getenv("POSTGRES_USER", "plumbing_app")
DB_PASS = os.getenv("POSTGRES_PASSWORD", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Site root and category listing pages come from the environment
# (see .env.example), so the scraper code carries no site-specific URLs.
BASE_URL = os.getenv("BRAND_B_BASE_URL", "").rstrip("/")
CATEGORY_URLS = [u.strip() for u in os.getenv("BRAND_B_CATEGORY_URLS", "").split(",") if u.strip()]

ITEM = "a.card-title"


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
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_argument(
        '--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    )
    try:
        return webdriver.Chrome(options=options)
    except WebDriverException as e:
        logger.critical(f"DRIVER SETUP FAILED: {e}")
        raise


def extract_links(page_source, cat_url):
    soup = bs(page_source, "lxml")
    extracted = []
    for el in soup.select(ITEM):
        href = el.get('href')
        if href:
            full = href if href.startswith('http') else f"{BASE_URL}{href}"
            extracted.append({'cat': cat_url, 'url': full})
    return extracted


def dismiss_cookies(driver):
    selector = "button[id='onetrust-accept-btn-handler']"
    try:
        WebDriverWait(driver, 2).until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
        btn = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
        btn.click()
        logger.info("Dismissed cookie banner.")
        WebDriverWait(driver, 10).until(EC.invisibility_of_element_located((By.CSS_SELECTOR, selector)))
    except TimeoutException:
        pass


def scroll_and_load(driver, cat_url):
    logger.info(f"Navigating to: {cat_url}")
    try:
        driver.get(cat_url)
        dismiss_cookies(driver)
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'footer.site-footer'))
        )
    except Exception as e:
        logger.warning(f"Error loading {cat_url}: {e}")
        return 0

    # Click "Load More" until gone
    while True:
        try:
            btn = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".btn.btn-more.btn-info.btn-block.load-more"))
            )
            driver.execute_script("arguments[0].scrollIntoView({behavior: 'auto', block: 'center'});", btn)
            time.sleep(1)
            btn.click()
            time.sleep(1)
        except Exception:
            logger.info("End of pagination.")
            break

    products = extract_links(driver.page_source, cat_url)
    if products:
        save_to_db(products)
        logger.info(f"  {cat_url} — {len(products)} products pushed.")
    else:
        logger.warning(f"  No products found for {cat_url}")

    return len(products)


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

        total = 0
        for cat in CATEGORY_URLS:
            total += scroll_and_load(driver, cat)

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
