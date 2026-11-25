"""Anti-scrape middleware: anomaly score from request headers and the block threshold."""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.middleware.anti_scrape import AntiScrapeMiddleware

# Distinct weights so every sum identifies which signals fired.
WEIGHTS = {
    "ANTI_SCRAPE_WEIGHT_SCRIPT_UA": 0.4,
    "ANTI_SCRAPE_WEIGHT_HEADLESS_UA": 0.7,
    "ANTI_SCRAPE_WEIGHT_NO_REFERER": 0.2,
    "ANTI_SCRAPE_WEIGHT_FOREIGN_REFERER": 0.6,
    "ANTI_SCRAPE_WEIGHT_MISSING_HEADERS": 0.1,
    "ANTI_SCRAPE_BLOCK_SCORE": 1.0,
}

BROWSER = {
    "User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 Safari/605.1.15",
    "Accept-Language": "en-CA,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}
SHOP = "example-store.myshopify.com"


class StateFromHeaders(BaseHTTPMiddleware):
    """Plays the role of the auth middleware: puts tenant and shop on request.state."""

    async def dispatch(self, request, call_next):
        request.state.tenant_id = request.headers.get("x-test-tenant", "tenant-1")
        shop = request.headers.get("x-test-shop")
        if shop:
            request.state.shop_domain = shop
        return await call_next(request)


@pytest.fixture
def client(monkeypatch):
    for name, value in WEIGHTS.items():
        monkeypatch.setattr(settings, name, value)

    app = FastAPI()
    app.add_middleware(AntiScrapeMiddleware)
    app.add_middleware(StateFromHeaders)        # added last, runs first

    @app.get("/score")
    async def score(request: Request):
        return {"score": request.state.anomaly_score}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/proxy/ping")
    async def proxy_ping():
        return {"status": "ok"}

    return TestClient(app)


def score_of(client, headers):
    response = client.get("/score", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["score"]


def test_browser_request_scores_zero(client):
    assert score_of(client, BROWSER) == 0


@pytest.mark.parametrize("user_agent", ["python-requests/2.32", "curl/8.4.0", "PostmanRuntime/7.36", ""])
def test_script_user_agent_adds_its_weight(client, user_agent):
    assert score_of(client, {**BROWSER, "User-Agent": user_agent}) == pytest.approx(0.4)


@pytest.mark.parametrize("user_agent", ["Mozilla/5.0 HeadlessChrome/120.0", "puppeteer-core"])
def test_headless_user_agent_adds_its_weight(client, user_agent):
    assert score_of(client, {**BROWSER, "User-Agent": user_agent}) == pytest.approx(0.7)


def test_missing_browser_headers_add_their_weight(client):
    without_language = {k: v for k, v in BROWSER.items() if k != "Accept-Language"}
    assert score_of(client, without_language) == pytest.approx(0.1)

    # httpx always sends Accept-Encoding; an empty value counts as missing.
    assert score_of(client, {**BROWSER, "Accept-Encoding": ""}) == pytest.approx(0.1)


def test_referer_is_checked_only_when_the_shop_is_known(client):
    # No shop on the request: the Referer is not scored at all.
    assert score_of(client, BROWSER) == 0

    known_shop = {**BROWSER, "x-test-shop": SHOP}
    assert score_of(client, known_shop) == pytest.approx(0.2)                                      # no Referer
    assert score_of(client, {**known_shop, "Referer": f"https://{SHOP}/products/x"}) == 0          # own storefront
    assert score_of(client, {**known_shop, "Referer": "https://elsewhere.example.com/"}) == pytest.approx(0.6)


def test_signals_add_up(client):
    headers = {"User-Agent": "curl/8.4.0", "Accept-Encoding": "gzip", "x-test-shop": SHOP}
    # script UA 0.4 + no Referer 0.2 + missing Accept-Language 0.1
    assert score_of(client, headers) == pytest.approx(0.7)


def test_request_is_blocked_at_the_threshold(client):
    headers = {
        "User-Agent": "python-requests/2.32 headless",      # 0.4 + 0.7
        "Accept-Language": "en",
        "Accept-Encoding": "gzip",
    }
    response = client.get("/score", headers=headers)
    assert response.status_code == 403
    assert response.json()["error"] == "Access Denied"


def test_score_just_below_the_threshold_passes(client, monkeypatch):
    monkeypatch.setattr(settings, "ANTI_SCRAPE_BLOCK_SCORE", 1.2)
    headers = {**BROWSER, "User-Agent": "python-requests/2.32 headless"}   # 1.1
    assert score_of(client, headers) == pytest.approx(1.1)

    monkeypatch.setattr(settings, "ANTI_SCRAPE_BLOCK_SCORE", 1.1)
    assert client.get("/score", headers=headers).status_code == 403


def test_health_and_proxy_paths_are_not_scored(client):
    script = {"User-Agent": "python-requests/2.32 headless"}
    assert client.get("/health", headers=script).status_code == 200
    assert client.get("/proxy/ping", headers=script).status_code == 200


def test_response_carries_signature_and_trace_headers(client):
    response = client.get("/score", headers={**BROWSER, "x-test-tenant": "tenant-42"})
    timestamp, _, digest = response.headers["X-Data-Signature"].partition(".")
    assert timestamp.isdigit()
    assert len(digest) == 64
    assert response.headers["X-Trace-Id"] == "tnt-tenant-42"
