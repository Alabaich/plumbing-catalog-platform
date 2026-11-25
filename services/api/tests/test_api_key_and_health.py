"""X-API-Key middleware on the real app, plus the health endpoints."""

import pytest

from app.config import settings

BROWSER = {
    "User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 Safari/605.1.15",
    "Accept-Language": "en-CA,en;q=0.9",
    "Accept-Encoding": "gzip",
}
# A keyed path with no route: 404 means the request got past authentication.
KEYED_PATH = "/api/v1/not-a-route"


def test_missing_key_is_rejected(client):
    response = client.get(KEYED_PATH, headers=BROWSER)
    assert response.status_code == 401
    assert response.json()["message"] == "Missing X-API-Key header"


def test_wrong_key_is_rejected(client):
    response = client.get(KEYED_PATH, headers={**BROWSER, "X-API-Key": "wrong"})
    assert response.status_code == 401
    assert response.json()["message"] == "Invalid API Key"


def test_key_is_not_matched_by_prefix(client):
    response = client.get(KEYED_PATH, headers={**BROWSER, "X-API-Key": "test-api"})
    assert response.status_code == 401


def test_valid_key_passes_authentication(client):
    response = client.get(KEYED_PATH, headers={**BROWSER, "X-API-Key": "test-api-key"})
    assert response.status_code == 404
    assert "X-RateLimit-Limit" in response.headers      # reached the rate limiter as a tenant


def test_key_with_surrounding_whitespace_is_accepted(client):
    response = client.get(KEYED_PATH, headers={**BROWSER, "X-API-Key": " test-api-key "})
    assert response.status_code == 404


def test_backend_key_is_used_when_api_key_is_unset(client, monkeypatch):
    monkeypatch.setattr(settings, "API_KEY", "")
    monkeypatch.setattr(settings, "BACKEND_API_KEY", "backend-key")
    assert client.get(KEYED_PATH, headers={**BROWSER, "X-API-Key": "backend-key"}).status_code == 404
    assert client.get(KEYED_PATH, headers={**BROWSER, "X-API-Key": "test-api-key"}).status_code == 401


def test_server_without_a_configured_key_fails_closed(client, monkeypatch):
    monkeypatch.setattr(settings, "API_KEY", "")
    monkeypatch.setattr(settings, "BACKEND_API_KEY", "")
    response = client.get(KEYED_PATH, headers={**BROWSER, "X-API-Key": "anything"})
    assert response.status_code == 500
    assert response.json()["error"] == "Server misconfiguration"


@pytest.mark.parametrize("path", ["/health", "/ready", "/proxy/catalog/health"])
def test_public_paths_need_no_key(client, path):
    assert client.get(path).status_code == 200


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready(client):
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_health_endpoints_ignore_script_user_agents(client):
    script = {"User-Agent": "curl/8.4.0 headless"}
    assert client.get("/health", headers=script).status_code == 200
    assert client.get("/ready", headers=script).status_code == 200
