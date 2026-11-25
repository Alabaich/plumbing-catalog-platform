"""
Brand registry — auto-discovers all brand scraper classes.

How it works:
1. Scans brands/ for subdirectories
2. Each subdirectory must have __init__.py with a class inheriting BaseScraper
3. The class is imported and registered

To add a new brand:
1. Create folder: brands/acme/
2. Create __init__.py with AcmeScraper(BaseScraper)
3. Create acme_collect_urls.py and acme_scrape_products.py
4. Done — orchestrator picks it up automatically

To DISABLE a brand temporarily:
- Add a file: brands/acme/DISABLED
- Or remove/rename __init__.py
"""

import os
import importlib
import sys

# Make sure scrapers root is importable
_SCRAPERS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRAPERS_ROOT not in sys.path:
    sys.path.insert(0, _SCRAPERS_ROOT)

from base_scraper import BaseScraper
from utils.logger import notify_telegram


def discover_brands() -> list[BaseScraper]:
    """
    Auto-discover and instantiate all brand scrapers.
    Returns list of BaseScraper instances, sorted alphabetically.
    """
    brands_dir = os.path.dirname(os.path.abspath(__file__))
    scrapers = []

    for entry in sorted(os.listdir(brands_dir)):
        brand_path = os.path.join(brands_dir, entry)

        # Skip non-directories and __pycache__
        if not os.path.isdir(brand_path):
            continue
        if entry.startswith("__"):
            continue

        # Check for DISABLED flag
        if os.path.exists(os.path.join(brand_path, "DISABLED")):
            print(f"[brands] {entry} — DISABLED, skipping")
            continue

        # Check for __init__.py
        init_file = os.path.join(brand_path, "__init__.py")
        if not os.path.exists(init_file):
            print(f"[brands] {entry} — no __init__.py, skipping")
            continue

        # Import the module
        # Import the module
        try:
            module = importlib.import_module(f"brands.{entry}")

            # Find the BaseScraper subclass
            found = False
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, BaseScraper)
                    and attr is not BaseScraper
                ):
                    instance = attr()
                    scrapers.append(instance)
                    print(f"[brands] {entry} — registered: {instance.BRAND_NAME}")
                    found = True
                    break

            if not found:
                print(f"[brands] {entry} — no BaseScraper subclass found, skipping")

        except Exception as e:
            print(f"[brands] {entry} — import error: {e}")
            notify_telegram(
                f"🔴 <b>BRAND IMPORT FAILED</b>\n"
                f"<b>Folder:</b> {entry}\n"
                f"<b>Error:</b> {e}"
            )

    return scrapers
