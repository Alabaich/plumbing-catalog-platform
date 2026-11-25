"""
Base Scraper — abstract class that all brand scrapers must inherit.

Each brand folder has 3 files:
    brands/acme/acme_collect_urls.py  — collect product URLs
    brands/acme/acme_scrape_products.py — scrape each product
    brands/acme/run_acme.py — run collect → scrape

But the ORCHESTRATOR doesn't call those files directly.
Instead, each brand registers itself by creating a simple class:

    # brands/acme/__init__.py
    from base_scraper import BaseScraper

    class AcmeScraper(BaseScraper):
        BRAND_NAME = "ACME"
        
        def collect_urls(self):
            from brands.acme.acme_collect_urls import collect_urls
            collect_urls()
        
        def scrape_products(self):
            from brands.acme.acme_scrape_products import main
            main()

This keeps existing scraper code untouched while giving the orchestrator
a clean interface to discover and run brands.
"""

from abc import ABC, abstractmethod
import json
import os
import time
from utils.logger import get_logger, run_with_crash_alert, notify_status, notify_changes, LOG_DIR


class BaseScraper(ABC):
    """
    Abstract base class for brand scrapers.
    
    Subclasses MUST define:
        BRAND_NAME: str  — e.g. "ACME"
    
    Subclasses MUST implement:
        collect_urls()     — gather product URLs into parsing_queue
        scrape_products()  — scrape each URL from the queue
    """
    
    BRAND_NAME: str = ""
    
    def __init__(self):
        if not self.BRAND_NAME:
            raise ValueError("BRAND_NAME must be set on the scraper class")
        self.logger = get_logger(self.BRAND_NAME, "runner")
    
    @abstractmethod
    def collect_urls(self):
        """Collect product URLs from category/listing pages into parsing_queue."""
        pass
    
    @abstractmethod
    def scrape_products(self):
        """Scrape individual product pages from parsing_queue."""
        pass
    
    def run(self) -> dict:
        """
        Execute full scrape cycle: collect → scrape.
        Returns stats dict with results.
        """
        stats = {
            "brand": self.BRAND_NAME,
            "collect_ok": False,
            "scrape_ok": False,
            "error": None,
        }
        
        self.logger.info(f"=== {self.BRAND_NAME} FULL RUN STARTED ===")
        notify_status(f"🔄 <b>{self.BRAND_NAME}</b> — scrape cycle started")
        
        start = time.time()
        
        # Step 1: Collect URLs
        try:
            self.logger.info("Step 1: Collecting URLs...")
            self.collect_urls()
            stats["collect_ok"] = True
            self.logger.info("Step 1: Collect — OK")
        except Exception as e:
            self.logger.error(f"Step 1: Collect FAILED: {e}", exc_info=True)
            stats["error"] = f"collect: {e}"
            self.logger.info(f"=== {self.BRAND_NAME} FULL RUN FINISHED (WITH ERRORS) ===")
            return stats
        
        # Step 2: Scrape products
        try:
            self.logger.info("Step 2: Scraping products...")
            self.scrape_products()
            stats["scrape_ok"] = True
            self.logger.info("Step 2: Scrape — OK")
        except Exception as e:
            self.logger.error(f"Step 2: Scrape FAILED: {e}", exc_info=True)
            stats["error"] = f"scrape: {e}"
        
        elapsed = round(time.time() - start)
        self.logger.info(f"=== {self.BRAND_NAME} FULL RUN FINISHED in {elapsed}s ===")

        # read scrape stats written by the scrape script
        scrape_line = ""
        try:
            stats_path = os.path.join(LOG_DIR, f"{self.BRAND_NAME.lower()}_last_stats.json")
            with open(stats_path) as _f:
                s = json.load(_f)
            scrape_line = (
                f"\nParsed: {s['parsed']} | "
                f"Updated: {s['updated']} | "
                f"Failed: {s['failed']}"
            )
        except Exception:
            pass

        status_emoji = "✅" if stats["scrape_ok"] else "⚠️"
        notify_status(
            f"{status_emoji} <b>{self.BRAND_NAME}</b> — finished in {elapsed}s\n"
            f"Collect: {'OK' if stats['collect_ok'] else 'FAIL'} | "
            f"Scrape: {'OK' if stats['scrape_ok'] else 'FAIL'}"
            f"{scrape_line}"
        )
        
        return stats
