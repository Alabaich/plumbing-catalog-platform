"""
rate_limit.py — Per-tenant Redis-backed rate limiting

Tiers (caps and score thresholds come from settings, see .env.example):
  Normal usage   → RATE_LIMIT_NORMAL_PER_MIN
  Low anomaly    → RATE_LIMIT_LOW_PER_MIN
  Medium anomaly → RATE_LIMIT_MEDIUM_PER_MIN
  High anomaly   → requests pass but product_svc poisons the data
  Critical       → key revoked instantly (handled by auth middleware on next req)

Also enforces hard global caps regardless of anomaly score to prevent
any single tenant from hammering the DB.
"""

import time
import json
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
import redis.asyncio as aioredis

from app.config import settings

# Hard caps (requests per minute) regardless of anomaly score
RATE_LIMITS = {
    "normal": settings.RATE_LIMIT_NORMAL_PER_MIN,   # a real storefront
    "low":    settings.RATE_LIMIT_LOW_PER_MIN,      # Curious dev
    "medium": settings.RATE_LIMIT_MEDIUM_PER_MIN,   # Testing limits — plus hint in response
    # high/critical handled in anti_scrape.py (poisoning / revoke)
}

# Redis key prefixes
_PREFIX_COUNT  = "rl:count:"   # rl:count:{tenant_id}:{window_minute}
_PREFIX_UNIQ   = "rl:slugs:"   # rl:slugs:{tenant_id}:{window_hour}
_PREFIX_IPS    = "rl:ips:"     # rl:ips:{tenant_id}:{window_hour}


class RateLimitMiddleware(BaseHTTPMiddleware):

    def __init__(self, app):
        super().__init__(app)
        # Lazy-init Redis pool so it doesn't block startup
        self._redis: aioredis.Redis | None = None

    async def _get_redis(self) -> aioredis.Redis:
        if not self._redis:
            self._redis = await aioredis.from_url(
                settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
            )
        return self._redis

    async def dispatch(self, request, call_next):
        # Skip health checks and proxy paths.
        # Proxy paths authenticate via HMAC, not API key — no tenant_id is set on
        # request.state here, and per-shop rate limiting is handled separately in
        # the proxy router (Phase 2.11).
        if request.url.path in ["/health", "/ready"] or request.url.path.startswith("/proxy/"):
            return await call_next(request)

        tenant_id = getattr(request.state, "tenant_id", None)
        if not tenant_id:
            # Auth middleware rejected it before us — just pass through
            return await call_next(request)

        anomaly_score = getattr(request.state, "anomaly_score", 0.0)

        redis = await self._get_redis()
        now = int(time.time())
        minute_window = now // 60
        hour_window = now // 3600

        # --- 1. Per-minute request count (sliding window) ---
        count_key = f"{_PREFIX_COUNT}{tenant_id}:{minute_window}"
        count = await redis.incr(count_key)
        if count == 1:
            await redis.expire(count_key, 90)  # 90s TTL covers window + buffer

        # Determine rate cap based on anomaly score
        if anomaly_score >= settings.RATE_LIMIT_MEDIUM_SCORE:
            cap = RATE_LIMITS["medium"]
            tier = "medium"
        elif anomaly_score >= settings.RATE_LIMIT_LOW_SCORE:
            cap = RATE_LIMITS["low"]
            tier = "low"
        else:
            cap = RATE_LIMITS["normal"]
            tier = "normal"

        if count > cap:
            retry_after = 60 - (now % 60)
            resp = JSONResponse(
                status_code=429,
                content={
                    "error": "Rate limit exceeded",
                    "retry_after": retry_after,
                    # Hint for medium tier — real clients should never hit this
                    **({"message": "Slow down. Contact support if this is unexpected."} if tier == "medium" else {}),
                },
            )
            resp.headers["Retry-After"] = str(retry_after)
            resp.headers["X-RateLimit-Limit"] = str(cap)
            resp.headers["X-RateLimit-Remaining"] = "0"
            return resp

        # --- 2. Track unique slugs requested this hour (for anomaly detection) ---
        # (Slug tracking feeds back into anti_scrape.py on the next request)
        slug = None
        path_parts = request.url.path.split("/")
        if len(path_parts) >= 5 and path_parts[3] == "products":
            slug = path_parts[4] if len(path_parts) > 4 else None
        if slug and slug not in ["", "list"]:
            slug_key = f"{_PREFIX_UNIQ}{tenant_id}:{hour_window}"
            await redis.sadd(slug_key, slug)
            if await redis.ttl(slug_key) < 0:
                await redis.expire(slug_key, 3700)

        # --- 3. Track unique IPs this hour ---
        ip = request.client.host if request.client else "unknown"
        ip_key = f"{_PREFIX_IPS}{tenant_id}:{hour_window}"
        await redis.sadd(ip_key, ip)
        if await redis.ttl(ip_key) < 0:
            await redis.expire(ip_key, 3700)

        # Pass rate limit stats to request state for anti_scrape to read
        request.state.req_count_this_minute = count
        request.state.unique_slug_count_this_hour = await redis.scard(
            f"{_PREFIX_UNIQ}{tenant_id}:{hour_window}"
        )
        request.state.unique_ip_count_this_hour = await redis.scard(
            f"{_PREFIX_IPS}{tenant_id}:{hour_window}"
        )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(cap)
        response.headers["X-RateLimit-Remaining"] = str(max(0, cap - count))
        return response