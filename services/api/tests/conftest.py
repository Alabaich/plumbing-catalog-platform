"""
Shared fixtures for the API tests.

Nothing here touches the network: Redis is replaced by fakeredis and the
database session by a small fake that answers the queries the proxy issues.
The environment is set before `app` is imported because app/config.py reads
it at import time.
"""

import os
import sys
from pathlib import Path

TEST_ENV = {
    "DATABASE_URL": "postgresql://test:test@localhost:5432/test",   # engine is created, never connected
    "REDIS_URL": "redis://localhost:6379/0",
    "API_ENV": "development",
    "API_KEY": "test-api-key",
    "BACKEND_API_KEY": "test-api-key",
    "API_SIGNING_SECRET": "test-signing-secret",
    "CURSOR_SECRET": "test-cursor-secret",
    "SHOPIFY_API_SECRET": "test-shopify-secret",
    "TYPESENSE_API_KEY": "test-typesense-key",
    "PROXY_TIMESTAMP_TTL": "60",
    "PROXY_NONCE_TTL": "120",
}
os.environ.update(TEST_ENV)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fakeredis
import pytest
import redis.asyncio as aioredis
from fastapi.testclient import TestClient

from app.dependencies import get_db
from app.main import app


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    """Every redis.asyncio.from_url() call returns a client on one in-memory server."""
    server = fakeredis.FakeServer()

    def from_url(url, **kwargs):
        return fakeredis.FakeAsyncRedis(
            server=server, decode_responses=kwargs.get("decode_responses", False))

    monkeypatch.setattr(aioredis, "from_url", from_url)
    return server


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def mappings(self):
        return self

    def first(self):
        return self.fetchone()

    def all(self):
        return self.fetchall()


class Row(dict):
    """Row usable both as a mapping (row["x"]) and by attribute (row.x)."""
    __getattr__ = dict.__getitem__


class FakeDB:
    """
    Stand-in for a SQLAlchemy session. Holds tenants, brand subscriptions and
    products in plain dicts and answers by looking at the table named in the SQL.
    """

    def __init__(self):
        self.tenants = {}          # shop_domain -> {"id": ..., "is_active": bool}
        self.subscriptions = {}    # shop_domain -> [vendor, ...]
        self.products = []         # {"handle", "title", "vendor", "primary_image"}

    def execute(self, statement, params=None):
        sql = str(statement)
        params = params or {}

        if "FROM tenants" in sql:
            tenant = self.tenants.get(params["shop"])
            return FakeResult([Row(tenant)] if tenant else [])

        if "tenant_subscribed_brands" in sql:
            return FakeResult([Row(vendor=v) for v in self.subscriptions.get(params["shop"], [])])

        if "COUNT(*)" in sql:
            total = sum(1 for p in self.products if p["vendor"] == params["vendor"])
            return FakeResult([Row(total=total)])

        if '"Vendor" as vendor' in sql and "handle_shopify = :h" in sql:
            match = [Row(vendor=p["vendor"]) for p in self.products if p["handle"] == params["h"]]
            return FakeResult(match)

        if "LIMIT :limit OFFSET :offset" in sql:
            rows = [Row(p) for p in self.products if p["vendor"] == params["vendor"]]
            return FakeResult(rows[params["offset"]:params["offset"] + params["limit"]])

        raise AssertionError(f"FakeDB got an unexpected query: {sql}")

    def close(self):
        pass


@pytest.fixture
def db():
    fake = FakeDB()
    fake.tenants["example-store.myshopify.com"] = {"id": "tenant-1", "is_active": True}
    fake.products = [
        {"handle": "acme-faucet-1", "title": "Acme Faucet", "vendor": "ACME", "primary_image": None},
        {"handle": "other-sink-1", "title": "Other Sink", "vendor": "OTHER", "primary_image": None},
    ]
    return fake


@pytest.fixture
def client(db):
    """TestClient on the real app with the DB dependency replaced."""
    app.dependency_overrides[get_db] = lambda: db
    # Middleware instances cache a Redis client bound to an event loop;
    # rebuild the stack so each test starts with fresh instances.
    app.middleware_stack = None
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
