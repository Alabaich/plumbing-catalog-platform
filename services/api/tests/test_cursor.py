"""Pagination cursor: encrypted, tenant-scoped, expiring."""

import base64
import json

import pytest

from app.config import settings
from app.services.product_svc import build_cursor, decode_cursor


def test_round_trip():
    cursor = build_cursor(offset=48, tenant_id="tenant-1")
    assert decode_cursor(cursor, tenant_id="tenant-1") == 48


def test_cursor_does_not_expose_its_payload():
    cursor = build_cursor(offset=123456, tenant_id="tenant-1")
    assert "123456" not in cursor
    assert "tenant-1" not in cursor
    decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
    assert b"offset" not in decoded and b"tenant-1" not in decoded


def test_two_cursors_for_the_same_page_differ():
    assert build_cursor(24, "tenant-1") != build_cursor(24, "tenant-1")


def test_tampered_cursor_is_rejected():
    cursor = build_cursor(offset=24, tenant_id="tenant-1")
    middle = len(cursor) // 2
    tampered = cursor[:middle] + ("A" if cursor[middle] != "A" else "B") + cursor[middle + 1:]
    with pytest.raises(ValueError, match="invalid or tampered"):
        decode_cursor(tampered, tenant_id="tenant-1")


@pytest.mark.parametrize("forged", [
    "",
    "not-a-cursor",
    base64.urlsafe_b64encode(json.dumps({"offset": 0, "tenant_id": "tenant-1", "exp": 9999999999}).encode()).decode(),
])
def test_forged_cursor_is_rejected(forged):
    with pytest.raises(ValueError, match="invalid or tampered"):
        decode_cursor(forged, tenant_id="tenant-1")


def test_cursor_of_another_tenant_is_rejected():
    cursor = build_cursor(offset=24, tenant_id="tenant-1")
    with pytest.raises(ValueError, match="tenant mismatch"):
        decode_cursor(cursor, tenant_id="tenant-2")


def test_expired_cursor_is_rejected():
    cursor = build_cursor(offset=24, tenant_id="tenant-1", ttl_seconds=-1)
    with pytest.raises(ValueError, match="expired"):
        decode_cursor(cursor, tenant_id="tenant-1")


def test_cursor_from_another_deployment_is_rejected(monkeypatch):
    cursor = build_cursor(offset=24, tenant_id="tenant-1")
    monkeypatch.setattr(settings, "CURSOR_SECRET", "a-different-secret")
    with pytest.raises(ValueError, match="invalid or tampered"):
        decode_cursor(cursor, tenant_id="tenant-1")
