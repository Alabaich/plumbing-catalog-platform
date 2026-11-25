"""
_cache.py — Redis cache helpers for the App Proxy.

Simple async get/set over redis.asyncio.
Cache values are raw JSON strings — serialization happens at the call site.

Key conventions (all proxy keys share the proxy: namespace):
    proxy:vendor:{handle}               — vendor lookup cache
    proxy:product:{handle}              — Phase 2.5 product detail
    proxy:desc:{handle}                 — Phase 2.6 full description
    proxy:variant:{handle}:{sku}        — Phase 2.7 variant gallery
    proxy:collection:{vendor}:p{page}   — Phase 2.8 collection pages
"""

import json
from typing import Any, Optional

import redis.asyncio as aioredis

from ...config import get_settings


def _redis() -> aioredis.Redis:
    return aioredis.from_url(get_settings().REDIS_URL, decode_responses=True)


async def cache_get(key: str) -> Optional[Any]:
    """Return parsed JSON value for key, or None on miss / error."""
    r = _redis()
    try:
        raw = await r.get(key)
        return json.loads(raw) if raw is not None else None
    except Exception:
        return None
    finally:
        await r.aclose()


async def cache_set(key: str, data: Any, ttl: int | None = None) -> None:
    """
    Serialize data to JSON and store under key with optional TTL (seconds).
    Silently swallows errors — cache failures must never break the response path.
    """
    if ttl is None:
        ttl = get_settings().REDIS_PROXY_CACHE_TTL
    r = _redis()
    try:
        await r.set(key, json.dumps(data), ex=ttl)
    except Exception:
        pass
    finally:
        await r.aclose()