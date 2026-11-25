"""
Brand A scraper — wraps existing collect/scrape scripts into BaseScraper interface.

Workers: when adding a NEW brand, copy this file as a template.
Replace BRAND_NAME and point collect_urls/scrape_products to your scripts.
"""

import sys
import os

# Ensure the scrapers root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from base_scraper import BaseScraper


class BrandAScraper(BaseScraper):
    BRAND_NAME = os.getenv("BRAND_A_NAME", "BRAND_A")

    def collect_urls(self):
        from brands.brand_a.brand_a_collect_urls import collect_urls
        collect_urls()

    def scrape_products(self):
        from brands.brand_a.brand_a_scrape_products import main
        main()
