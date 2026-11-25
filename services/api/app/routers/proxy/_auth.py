"""
_auth.py — Shared security layer for the App Proxy.

Contains:
    - _verify_hmac()       HMAC-SHA256 + timestamp TTL check
    - _consume_nonce()     Redis SETNX replay protection
    - ProxyContext         Dataclass returned to every endpoint
    - get_proxy_context()  FastAPI dependency — runs the full security chain
"""

import hashlib
import hmac
import time
from dataclasses import dataclass, field
from typing import List

import redis.asyncio as aioredis
from fastapi import Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import get_settings
from ...dependencies import get_db


# ---------------------------------------------------------------------------
# ProxyContext — passed to every endpoint via Depends(get_proxy_context)
# ---------------------------------------------------------------------------

@dataclass
class ProxyContext:
    shop_domain: str
    tenant_id: str
    allowed_vendors: List[str] = field(default_factory=list)
    # empty list = unrestricted (no rows in tenant_subscribed_brands)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _verify_hmac(params, secret: str, ttl: int) -> None:
    """
    Raise HTTPException if the request signature is missing, expired, or wrong.

    Shopify signs the full query string as:
        key1=val1key2=val2...  (sorted, no separator, signature excluded)
    """
    provided = params.get("signature", "")
    if not provided:
        raise HTTPException(status_code=401, detail="Missing signature")

    ts_raw = params.get("timestamp", "")
    if not ts_raw:
        raise HTTPException(status_code=401, detail="Missing timestamp")
    try:
        ts = int(ts_raw)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid timestamp")
    if abs(time.time() - ts) > ttl:
        raise HTTPException(status_code=401, detail="Signature expired")

    grouped: dict[str, list[str]] = {}
    for k, v in params.multi_items():
        if k == "signature":
            continue
        grouped.setdefault(k, []).append(v)

    message = "".join(f"{k}={','.join(vs)}" for k, vs in sorted(grouped.items()))
    expected = hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=401, detail="Invalid signature")


async def _consume_nonce(redis: aioredis.Redis, nonce: str, ttl: int) -> None:
    """
    SETNX the nonce key. Already exists → replay attack → 401.
    """
    if not nonce:
        raise HTTPException(status_code=401, detail="Missing nonce")

    stored = await redis.set(f"proxy:nonce:{nonce}", 1, nx=True, ex=ttl)
    if stored is None:
        raise HTTPException(status_code=401, detail="Replayed request")


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------

async def get_proxy_context(
    request: Request,
    db: Session = Depends(get_db),
) -> ProxyContext:
    """
    Runs the full security chain on every authenticated proxy request:
        1. HMAC + timestamp
        2. Nonce (replay protection)
        3. Tenant active-check
        4. Brand-scope load
    """
    settings = get_settings()

    _verify_hmac(request.query_params, settings.SHOPIFY_API_SECRET, settings.PROXY_TIMESTAMP_TTL)

    nonce = request.query_params.get("nonce", "")
    redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        await _consume_nonce(redis, nonce, settings.PROXY_NONCE_TTL)
    finally:
        await redis.aclose()

    shop_domain = request.query_params.get("shop", "")
    if not shop_domain:
        raise HTTPException(status_code=401, detail="Missing shop parameter")

    row = db.execute(
        text("SELECT id, is_active FROM tenants WHERE shop_domain = :shop"),
        {"shop": shop_domain},
    ).fetchone()

    if row is None:
        raise HTTPException(status_code=401, detail="Unknown shop")
    if not row.is_active:
        raise HTTPException(status_code=403, detail="Shop is not active")

    brands = db.execute(
        text("SELECT vendor FROM tenant_subscribed_brands WHERE shop_domain = :shop"),
        {"shop": shop_domain},
    ).fetchall()

    return ProxyContext(
        shop_domain=shop_domain,
        tenant_id=str(row.id),
        allowed_vendors=[r.vendor for r in brands],
    )