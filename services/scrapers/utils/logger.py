"""
Shared logging module for all brand scrapers.

Features:
- JSON structured logs to logs/scrapers/<brand>_<action>.log
- Colored console output for local dev
- Telegram alerts: 3 channels (errors, status, changes)
- run_with_crash_alert() wrapper for main()

Usage:
    from utils.logger import get_logger, notify_telegram, notify_status, notify_changes

    logger = get_logger("ACME", "collect")
    notify_status("Cycle started — 5 brands queued")
    notify_changes("ACME: 12 new products found")
"""

import logging
import json
import os
import sys
import traceback
import datetime
import requests


# ---------------------------------------------------------------------------
# Telegram config — 3 channels
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# Channel routing
TG_CHAT_ERRORS = os.getenv("TG_CHAT_ERRORS", "")
TG_CHAT_STATUS = os.getenv("TG_CHAT_STATUS", "")
TG_CHAT_CHANGES = os.getenv("TG_CHAT_CHANGES", "")
TG_CHAT_FALLBACK = os.getenv("TELEGRAM_CHAT_IDS", os.getenv("TELEGRAM_CHAT_ID", ""))


def _resolve_chat_id(channel: str) -> str:
    """Pick the right chat_id. Falls back to single TELEGRAM_CHAT_ID."""
    mapping = {
        "errors": TG_CHAT_ERRORS,
        "status": TG_CHAT_STATUS,
        "changes": TG_CHAT_CHANGES,
    }
    chat_id = mapping.get(channel, "")
    return chat_id if chat_id else TG_CHAT_FALLBACK


# ---------------------------------------------------------------------------
# Log directory
# ---------------------------------------------------------------------------
_THIS_DIR = os.path.dirname(os.path.realpath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(_THIS_DIR, '..', '..', '..'))
LOG_DIR = os.path.join(PROJECT_ROOT, 'logs', 'scrapers')
os.makedirs(LOG_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Telegram notifiers
# ---------------------------------------------------------------------------
def _send_telegram(chat_ids: str, message: str):
    """Low-level send to multiple comma-separated chat_ids. Silently fails if not configured."""
    if not TELEGRAM_BOT_TOKEN or not chat_ids:
        return
    for chat_id in chat_ids.split(","):
        chat_id = chat_id.strip()
        if not chat_id:
            continue
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": message[:4096],
                "parse_mode": "HTML",
            }
            requests.post(url, json=payload, timeout=10)
        except Exception:
            pass


def notify_telegram(message: str):
    """Send to ERRORS channel (backward compat)."""
    _send_telegram(_resolve_chat_id("errors"), message)


def notify_status(message: str):
    """Send to STATUS channel — cycle start/end, stats."""
    _send_telegram(_resolve_chat_id("status"), message)


def notify_changes(message: str):
    """Send to CHANGES channel — new products, price changes."""
    _send_telegram(_resolve_chat_id("changes"), message)


# ---------------------------------------------------------------------------
# JSON file formatter
# ---------------------------------------------------------------------------
class JSONFormatter(logging.Formatter):
    def __init__(self, brand: str, action: str):
        super().__init__()
        self.brand = brand.lower()
        self.action = action.lower()

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "service": "scraper",
            "brand": self.brand,
            "action": self.action,
            "level": record.levelname,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Colored console formatter
# ---------------------------------------------------------------------------
class ColoredFormatter(logging.Formatter):
    GREY = "\x1b[38;20m"
    GREEN = "\x1b[32;20m"
    YELLOW = "\x1b[33;20m"
    RED = "\x1b[31;20m"
    BOLD_RED = "\x1b[31;1m"
    RESET = "\x1b[0m"

    COLORS = {
        logging.DEBUG: GREY,
        logging.INFO: GREEN,
        logging.WARNING: YELLOW,
        logging.ERROR: RED,
        logging.CRITICAL: BOLD_RED,
    }

    def __init__(self, brand: str, action: str):
        self.brand = brand.upper()
        self.action = action.upper()
        super().__init__()

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelno, self.GREY)
        fmt = f"{color}[%(asctime)s] [%(levelname)s] [{self.brand}-{self.action}] %(message)s{self.RESET}"
        formatter = logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S")
        return formatter.format(record)


# ---------------------------------------------------------------------------
# Telegram handler — fires on ERROR and above
# ---------------------------------------------------------------------------
class TelegramHandler(logging.Handler):
    def __init__(self, brand: str, action: str):
        super().__init__(level=logging.ERROR)
        self.brand = brand.upper()
        self.action = action.upper()

    def emit(self, record: logging.LogRecord):
        try:
            emoji = "🔴" if record.levelno >= logging.CRITICAL else "🟠"
            text = (
                f"{emoji} <b>SCRAPER {record.levelname}</b>\n"
                f"<b>Brand:</b> {self.brand}\n"
                f"<b>Action:</b> {self.action}\n"
                f"<b>Message:</b> {record.getMessage()}\n"
            )
            if record.exc_info and record.exc_info[0] is not None:
                tb = traceback.format_exception(*record.exc_info)
                text += f"<pre>{''.join(tb[-3:])}</pre>"
            notify_telegram(text)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main factory
# ---------------------------------------------------------------------------
def get_logger(brand: str, action: str) -> logging.Logger:
    """
    Returns a configured logger instance.

    Args:
        brand:  e.g. "ACME"
        action: e.g. "collect", "scrape", "orchestrator"

    Log file: logs/scrapers/acme_collect.log  (JSON, one object per line)
    Console:  colored human-readable
    Telegram: ERROR and CRITICAL only (to errors channel)
    """
    name = f"scraper.{brand.lower()}.{action.lower()}"
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False

    # 1. JSON file handler
    log_file = os.path.join(LOG_DIR, f"{brand.lower()}_{action.lower()}.log")
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(JSONFormatter(brand, action))
    logger.addHandler(fh)

    # 2. Colored console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(ColoredFormatter(brand, action))
    logger.addHandler(ch)

    # 3. Telegram handler (ERROR+)
    th = TelegramHandler(brand, action)
    logger.addHandler(th)

    return logger


# ---------------------------------------------------------------------------
# Crash catcher
# ---------------------------------------------------------------------------
def run_with_crash_alert(brand: str, action: str, func, *args, **kwargs):
    """
    Runs `func` and catches unhandled exceptions.
    Sends CRITICAL Telegram alert if the process crashes.
    """
    logger = get_logger(brand, action)
    try:
        return func(*args, **kwargs)
    except Exception as e:
        logger.critical(f"PROCESS CRASHED: {e}", exc_info=True)
        return None
