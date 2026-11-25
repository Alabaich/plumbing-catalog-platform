"""
_helpers.py — Shared test utilities for proxy smoke tests.
"""

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

API_BASE       = os.getenv("PROXY_TEST_API_BASE", "http://localhost:8000")
# Shopify app secret used to sign App Proxy requests — never hardcode it.
SHOPIFY_SECRET = os.getenv("SHOPIFY_API_SECRET", "")
TEST_SHOP      = os.getenv("PROXY_TEST_SHOP", "example-store.myshopify.com")
TEST_HANDLE    = os.getenv("PROXY_TEST_HANDLE", "example-product-handle")
TEST_VENDOR    = os.getenv("PROXY_TEST_VENDOR", "EXAMPLE")

if not SHOPIFY_SECRET:
    raise SystemExit("SHOPIFY_API_SECRET is not set — export it before running the proxy smoke tests.")


def sign(params: dict, secret: str) -> str:
    message = "".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def req(path: str, params: dict) -> tuple[int, any]:
    url = f"{API_BASE}{path}?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, raw.decode()
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw.decode()


def make_params(shop: str = TEST_SHOP, extra: dict = {}) -> dict:
    p = {
        "shop":        shop,
        "timestamp":   str(int(time.time())),
        "nonce":       uuid.uuid4().hex,
        "path_prefix": "/apps/catalog",
    }
    p.update(extra)
    p["signature"] = sign(p, SHOPIFY_SECRET)
    return p


def test(label: str, status: int, body, expect: int) -> bool:
    ok = status == expect
    icon = "✅" if ok else "❌"
    print(f"{icon}  [{status}] {label}")
    if not ok:
        print(f"     expected {expect}, got: {body}")
    return ok