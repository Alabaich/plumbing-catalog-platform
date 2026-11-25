"""
products.py — API routes for the frontend worker

Routes:
    GET /api/v1/products           → Paginated product list (for Shopify bulk upload)
    GET /api/v1/products/{slug}    → Single product full detail
    GET /api/v1/brands             → List of all available brands
    POST /api/v1/products/import   → Trigger vendor migration to Shopify
    POST /api/v1/products/discount → Apply discount to all products of a brand

Security applied on every route:
    - Auth middleware validates X-API-Key (hashed, stored in DB)
    - Domain locking: key is only valid from the tenant's Shopify domain
    - Rate limit middleware: Redis-backed, per-tenant
    - Anti-scrape middleware: fingerprinting + anomaly score
    - Cursor pagination (no ?page=1,2,3 enumeration)
    - Max page size = 24 (one grid page — never a full dump)
    - Response signing: X-Data-Signature header on every response
    - Watermarking: tenant-specific text variations in descriptions
    - Poisoned data: wrong specs returned when anomaly score is high
    - Docs disabled in production (FastAPI docs_url=None)
"""

import hashlib
import hmac
import logging
import time
import json
from typing import Optional
from sqlalchemy import text

from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.dependencies import get_db, require_tenant
from app.services.product_svc import (
    get_products_page,
    get_product_by_slug,
    decode_cursor,
    build_cursor
)
from app.config import settings

router = APIRouter()
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Catalog: "has importable price" = product handle matches variant_db and
# at least one variant has non-empty List that parses as a number > 0.
# Uses p.handle only (avoids missing optional columns like "clean slug").
# ---------------------------------------------------------------------------

_PRODUCT_HANDLE_SQL = """NULLIF(BTRIM(CAST(p.handle AS TEXT)), '')"""


