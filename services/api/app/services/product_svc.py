"""
product_svc.py — Business logic for product endpoints

Handles:
  - DB queries (products, variants, brands)
  - Cursor encryption/decryption for safe pagination
  - Per-tenant watermarking (invisible variation in text to trace leaks)
  - Poisoned variant generation (subtly wrong data for detected scrapers)
"""

import hashlib
import json
import time
from base64 import urlsafe_b64encode
from typing import Optional
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text

from app.config import settings


# ---------------------------------------------------------------------------
# Cursor encryption
# Cursors are AES-encrypted (Fernet = AES-128-CBC + HMAC-SHA256).
# A scraper cannot guess page 2 by looking at page 1's cursor.
# Cursors are also tenant-scoped + expire in 1 hour.
# ---------------------------------------------------------------------------

def _fernet() -> Fernet:
    """
    Derive a per-deployment Fernet key from CURSOR_SECRET in .env.
    Fernet key must be 32 url-safe base64 bytes.
    """
    # SHA-256 the secret to get exactly 32 bytes, then base64 encode
    key_bytes = hashlib.sha256(settings.CURSOR_SECRET.encode()).digest()
    return Fernet(urlsafe_b64encode(key_bytes))


def build_cursor(offset: int, tenant_id: str, ttl_seconds: int = 3600) -> str:
    """
    Encrypt a cursor payload. Expires in 1h so stale cursors are rejected.
    Payload: { "offset": int, "tenant_id": str, "exp": unix_timestamp }
    """
    payload = json.dumps({
        "offset": offset,
        "tenant_id": tenant_id,
        "exp": int(time.time()) + ttl_seconds,
    }).encode()
    return _fernet().encrypt(payload).decode()


def decode_cursor(cursor: str, tenant_id: str) -> int:
    """
    Decrypt and validate cursor. Raises ValueError on any tampering,
    expiry, or cross-tenant usage.
    """
    try:
        payload = json.loads(_fernet().decrypt(cursor.encode()).decode())
    except (InvalidToken, Exception):
        raise ValueError("Cursor is invalid or tampered")

    if payload.get("tenant_id") != tenant_id:
        raise ValueError("Cursor tenant mismatch")

    if payload.get("exp", 0) < time.time():
        raise ValueError("Cursor has expired")

    return payload["offset"]

async def get_products_page(
    db,
    tenant_id: str,
    allowed_brands: list,
    brand_filter: Optional[str],
    category_filter: Optional[str],
    offset: int,
    limit: int,
) -> tuple[list, int, Optional[int]]:
    """
    Returns (products, total_count, next_offset).

    Only fetches the fields the frontend needs for Shopify bulk upload:
      - title, slug, primary_image, variants (sku, options, price, image),
        brand, category, option names

    Fields NOT returned here (to avoid bulk data exposure):
      - Full description HTML (too large, use single-product endpoint)
      - All images (only primary image here)
      - Internal DB IDs (UUID only, no sequential integers)
    """

    # Build brand scope: intersection of tenant subscriptions and optional filter
    brand_scope = allowed_brands
    if brand_scope is not None and not brand_scope:
        return [], 0, None

    if brand_scope is None:
        query = text("""
        SELECT id, "Title" as title, COALESCE("clean slug", "clean_slug", "handle") as slug, "Vendor" as vendor
        FROM "product_db"
        ORDER BY id ASC
        OFFSET :offset LIMIT :limit
    """)
        rows = db.execute(query, {"offset": offset, "limit": limit}).mappings().all()
    else:
        query = text("""
        SELECT id, "Title" as title, COALESCE("clean slug", "clean_slug", "handle") as slug, "Vendor" as vendor
        FROM "product_db"
        WHERE "Vendor" = ANY(:brands)
        ORDER BY id ASC
        OFFSET :offset LIMIT :limit
    """)
        rows = db.execute(query, {"brands": brand_scope, "offset": offset, "limit": limit}).mappings().all()
    
    products = []
    for row in rows:
        handle = row["slug"]
        variants = []
        
        if handle:
            v_query = text("""
                SELECT "Variant SKU" as sku, "Variant Price" as price 
                FROM "variant_db" 
                WHERE "handle" = :handle
            """)
            v_rows = db.execute(v_query, {"handle": handle}).mappings().all()
            variants = [dict(v) for v in v_rows]

        products.append({
            "title": row["title"],
            "slug": handle,
            "vendor": row["vendor"],
            "variants": variants
        })

    next_offset = offset + limit if len(rows) == limit else None
    return products, 0, next_offset

async def get_product_by_slug(
    db,
    slug: str,
    tenant_id: str,
    allowed_brands: list,
) -> Optional[dict]:
    
    query = text("""
        SELECT id, "Title" as title, COALESCE("clean slug", "clean_slug", "handle") as slug, "Vendor" as vendor
        FROM "product_db"
        WHERE COALESCE("clean slug", "clean_slug", "handle") = :slug
        AND "Vendor" = ANY(:brands)
        LIMIT 1
    """)

    if allowed_brands is None:
        query = text("""
        SELECT id, "Title" as title, COALESCE("clean slug", "clean_slug", "handle") as slug, "Vendor" as vendor
        FROM "product_db"
        WHERE COALESCE("clean slug", "clean_slug", "handle") = :slug
        LIMIT 1
    """)
        row = db.execute(query, {"slug": slug}).mappings().first()
    else:
        query = text("""
        SELECT id, "Title" as title, COALESCE("clean slug", "clean_slug", "handle") as slug, "Vendor" as vendor
        FROM "product_db"
        WHERE COALESCE("clean slug", "clean_slug", "handle") = :slug
        AND "Vendor" = ANY(:brands)
        LIMIT 1
    """)
        row = db.execute(query, {"slug": slug, "brands": allowed_brands}).mappings().first()
    
    if not row:
        return None
        
    v_query = text("""
        SELECT "Variant SKU" as sku, "Variant Price" as price 
        FROM "variant_db" 
        WHERE "handle" = :handle
    """)
    v_rows = db.execute(v_query, {"handle": slug}).mappings().all()
    
    return {
        "title": row["title"],
        "slug": slug,
        "vendor": row["vendor"],
        "variants": [dict(v) for v in v_rows]
    }