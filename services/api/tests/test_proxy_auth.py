"""Shopify App Proxy: signature, timestamp, nonce, tenant and brand scope."""

import hashlib
import hmac
import time
import uuid

import pytest
from fastapi import HTTPException
from starlette.datastructures import QueryParams

from app.routers.proxy._auth import _verify_hmac

SECRET = "test-shopify-secret"
SHOP = "example-store.myshopify.com"


def sign(params: dict, secret: str = SECRET) -> str:
    message = "".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def signed_params(shop: str = SHOP, **extra) -> dict:
    params = {
        "shop": shop,
        "timestamp": str(int(time.time())),
        "nonce": uuid.uuid4().hex,
        "path_prefix": "/apps/catalog",
    }
    params.update(extra)
    params["signature"] = sign(params)
    return params


COLLECTION = "/proxy/catalog/collection/ACME"


# ---------------------------------------------------------------------------
# Signature
# ---------------------------------------------------------------------------

def test_valid_signature_is_accepted(client):
    response = client.get(COLLECTION, params=signed_params())
    assert response.status_code == 200
    body = response.json()
    assert body["vendor"] == "ACME"
    assert [p["handle"] for p in body["products"]] == ["acme-faucet-1"]


def test_missing_signature_is_rejected(client):
    params = signed_params()
    del params["signature"]
    response = client.get(COLLECTION, params=params)
    assert response.status_code == 401
    assert response.json()["detail"] == "Missing signature"


def test_changed_parameter_invalidates_signature(client):
    params = signed_params()
    params["shop"] = "attacker-store.myshopify.com"
    response = client.get(COLLECTION, params=params)
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid signature"


def test_added_parameter_invalidates_signature(client):
    params = signed_params()
    params["page"] = "2"
    assert client.get(COLLECTION, params=params).status_code == 401


def test_signature_with_wrong_secret_is_rejected(client):
    params = signed_params()
    unsigned = {k: v for k, v in params.items() if k != "signature"}
    params["signature"] = sign(unsigned, secret="wrong-secret")
    assert client.get(COLLECTION, params=params).status_code == 401


def test_expired_timestamp_is_rejected(client):
    params = signed_params(timestamp=str(int(time.time()) - 3600))
    response = client.get(COLLECTION, params=params)
    assert response.status_code == 401
    assert response.json()["detail"] == "Signature expired"


def test_timestamp_from_the_future_is_rejected(client):
    params = signed_params(timestamp=str(int(time.time()) + 3600))
    assert client.get(COLLECTION, params=params).status_code == 401


def test_non_numeric_timestamp_is_rejected(client):
    params = signed_params(timestamp="yesterday")
    response = client.get(COLLECTION, params=params)
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid timestamp"


def test_verify_hmac_joins_repeated_parameters_with_commas():
    # Shopify signs repeated keys as key=a,b
    now = str(int(time.time()))
    message = f"ids=1,2shop={SHOP}timestamp={now}"
    signature = hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
    params = QueryParams(f"ids=1&ids=2&shop={SHOP}&timestamp={now}&signature={signature}")

    _verify_hmac(params, SECRET, ttl=60)   # does not raise

    reordered = QueryParams(f"ids=2&ids=1&shop={SHOP}&timestamp={now}&signature={signature}")
    with pytest.raises(HTTPException) as error:
        _verify_hmac(reordered, SECRET, ttl=60)
    assert error.value.status_code == 401


# ---------------------------------------------------------------------------
# Nonce
# ---------------------------------------------------------------------------

def test_replayed_nonce_is_rejected(client):
    params = signed_params()
    assert client.get(COLLECTION, params=params).status_code == 200

    replay = client.get(COLLECTION, params=params)
    assert replay.status_code == 401
    assert replay.json()["detail"] == "Replayed request"


def test_missing_nonce_is_rejected(client):
    params = {"shop": SHOP, "timestamp": str(int(time.time()))}
    params["signature"] = sign(params)
    response = client.get(COLLECTION, params=params)
    assert response.status_code == 401
    assert response.json()["detail"] == "Missing nonce"


def test_fresh_nonce_each_time_is_accepted(client):
    assert client.get(COLLECTION, params=signed_params()).status_code == 200
    assert client.get(COLLECTION, params=signed_params()).status_code == 200


# ---------------------------------------------------------------------------
# Tenant
# ---------------------------------------------------------------------------

def test_unknown_shop_is_rejected(client):
    response = client.get(COLLECTION, params=signed_params(shop="no-such-store.myshopify.com"))
    assert response.status_code == 401
    assert response.json()["detail"] == "Unknown shop"


def test_inactive_shop_is_forbidden(client, db):
    db.tenants[SHOP]["is_active"] = False
    response = client.get(COLLECTION, params=signed_params())
    assert response.status_code == 403
    assert response.json()["detail"] == "Shop is not active"


# ---------------------------------------------------------------------------
# Brand scope
# ---------------------------------------------------------------------------

def test_tenant_without_subscriptions_sees_every_brand(client):
    assert client.get("/proxy/catalog/collection/ACME", params=signed_params()).status_code == 200
    assert client.get("/proxy/catalog/collection/OTHER", params=signed_params()).status_code == 200


def test_subscribed_tenant_sees_only_its_brands(client, db):
    db.subscriptions[SHOP] = ["ACME"]

    allowed = client.get("/proxy/catalog/collection/ACME", params=signed_params())
    assert allowed.status_code == 200

    denied = client.get("/proxy/catalog/collection/OTHER", params=signed_params())
    assert denied.status_code == 403
    assert denied.json()["detail"] == "Vendor not in your brand scope"


def test_product_endpoint_enforces_brand_scope(client, db):
    db.subscriptions[SHOP] = ["ACME"]
    response = client.get("/proxy/catalog/product/other-sink-1", params=signed_params())
    assert response.status_code == 403
    assert response.json()["detail"] == "Product not in your brand scope"


def test_unknown_product_is_404(client):
    response = client.get("/proxy/catalog/product/missing", params=signed_params())
    assert response.status_code == 404


def test_collection_response_is_cached(client):
    first = client.get(COLLECTION, params=signed_params())
    second = client.get(COLLECTION, params=signed_params())
    assert first.headers["X-Cache"] == "MISS"
    assert second.headers["X-Cache"] == "HIT"
    assert second.json() == first.json()


def test_proxy_health_needs_no_signature(client):
    response = client.get("/proxy/catalog/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
