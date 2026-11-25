"""
test_auth.py — Security chain tests (HMAC, timestamp, nonce, tenant).
"""

import time
import uuid

from ._helpers import SHOPIFY_SECRET, TEST_SHOP, make_params, req, sign, test


def run():
    print("\n── Auth ──────────────────────────────────────────")

    # 1. Health — no auth needed
    s, b = req("/proxy/catalog/health", {})
    test("Health — no auth needed", s, b, 200)

    # 2. Missing signature
    s, b = req("/proxy/catalog/product/x", {
        "shop": TEST_SHOP, "timestamp": str(int(time.time()))
    })
    test("Missing signature → 401", s, b, 401)

    # 3. Expired timestamp
    p = {"shop": TEST_SHOP, "timestamp": str(int(time.time()) - 120),
         "nonce": uuid.uuid4().hex, "path_prefix": "/apps/catalog"}
    p["signature"] = sign(p, SHOPIFY_SECRET)
    s, b = req("/proxy/catalog/product/x", p)
    test("Expired timestamp → 401", s, b, 401)

    # 4. Wrong secret
    p = make_params()
    p["signature"] = sign({k: v for k, v in p.items() if k != "signature"}, "wrong_secret")
    s, b = req("/proxy/catalog/product/x", p)
    test("Wrong secret → 401", s, b, 401)

    # 5. Unknown shop
    s, b = req("/proxy/catalog/product/x", make_params("no-such-shop.myshopify.com"))
    test("Unknown shop → 401", s, b, 401)

    # 6. Replay — same nonce twice
    nonce = uuid.uuid4().hex
    p1 = {"shop": TEST_SHOP, "timestamp": str(int(time.time())),
          "nonce": nonce, "path_prefix": "/apps/catalog"}
    p1["signature"] = sign(p1, SHOPIFY_SECRET)
    req("/proxy/catalog/product/x", p1)

    p2 = {"shop": TEST_SHOP, "timestamp": str(int(time.time())),
          "nonce": nonce, "path_prefix": "/apps/catalog"}
    p2["signature"] = sign(p2, SHOPIFY_SECRET)
    s, b = req("/proxy/catalog/product/x", p2)
    test("Replay same nonce → 401", s, b, 401)