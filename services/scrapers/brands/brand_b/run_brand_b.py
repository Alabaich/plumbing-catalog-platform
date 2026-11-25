"""
Brand B — Runner: executes collect → scrape in sequence.
Sends a Telegram alert if either step crashes entirely.
"""

import subprocess
import sys
import os

# --- Use shared logger ---
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from utils.logger import get_logger, notify_telegram

BRAND_NAME = os.getenv("BRAND_B_NAME", "BRAND_B")
logger = get_logger(BRAND_NAME, "runner")

BASE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def run_step(step_name: str, script_file: str) -> bool:
    """Run a script as subprocess. Returns True on success."""
    script_path = os.path.join(BASE, script_file)
    logger.info(f"STEP: {step_name} — running {script_file}")

    result = subprocess.run(
        [PY, script_path],
        capture_output=False,  # let child print to console
    )

    if result.returncode != 0:
        msg = f"{step_name} exited with code {result.returncode}"
        logger.error(msg)
        notify_telegram(
            f"🔴 <b>{BRAND_NAME} RUNNER FAILED</b>\n"
            f"<b>Step:</b> {step_name}\n"
            f"<b>Script:</b> {script_file}\n"
            f"<b>Exit code:</b> {result.returncode}"
        )
        return False

    logger.info(f"STEP: {step_name} — completed successfully.")
    return True


def main():
    logger.info(f"=== {BRAND_NAME} FULL RUN STARTED ===")

    ok = run_step("1 — Collect URLs", "brand_b_collect_urls.py")
    if not ok:
        logger.error("Collect failed. Skipping scrape step.")
        logger.info(f"=== {BRAND_NAME} FULL RUN FINISHED (WITH ERRORS) ===")
        return

    run_step("2 — Scrape Products", "brand_b_scrape_products.py")

    logger.info(f"=== {BRAND_NAME} FULL RUN FINISHED ===")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.critical(f"RUNNER CRASHED: {e}", exc_info=True)