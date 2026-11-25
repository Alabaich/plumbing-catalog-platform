"""
TEMPLATE — Copy this folder to create a new brand scraper.

Steps:
1. Copy this folder:           cp -r brands/_template brands/acme
2. Rename this file:           (keep as __init__.py)
3. Update BRAND_NAME below
4. Create your collect + scrape scripts
5. Point collect_urls() and scrape_products() to them
6. Done — orchestrator picks it up automatically next cycle

To temporarily disable: touch brands/acme/DISABLED
To re-enable:           rm brands/acme/DISABLED
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from base_scraper import BaseScraper


class TemplateScraper(BaseScraper):
    # ⚠️ CHANGE THIS to your brand name (uppercase)
    BRAND_NAME = "TEMPLATE"

    def collect_urls(self):
        """
        Collect product URLs from category pages.
        Insert into parsing_queue with status='pending'.
        Use ON CONFLICT DO NOTHING so re-runs don't duplicate.
        """
        # ⚠️ CHANGE: import your collect script
        # from brands.acme.acme_collect_urls import collect_urls
        # collect_urls()
        raise NotImplementedError("Implement collect_urls for your brand")

    def scrape_products(self):
        """
        Scrape each product from parsing_queue WHERE status='pending'.
        After scraping, update status to 'processed' or 'failed'.
        """
        # ⚠️ CHANGE: import your scrape script
        # from brands.acme.acme_scrape_products import main
        # main()
        raise NotImplementedError("Implement scrape_products for your brand")
