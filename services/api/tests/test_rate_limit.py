"""Rate limiter: the per-minute cap follows the anomaly score, 429 once it is exceeded."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.middleware import rate_limit
from app.middleware.rate_limit import RateLimitMiddleware

CAPS = {"normal": 5, "low": 3, "medium": 1}


class StateFromHeaders(BaseHTTPMiddleware):
    """Plays the role of auth + anti-scrape: sets tenant and anomaly score."""

    async def dispatch(self, request, call_next):
        tenant = request.headers.get("x-test-tenant")
        if tenant:
            request.state.tenant_id = tenant
        request.state.anomaly_score = float(request.headers.get("x-test-score", "0"))
        return await call_next(request)


@pytest.fixture
def client(monkeypatch):
    for tier, cap in CAPS.items():
        monkeypatch.setitem(rate_limit.RATE_LIMITS, tier, cap)
    monkeypatch.setattr(settings, "RATE_LIMIT_LOW_SCORE", 0.25)
    monkeypatch.setattr(settings, "RATE_LIMIT_MEDIUM_SCORE", 0.5)

    app = FastAPI()
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(StateFromHeaders)        # added last, runs first

    @app.get("/api/v1/products/{slug}")
    async def product(slug: str):
        return {"slug": slug}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    with TestClient(app) as test_client:
        yield test_client


def get(client, tenant="tenant-1", score=0.0, path="/api/v1/products/some-product"):
    headers = {"x-test-score": str(score)}
    if tenant:
        headers["x-test-tenant"] = tenant
    return client.get(path, headers=headers)


@pytest.mark.parametrize("score, tier", [
    (0.0, "normal"),
    (0.24, "normal"),
    (0.25, "low"),
    (0.49, "low"),
    (0.5, "medium"),
    (0.9, "medium"),
])
def test_cap_depends_on_anomaly_score(client, score, tier):
    response = get(client, score=score)
    assert response.status_code == 200
    assert response.headers["X-RateLimit-Limit"] == str(CAPS[tier])


def test_remaining_counts_down(client):
    remaining = [get(client).headers["X-RateLimit-Remaining"] for _ in range(3)]
    assert remaining == ["4", "3", "2"]


def test_429_after_the_cap_is_exceeded(client):
    for _ in range(CAPS["normal"]):
        assert get(client).status_code == 200

    blocked = get(client)
    assert blocked.status_code == 429
    assert blocked.json()["error"] == "Rate limit exceeded"
    assert 1 <= int(blocked.headers["Retry-After"]) <= 60
    assert blocked.headers["X-RateLimit-Limit"] == str(CAPS["normal"])
    assert blocked.headers["X-RateLimit-Remaining"] == "0"


def test_suspicious_client_is_limited_sooner(client):
    assert get(client, score=0.6).status_code == 200
    blocked = get(client, score=0.6)
    assert blocked.status_code == 429
    assert "Slow down" in blocked.json()["message"]


def test_tenants_have_separate_counters(client):
    for _ in range(CAPS["normal"]):
        get(client, tenant="tenant-1")
    assert get(client, tenant="tenant-1").status_code == 429
    assert get(client, tenant="tenant-2").status_code == 200


def test_request_without_tenant_is_not_counted(client):
    for _ in range(CAPS["normal"] + 3):
        response = get(client, tenant=None)
        assert response.status_code == 200
    assert "X-RateLimit-Limit" not in response.headers


def test_health_is_not_rate_limited(client):
    for _ in range(CAPS["normal"] + 3):
        assert get(client, path="/health").status_code == 200