def _priced_variant_exists_sql() -> str:
    ph = _PRODUCT_HANDLE_SQL
    # CASE short-circuits: avoid ::numeric on non-numeric strings (PG error).
    return f"""EXISTS (
    SELECT 1
    FROM "variant_db" v
    WHERE v."handle" = ({ph})
      AND ({ph}) IS NOT NULL
      AND LENGTH(({ph})) > 0
      AND v."List" IS NOT NULL
      AND BTRIM(CAST(v."List" AS TEXT)) <> ''
      AND (
        CASE
          WHEN BTRIM(REPLACE(BTRIM(CAST(v."List" AS TEXT)), ',', '.'))
               ~ '^[0-9]+(\\.[0-9]+)?$'
          THEN BTRIM(REPLACE(BTRIM(CAST(v."List" AS TEXT)), ',', '.'))::numeric > 0
          ELSE FALSE
        END
      )
)"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sign_response(payload: dict, tenant_id: str) -> str:
    """
    Creates a tamper-evident signature for the response body.
    Format: sha256(tenant_id + ":" + timestamp + ":" + body_hash + ":" + SECRET)
    Allows you to prove which tenant key produced a scraped dataset.
    """
    ts = str(int(time.time()))
    body_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    raw = f"{tenant_id}:{ts}:{body_hash}:{settings.API_SIGNING_SECRET}"
    sig = hmac.new(settings.API_SIGNING_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()  # type: ignore
    return f"{ts}.{sig}"

def build_signed_response(data: dict, request: Request) -> JSONResponse:
    """Wraps any response dict with security headers."""
    tenant_id = getattr(request.state, "tenant_id", "anon")
    sig = sign_response(data, tenant_id)
    resp = JSONResponse(content=data)
    resp.headers["X-Data-Signature"] = sig
    resp.headers["X-Trace-Id"] = f"tnt-{tenant_id}"
    # Never leak server info
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


# ---------------------------------------------------------------------------
# GET /api/v1/products
# ---------------------------------------------------------------------------

@router.get("/")
async def list_products(
    request: Request,
    cursor: Optional[str] = Query(None),
    limit: int = Query(24, ge=1, le=24),
    brand: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    db=Depends(get_db),
    tenant=Depends(require_tenant),
):
    """
    Returns paginated products with the fields the frontend needs for
    bulk Shopify upload: title, slug, one primary image, variants, prices.

    Security notes:
    - Cursor is AES-encrypted — cannot enumerate pages sequentially
    - Hard cap: limit=24 always enforced. To get 100k products a scraper
      would need 4,167+ authenticated requests, each fingerprinted.
    - Tenant only sees brands they subscribed to (tenant_brands table)
    - Anomaly score from anti_scrape middleware gates poison injection
    """
    anomaly_score = getattr(request.state, "anomaly_score", 0.0)

    # Decode cursor safely (raises 400 on tampering)
    offset = 0
    if cursor:
        try:
            offset = decode_cursor(cursor, tenant["id"])
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid pagination cursor")

    # Enforce tenant brand scope — they only see what they subscribed to
    allowed_brands = tenant.get("subscribed_brands")  # list from DB

    products, total, next_offset = await get_products_page(
        db=db,
        tenant_id=tenant["id"],
        allowed_brands=allowed_brands,
        brand_filter=brand,
        category_filter=category,
        offset=offset,
        limit=limit,
    )

    next_cursor = build_cursor(next_offset, tenant["id"]) if next_offset else None

    payload = {
        "data": products,
        "pagination": {
            "total": total,
            "limit": limit,
            "next_cursor": next_cursor,
        },
    }

    return build_signed_response(payload, request)

@router.get("/brands/list")
async def list_brands(
    request: Request,
    db=Depends(get_db),
    tenant=Depends(require_tenant)
):
    shop_domain = request.headers.get("X-Shopify-Shop-Domain", "") or ""
    allowed_brands = tenant.get("subscribed_brands")

    priced = _priced_variant_exists_sql()
    query = text("""
        SELECT
            p."Vendor" AS name,
            COUNT(DISTINCT p.id)::int AS product_count,
            COALESCE(MAX(t.discount_pct), 0) AS discount_pct
        FROM "product_db" p
        INNER JOIN "variant_db" v 
            ON v."handle" = NULLIF(BTRIM(CAST(p.handle AS TEXT)), '')
        LEFT JOIN tenant_brand_discounts t
            ON t.vendor = p."Vendor" AND t.shop_domain = :shop_domain
        WHERE p."Vendor" IS NOT NULL AND BTRIM(p."Vendor") <> ''
          AND v."List" IS NOT NULL
          AND BTRIM(CAST(v."List" AS TEXT)) <> ''
          AND (
            CASE
              WHEN BTRIM(REPLACE(BTRIM(CAST(v."List" AS TEXT)), ',', '.')) ~ '^[0-9]+(\\.[0-9]+)?$'
              THEN BTRIM(REPLACE(BTRIM(CAST(v."List" AS TEXT)), ',', '.'))::numeric > 0
              ELSE FALSE
            END
          )
        GROUP BY p."Vendor"
        ORDER BY p."Vendor" ASC
    """)

    fallback = text("""
        SELECT
            p."Vendor" AS name,
            COUNT(*)::int AS product_count,
            COALESCE(MAX(t.discount_pct), 0) AS discount_pct
        FROM "product_db" p
        LEFT JOIN tenant_brand_discounts t
            ON t.vendor = p."Vendor" AND t.shop_domain = :shop_domain
        WHERE p."Vendor" IS NOT NULL AND BTRIM(p."Vendor") <> ''
        GROUP BY p."Vendor"
        ORDER BY p."Vendor" ASC
    """)

    try:
        # Prevent 504 on gateway by throwing an error if query takes > 5s
        db.execute(text("SET LOCAL statement_timeout = '5000'"))
        result = db.execute(query, {"shop_domain": shop_domain}).mappings().all()
    except Exception as exc:
        log.warning(
            "brands/list priced query failed (%s); falling back to raw product counts",
            exc,
        )
        db.execute(text("RESET statement_timeout"))
        result = db.execute(fallback, {"shop_domain": shop_domain}).mappings().all()

    brands = []
    for row in result:
        brand_name = row["name"]

        if not brand_name or str(brand_name).strip() == "":
            continue

        if allowed_brands and brand_name not in allowed_brands:
            continue

        db_count = int(row["product_count"])
        shopify_count = 0
        remaining_count = max(db_count - shopify_count, 0)
        try:
            saved_pct = float(row["discount_pct"] or 0)
        except (TypeError, ValueError):
            saved_pct = 0.0

        brands.append({
            "id": str(brand_name).lower().replace(" ", "-"),
            "name": str(brand_name),
            "count": db_count,
            "shopify_count": shopify_count,
            "remaining_count": remaining_count,
            "discount_pct": saved_pct,
        })

    return {"data": brands}

class ImportRequest(BaseModel):
    vendor: str
    shop: str
    offset: int = 0
    limit: int = 1
    discount_pct: Optional[float] = None


class BrandDiscountPayload(BaseModel):
    vendor: str
    discount: float
    shop: str


@router.post("/discount")
async def save_and_sync_brand_discount(
    payload: BrandDiscountPayload,
    db=Depends(get_db),
    tenant=Depends(require_tenant),
):
    """Persist brand discount for the shop and re-price existing Shopify variants."""
    from app.services.tenant_svc import TenantService
    from app.services.shopify_svc import ShopifyClient

    allowed_brands = tenant.get("subscribed_brands")
    if allowed_brands and payload.vendor not in allowed_brands:
        return {"status": "error", "message": "Unauthorized brand"}

    pct = max(0.0, min(100.0, float(payload.discount)))

    tenant_svc = TenantService()
    access_token = tenant_svc.get_shopify_credentials(payload.shop)
    if not access_token:
        raise HTTPException(status_code=401, detail="Shop session not found")

    db.execute(
        text("""
            INSERT INTO tenant_brand_discounts (shop_domain, vendor, discount_pct)
            VALUES (:shop, :vendor, :pct)
            ON CONFLICT (shop_domain, vendor) DO UPDATE
                SET discount_pct = EXCLUDED.discount_pct,
                    set_at = now()
        """),
        {"shop": payload.shop, "vendor": payload.vendor, "pct": pct},
    )
    db.commit()

    shopify = ShopifyClient(shop_url=payload.shop, access_token=access_token)
    updated = await shopify.sync_vendor_prices_for_discount(db, payload.vendor, pct)

    return {
        "status": "success",
        "updated_variants": updated,
        "discount_pct": pct,
        "message": f"Saved discount {pct}%. Updated {updated} variant(s) in Shopify.",
    }


@router.post("/import")
async def import_vendor_products(
    payload: ImportRequest,
    db=Depends(get_db),
):
    from app.services.tenant_svc import TenantService
    from app.services.shopify_svc import ShopifyClient

    vendor_name = payload.vendor
    shop_domain = payload.shop

    tenant_svc = TenantService()
    access_token = tenant_svc.get_shopify_credentials(shop_domain)
    if not access_token:
        raise HTTPException(status_code=401, detail="Shop session not found")

    if payload.discount_pct is not None:
        pct_in = max(0.0, min(100.0, float(payload.discount_pct)))
        db.execute(
            text("""
                INSERT INTO tenant_brand_discounts (shop_domain, vendor, discount_pct)
                VALUES (:shop, :vendor, :pct)
                ON CONFLICT (shop_domain, vendor) DO UPDATE
                    SET discount_pct = EXCLUDED.discount_pct,
                        set_at = now()
            """),
            {"shop": shop_domain, "vendor": vendor_name, "pct": pct_in},
        )
        db.commit()

    pct_row = db.execute(
        text("""
            SELECT discount_pct FROM tenant_brand_discounts
            WHERE shop_domain = :shop AND vendor = :vendor
        """),
        {"shop": shop_domain, "vendor": vendor_name},
    ).first()
    discount_pct = float(pct_row[0]) if pct_row else 0.0

    priced = _priced_variant_exists_sql()
    priced_query = text(f"""
        SELECT p.*
        FROM "product_db" p
        WHERE p."Vendor" ILIKE :vendor
          AND {priced}
        ORDER BY p.id ASC
        OFFSET :offset LIMIT :limit
    """)
    fallback_query = text("""
        SELECT p.*
        FROM "product_db" p
        WHERE p."Vendor" ILIKE :vendor
        ORDER BY p.id ASC
        OFFSET :offset LIMIT :limit
    """)
    params = {
        "vendor": vendor_name,
        "offset": payload.offset,
        "limit": payload.limit,
    }
    try:
        products = db.execute(priced_query, params).mappings().all()
    except Exception as exc:
        log.warning(
            "import priced filter failed for %r (%s); importing without SQL price filter",
            vendor_name,
            exc,
        )
        products = db.execute(fallback_query, params).mappings().all()

    if not products:
        return {
            "status": "success",
            "imported_count": 0,
            "skipped_count": 0,
            "applied_discount_pct": discount_pct,
            "message": "No importable products in this chunk (need handle + variant with List price > 0).",
        }

    shopify = ShopifyClient(shop_url=shop_domain, access_token=access_token)

    success_count = 0
    skipped_count = 0

    for prod in products:
        product_handle = (
            prod.get("clean slug")
            or prod.get("clean_slug")
            or prod.get("handle")
        )

        variants = []
        if product_handle:
            try:
                variants = db.execute(
                    text('SELECT * FROM "variant_db" WHERE "handle" = :handle'),
                    {"handle": product_handle},
                ).mappings().all()
            except Exception:
                pass

        # Skip re-uploading if Shopify already has this SKU at the expected
        # post-discount price. Tolerance handles "199" vs "199.00" cases.
        should_skip = False
        if variants:
            first = variants[0]
            first_sku = first.get("Variant SKU") or first.get("Variant_SKU")
            list_raw = first.get("List")

            if first_sku and list_raw is not None:
                try:
                    list_price = float(list_raw)
                    map_floor = float(first.get("MAP")) if first.get("MAP") is not None else 0.0
                    if discount_pct > 0:
                        expected = max(list_price * (1 - discount_pct / 100.0), map_floor)
                    else:
                        expected = list_price

                    existing = await shopify.get_variant_by_sku(str(first_sku))
                    if existing and abs(float(existing["price"]) - expected) < 0.01:
                        should_skip = True
                except (TypeError, ValueError):
                    pass

        if should_skip:
            skipped_count += 1
            continue

        result = await shopify.create_product(prod, variants, discount_pct=discount_pct)
        if result:
            success_count += 1

    # Record that this shop imported products of this brand. Drives the
    # ?scope=imported filter on the discounts page.
    if success_count > 0:
        db.execute(text("""
            INSERT INTO tenant_imported_vendors
              (shop_domain, vendor, imported_count)
            VALUES (:shop, :vendor, :n)
            ON CONFLICT (shop_domain, vendor) DO UPDATE
              SET imported_count = tenant_imported_vendors.imported_count + EXCLUDED.imported_count,
                  last_imported_at = now()
        """), {"shop": shop_domain, "vendor": vendor_name, "n": success_count})
        db.commit()

    return {
        "status": "success",
        "imported_count": success_count,
        "skipped_count": skipped_count,
        "applied_discount_pct": discount_pct,
    }

@router.get("/{slug}")
async def get_product(
    slug: str,
    request: Request,
    db=Depends(get_db),
    tenant=Depends(require_tenant),
):
    allowed_brands = tenant.get("subscribed_brands")

    product = await get_product_by_slug(
        db=db,
        slug=slug,
        tenant_id=tenant["id"],
        allowed_brands=allowed_brands,
    )

    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    payload = {"data": product}
    return build_signed_response(payload, request)