"""
product.py — Product endpoints for the App Proxy.

    GET /proxy/catalog/product/{handle}              Phase 2.5
    GET /proxy/catalog/product/{handle}/description  Phase 2.6
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from ...dependencies import get_db
from ._auth import ProxyContext, get_proxy_context
from ._cache import cache_get, cache_set

router = APIRouter()


async def _resolve_vendor(handle: str, db: Session) -> str | None:
    """
    Fetch vendor for handle from DB (or None if not found).
    Caller decides whether to cache the result.
    """
    row = db.execute(
        text(
            'SELECT "Vendor" as vendor '
            'FROM "product_db" '
            'WHERE handle_shopify = :h '
            'LIMIT 1'
        ),
        {"h": handle},
    ).mappings().first()
    return row["vendor"] if row else None


async def _get_vendor(handle: str, db: Session) -> str:
    """
    Returns vendor for handle — from cache if available, DB otherwise.
    Raises 404 if handle not found.
    """
    vendor_key = f"proxy:vendor:{handle}"
    vendor = await cache_get(vendor_key)
    if vendor is None:
        vendor = await _resolve_vendor(handle, db)
        if vendor is None:
            raise HTTPException(status_code=404, detail="Product not found")
        await cache_set(vendor_key, vendor)
    return vendor


def _check_brand_scope(ctx: ProxyContext, vendor: str) -> None:
    if ctx.allowed_vendors and vendor not in ctx.allowed_vendors:
        raise HTTPException(status_code=403, detail="Product not in your brand scope")


# ---------------------------------------------------------------------------
# 2.5  GET /product/{handle}
# ---------------------------------------------------------------------------

@router.get("/product/{handle}")
async def proxy_product(
    handle: str,
    request: Request,
    ctx: ProxyContext = Depends(get_proxy_context),
    db: Session = Depends(get_db),
):
    """
    Minimal product payload for first-paint + SEO.
    Title/vendor are in the Shopify Liquid shell — not duplicated here.
    """
    vendor = await _get_vendor(handle, db)
    _check_brand_scope(ctx, vendor)

    cache_key = f"proxy:product:{handle}"
    cached = await cache_get(cache_key)
    if cached is not None:
        return JSONResponse(
            content=cached,
            headers={"X-Cache": "HIT", "Cache-Control": "public, max-age=3600"},
        )

    product_row = db.execute(
        text(
            'SELECT '
            '  LEFT("Body (HTML)", 600) as short_description, '
            '  all_image_srcs[1] as primary_image '
            'FROM "product_db" '
            'WHERE handle_shopify = :h '
            'LIMIT 1'
        ),
        {"h": handle},
    ).mappings().first()

    if product_row is None:
        raise HTTPException(status_code=404, detail="Product not found")

    variant_rows = db.execute(
        text(
            'SELECT '
            '  TRIM("Variant SKU") as sku, '
            '  "Option1 Value" as option1, '
            '  "Option2 Value" as option2, '
            '  "Option3 Value" as option3, '
            '  "List" as list_price, '
            '  "MAP" as map_price, '
            '  "variant_image_src" as thumbnail_url '
            'FROM "variant_db" '
            'WHERE "handle_shopify" = :h '
            'ORDER BY "Variant SKU" ASC'
        ),
        {"h": handle},
    ).mappings().all()

    variants = [
        {
            "sku":           v.get("sku"),
            "options":       [o for o in [v.get("option1"), v.get("option2"), v.get("option3")] if o],
            "list_price":    str(v["list_price"]) if v.get("list_price") else None,
            "map_price":     str(v["map_price"]) if v.get("map_price") else None,
            "thumbnail_url": v.get("thumbnail_url"),
        }
        for v in variant_rows
    ]

    payload = {
        "handle":            handle,
        "short_description": product_row.get("short_description"),
        "primary_image":     product_row.get("primary_image"),
        "variants":          variants,
    }

    await cache_set(cache_key, payload)
    return JSONResponse(
        content=payload,
        headers={"X-Cache": "MISS", "Cache-Control": "public, max-age=3600"},
    )


# ---------------------------------------------------------------------------
# 2.6  GET /product/{handle}/description
# ---------------------------------------------------------------------------

@router.get("/product/{handle}/description")
async def proxy_product_description(
    handle: str,
    request: Request,
    ctx: ProxyContext = Depends(get_proxy_context),
    db: Session = Depends(get_db),
):
    """
    Full HTML description — lazy-loaded when customer clicks "Read more".
    Kept separate from 2.5 to keep first-paint payload small.
    """
    vendor = await _get_vendor(handle, db)
    _check_brand_scope(ctx, vendor)

    cache_key = f"proxy:desc:{handle}"
    cached = await cache_get(cache_key)
    if cached is not None:
        return JSONResponse(
            content=cached,
            headers={"X-Cache": "HIT", "Cache-Control": "public, max-age=3600"},
        )

    row = db.execute(
        text(
            # Published LLM enrichment wins over the raw scraped body.
            # Rows held as 'needs_review' are never served.
            'SELECT '
            '  COALESCE(e.html_description, p."Body (HTML)") as description, '
            '  e.html_specs_table as specs_table '
            'FROM "product_db" p '
            'LEFT JOIN product_enrichment e '
            "  ON e.handle = p.handle AND e.status = 'published' "
            'WHERE p.handle_shopify = :h '
            'LIMIT 1'
        ),
        {"h": handle},
    ).mappings().first()

    if row is None:
        raise HTTPException(status_code=404, detail="Product not found")

    # Final assembly: description followed by the specification table.
    description = row.get("description")
    specs_table = row.get("specs_table")
    if description and specs_table:
        description += f"<h3>Technical Specifications</h3>{specs_table}"

    payload = {"handle": handle, "description": description}

    await cache_set(cache_key, payload)
    return JSONResponse(
        content=payload,
        headers={"X-Cache": "MISS", "Cache-Control": "public, max-age=3600"},
    )