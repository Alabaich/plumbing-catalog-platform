"""
logging.py — Structured request logging middleware for the API

What this does:
  - Logs every request as a JSON line to /logs/api/api.log
  - Mirrors ERROR+ to console (Docker picks it up)
  - Sends critical API events to Telegram (errors channel)

What goes to Telegram vs what stays in logs only — see ALERT TIERS below.

File location: services/api/app/middleware/logging.py
Log location:  /logs/api/api.log  (mounted host volume, survives restarts)
"""

import json
import logging
import os
import time
import traceback
from datetime import datetime, timezone

import requests
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ERRORS     = os.getenv("TG_CHAT_ERRORS", "")      # API errors channel
TG_CHAT_STATUS     = os.getenv("TG_CHAT_STATUS", "")      # Important events
TG_CHAT_FALLBACK   = os.getenv("TELEGRAM_CHAT_ID", "")    # Single-channel fallback

LOG_DIR = os.getenv("LOG_DIR", "/logs/api")
os.makedirs(LOG_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# ALERT TIERS — what goes to Telegram vs logs only
#
# ✅ TELEGRAM (errors channel) — you need to act NOW:
#   - API key revoked (active scraping confirmed)
#   - 500 errors (unhandled exceptions — your code broke)
#   - Auth middleware crash (all tenants locked out)
#   - DB connection lost (all endpoints returning 500)
#   - Redis connection lost (rate limiting blind)
#   - Anomaly score CRITICAL (>= 1.0) triggered on a tenant
#
# ✅ TELEGRAM (status channel) — good to know, no action needed:
#   - New tenant made their first API call (onboarding signal)
#   - A tenant's key was auto-suspended (billing / abuse)
#
# ❌ LOGS ONLY — noise at Telegram scale, grep when needed:
#   - 401 Unauthorized (expected — bad keys, expired tokens)
#   - 429 Rate limited (expected — anti-scrape working)
#   - 404 Not found (frontend bug, not your problem)
#   - 400 Bad request (bad cursor, validation error)
#   - Anomaly score low/medium (normal anti-scrape operation)
#   - Every successful 200 request (too much volume)
#   - Slow requests > 500ms (log for debugging, not urgent)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Telegram sender
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


def alert_error(message: str):
    """Send to errors channel. Use for: 500s, key revocations, DB/Redis down."""
    chat = TG_CHAT_ERRORS or TG_CHAT_FALLBACK
    _send_telegram(chat, message)


def alert_status(message: str):
    """Send to status channel. Use for: first tenant call, key suspended."""
    chat = TG_CHAT_STATUS or TG_CHAT_FALLBACK
    _send_telegram(chat, message)


# ---------------------------------------------------------------------------
# JSON file logger
# ---------------------------------------------------------------------------
class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "service": "api",
            "level": record.levelname,
            "message": record.getMessage(),
        }
        # Attach structured fields if passed via 'extra'
        for key in ("tenant_id", "endpoint", "status", "latency_ms",
                    "ip", "anomaly_score", "cache_hit", "method"):
            if hasattr(record, key):
                entry[key] = getattr(record, key)
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def _build_logger() -> logging.Logger:
    logger = logging.getLogger("api.requests")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False

    # JSON to file
    fh = logging.FileHandler(os.path.join(LOG_DIR, "api.log"), encoding="utf-8")
    fh.setFormatter(_JSONFormatter())
    fh.setLevel(logging.INFO)
    logger.addHandler(fh)

    # Plain text to stdout (Docker captures this)
    sh = logging.StreamHandler()
    sh.setLevel(logging.WARNING)  # Only warnings+ to console to avoid Docker log spam
    logger.addHandler(sh)

    return logger


