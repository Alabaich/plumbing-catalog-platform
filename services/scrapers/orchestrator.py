"""
Orchestrator — main entry point for the scrapers container.

What it does:
1. Discovers all registered brands in brands/
2. Runs each brand's collect → scrape cycle in sequence
3. Pauses between brands (polite crawling)
4. After all brands done, waits and starts over
5. Sends Telegram status updates at cycle start/end
6. After each brand, enqueues LLM enrichment jobs (RQ) for products whose
   diff-engine hash changed — skipped when no LLM key is configured

The parsing_queue table holds one row per product URL:
- collect_urls() upserts on the unique link and sets status='pending',
  so re-runs never duplicate a URL and every cycle revisits each product
- scrape_products() only processes status='pending'
- the diff engine then skips DB writes for products that did not change

Usage:
    python orchestrator.py              # continuous loop
    RUN_ONCE=true python orchestrator.py  # single cycle then exit
"""

import time
import datetime
import sys
import os

# Make sure we can import from the scrapers root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import PAUSE_BETWEEN_BRANDS, PAUSE_BETWEEN_CYCLES, RUN_ONCE
from brands import discover_brands
from enrichment import enqueue_changed
from utils.logger import get_logger, notify_status, notify_telegram


logger = get_logger("ORCHESTRATOR", "main")


def enqueue_enrichment(brand_name: str) -> None:
    """
    Hand new/changed products of a brand to the enrichment queue.
    Enrichment is optional: a missing key, Redis outage or missing table
    must never stop the scrape cycle, so failures are logged and swallowed.
    """
    try:
        enqueue_changed(brand_name)
    except Exception as e:
        logger.warning(f"Enrichment enqueue failed for {brand_name}: {e}")


def run_cycle(scrapers) -> dict:
    """
    Run one full cycle through all brands.
    Returns summary stats.
    """
    cycle_start = time.time()
    results = []

    logger.info(f"=== CYCLE STARTED — {len(scrapers)} brands ===")
    notify_status(
        f"🚀 <b>Scrape cycle started</b>\n"
        f"Brands: {', '.join(s.BRAND_NAME for s in scrapers)}\n"
        f"Time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )

    for i, scraper in enumerate(scrapers, 1):
        logger.info(f"--- Brand {i}/{len(scrapers)}: {scraper.BRAND_NAME} ---")

        try:
            stats = scraper.run()
            results.append(stats)
            enqueue_enrichment(scraper.BRAND_NAME)
        except Exception as e:
            logger.error(f"Unhandled error in {scraper.BRAND_NAME}: {e}", exc_info=True)
            results.append({
                "brand": scraper.BRAND_NAME,
                "collect_ok": False,
                "scrape_ok": False,
                "error": str(e),
            })

        # Pause between brands (skip after last one)
        if i < len(scrapers):
            logger.info(f"Pausing {PAUSE_BETWEEN_BRANDS}s before next brand...")
            time.sleep(PAUSE_BETWEEN_BRANDS)

    cycle_time = round(time.time() - cycle_start)

    # Summary
    success = sum(1 for r in results if r.get("scrape_ok"))
    failed = len(results) - success
    failed_brands = [r["brand"] for r in results if not r.get("scrape_ok")]

    summary = (
        f"📊 <b>Cycle complete</b> — {cycle_time}s\n"
        f"✅ Success: {success}/{len(results)}\n"
    )
    if failed_brands:
        summary += f"❌ Failed: {', '.join(failed_brands)}\n"

    logger.info(f"=== CYCLE DONE — {success}/{len(results)} OK, {cycle_time}s ===")
    notify_status(summary)

    return {
        "total": len(results),
        "success": success,
        "failed": failed,
        "failed_brands": failed_brands,
        "cycle_time_sec": cycle_time,
    }


def main():
    logger.info("=" * 60)
    logger.info("ORCHESTRATOR STARTING")
    logger.info("=" * 60)

    # Discover brands
    scrapers = discover_brands()

    if not scrapers:
        logger.warning("No brands discovered! Add brand folders to brands/")
        notify_telegram("⚠️ <b>Orchestrator</b>: no brands found. Nothing to scrape.")
        return

    logger.info(f"Discovered {len(scrapers)} brands: {[s.BRAND_NAME for s in scrapers]}")

    cycle_num = 0

    while True:
        cycle_num += 1
        logger.info(f"\n{'='*60}")
        logger.info(f"CYCLE #{cycle_num}")
        logger.info(f"{'='*60}")

        run_cycle(scrapers)

        if RUN_ONCE:
            logger.info("RUN_ONCE=true — exiting after single cycle.")
            break

        logger.info(f"Cycle done. Sleeping {PAUSE_BETWEEN_CYCLES}s before next cycle...")
        logger.info(f"Next cycle at: {datetime.datetime.now() + datetime.timedelta(seconds=PAUSE_BETWEEN_CYCLES)}")
        time.sleep(PAUSE_BETWEEN_CYCLES)

    logger.info("ORCHESTRATOR STOPPED")


if __name__ == "__main__":
    main()
