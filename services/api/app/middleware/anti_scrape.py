import time
import hashlib
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.config import settings

class AntiScrapeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Skip health checks and proxy paths.
        # Proxy requests arrive from Shopify's servers — their User-Agent is not
        # browser-like and would incorrectly raise the anomaly score.
        if request.url.path in ["/health", "/ready"] or request.url.path.startswith("/proxy/"):
            return await call_next(request)

        anomaly_score = 0.0
        headers = request.headers

        # 1. User-Agent check
        user_agent = headers.get("user-agent", "").lower()
        if not user_agent or "python-requests" in user_agent or "curl" in user_agent or "postman" in user_agent:
            anomaly_score += settings.ANTI_SCRAPE_WEIGHT_SCRIPT_UA  # Clear sign of a script
        if "headless" in user_agent or "puppeteer" in user_agent:
            anomaly_score += settings.ANTI_SCRAPE_WEIGHT_HEADLESS_UA

        # 2. Referer check (must match the client's Shopify domain)
        referer = headers.get("referer", "")
        # expected_domain is meant to come from the DB (via the auth middleware)
        expected_domain = getattr(request.state, "shop_domain", None)
        
        if expected_domain:
            if not referer:
                anomaly_score += settings.ANTI_SCRAPE_WEIGHT_NO_REFERER  # Direct request without a Referer
            elif expected_domain not in referer:
                anomaly_score += settings.ANTI_SCRAPE_WEIGHT_FOREIGN_REFERER  # Someone else is trying to use the key

        # 3. Suspicious headers
        if not headers.get("accept-language") or not headers.get("accept-encoding"):
            anomaly_score += settings.ANTI_SCRAPE_WEIGHT_MISSING_HEADERS  # Most browsers always send these headers

        # Keep the score for later use (the rate limiter reads it)
        request.state.anomaly_score = anomaly_score

        # Critical score — block right away
        if anomaly_score >= settings.ANTI_SCRAPE_BLOCK_SCORE:
            # Instead of 403 we could return 200 with an empty array or fake data
            # to confuse a scraper. For now we return 403.
            return JSONResponse(
                status_code=403,
                content={"error": "Access Denied", "message": "Detected suspicious behavior."}
            )

        # Pass the request on
        response = await call_next(request)

        # 4. Watermarking & Signing Response
        # The signature lets us prove that the data was pulled with a specific API key
        tenant_id = getattr(request.state, "tenant_id", "anonymous")
        timestamp = str(int(time.time()))
        
        # Signature: sha256(tenant_id + timestamp + signing secret)
        # The secret comes from config (API_SIGNING_SECRET)
        signature_base = f"{tenant_id}:{timestamp}:{settings.API_SIGNING_SECRET}"
        signature = hashlib.sha256(signature_base.encode()).hexdigest()

        response.headers["X-Data-Signature"] = f"{timestamp}.{signature}"
        
        # Add a trace header identifying the tenant
        response.headers["X-Trace-Id"] = f"tnt-{tenant_id}"

        return response