_logger = _build_logger()


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
class LoggingMiddleware(BaseHTTPMiddleware):

    async def dispatch(self, request: Request, call_next) -> Response:
        # Skip health checks — not useful to log 288 times per day
        if request.url.path in ["/health", "/ready"]:
            return await call_next(request)

        start = time.perf_counter()

        # Capture response (and any unhandled exception)
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception as exc:
            # Unhandled exception — this is always a Telegram alert
            latency_ms = int((time.perf_counter() - start) * 1000)
            self._log_and_alert_crash(request, exc, latency_ms)
            raise  # Re-raise so FastAPI returns 500

        latency_ms = int((time.perf_counter() - start) * 1000)
        self._log_request(request, status_code, latency_ms)

        # Slow response warning (> 2s) — log only, no Telegram
        if latency_ms > 2000:
            _logger.warning(
                f"Slow response: {request.url.path} took {latency_ms}ms",
                extra=self._extra(request, status_code, latency_ms),
            )

        return response

    # ------------------------------------------------------------------
    # Logging helpers
    # ------------------------------------------------------------------

    def _extra(self, request: Request, status_code: int, latency_ms: int) -> dict:
        return {
            "tenant_id": getattr(request.state, "tenant_id", None),
            "method": request.method,
            "endpoint": request.url.path,
            "status": status_code,
            "latency_ms": latency_ms,
            "ip": request.client.host if request.client else None,
            "anomaly_score": getattr(request.state, "anomaly_score", 0.0),
        }

    def _log_request(self, request: Request, status_code: int, latency_ms: int):
        extra = self._extra(request, status_code, latency_ms)
        tenant_id = extra["tenant_id"] or "anon"

        if status_code >= 500:
            # 500 — always Telegram
            _logger.error(f"500 on {request.url.path}", extra=extra)
            alert_error(
                f"🔴 <b>API 500 ERROR</b>\n"
                f"<b>Endpoint:</b> {request.method} {request.url.path}\n"
                f"<b>Tenant:</b> {tenant_id}\n"
                f"<b>Latency:</b> {latency_ms}ms\n"
                f"<b>IP:</b> {extra['ip']}"
            )
        elif status_code == 429:
            # Rate limited — log only (this is anti-scrape working correctly)
            _logger.info(f"429 rate limited", extra=extra)
        elif status_code == 401:
            # Bad key — log only (expected noise)
            _logger.info(f"401 unauthorized", extra=extra)
        elif status_code >= 400:
            # Other 4xx — log only
            _logger.info(f"{status_code} client error", extra=extra)
        else:
            # Happy path — log at INFO
            _logger.info(f"200 ok", extra=extra)

    def _log_and_alert_crash(self, request: Request, exc: Exception, latency_ms: int):
        """Unhandled exception — always goes to Telegram immediately."""
        tenant_id = getattr(request.state, "tenant_id", "unknown")
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        tb_short = tb[-800:]  # Last 800 chars fits in Telegram message

        _logger.critical(
            f"UNHANDLED EXCEPTION: {exc}",
            exc_info=True,
            extra=self._extra(request, 500, latency_ms),
        )

        alert_error(
            f"🔴 <b>API CRASH — UNHANDLED EXCEPTION</b>\n"
            f"<b>Endpoint:</b> {request.method} {request.url.path}\n"
            f"<b>Tenant:</b> {tenant_id}\n"
            f"<b>Error:</b> {type(exc).__name__}: {str(exc)[:200]}\n\n"
            f"<pre>{tb_short}</pre>"
        )


# ---------------------------------------------------------------------------
# Standalone alert functions
# Called directly from other parts of the codebase, not from middleware
# ---------------------------------------------------------------------------

def alert_key_revoked(tenant_id: str, reason: str, ip: str):
    """
    Call this from anti_scrape.py when anomaly score hits CRITICAL
    and you revoke the API key.
    """
    alert_error(
        f"🚨 <b>API KEY REVOKED</b>\n"
        f"<b>Tenant:</b> {tenant_id}\n"
        f"<b>Reason:</b> {reason}\n"
        f"<b>IP:</b> {ip}\n"
        f"Check DB: UPDATE tenants SET is_active=false WHERE id='{tenant_id}'"
    )


def alert_db_down(error: str):
    """
    Call this from dependencies.py if DB connection pool fails.
    All endpoints will return 500 until this is fixed.
    """
    alert_error(
        f"🔴 <b>DATABASE CONNECTION LOST</b>\n"
        f"<b>Error:</b> {error[:300]}\n"
        f"All API endpoints are returning 500.\n"
        f"SSH in and check: docker compose ps"
    )


def alert_redis_down(error: str):
    """
    Call this from rate_limit.py if Redis is unreachable.
    Rate limiting is blind until fixed — scrapers can run unchecked.
    """
    alert_error(
        f"🟠 <b>REDIS CONNECTION LOST</b>\n"
        f"<b>Error:</b> {error[:300]}\n"
        f"Rate limiting is DISABLED. Scrapers can now bypass limits.\n"
        f"SSH in and check: docker compose ps"
    )


def alert_first_tenant_call(tenant_id: str, shop_domain: str):
    """
    Call this from auth.py when a tenant makes their very first API call.
    Good signal that onboarding worked.
    """
    alert_status(
        f"✅ <b>NEW TENANT FIRST CALL</b>\n"
        f"<b>Shop:</b> {shop_domain}\n"
        f"<b>Tenant ID:</b> {tenant_id}\n"
        f"Onboarding is working 🎉"
    )


def alert_scraping_detected(tenant_id: str, score: float, slug_count: int, ip_count: int):
    """
    Call this from anti_scrape.py when anomaly score crosses HIGH threshold.
    Data is being poisoned automatically — this is informational.
    """
    alert_error(
        f"🟠 <b>ACTIVE SCRAPING DETECTED</b>\n"
        f"<b>Tenant:</b> {tenant_id}\n"
        f"<b>Anomaly score:</b> {score:.2f}\n"
        f"<b>Unique slugs/hr:</b> {slug_count}\n"
        f"<b>Unique IPs/hr:</b> {ip_count}\n"
        f"⚠️ Poisoned data is being returned automatically.\n"
        f"Monitor and revoke key if it continues."
    )