"""
collection.py — Collection endpoint for the App Proxy.

    GET /proxy/catalog/collection/{vendor}   Phase 2.8
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from ...dependencies import get_db
from ._auth import ProxyContext, get_proxy_context
from ._cache import cache_get, cache_set

router = APIRouter()


@router.get("/collection/{vendor}")
async def proxy_collection(
    vendor: str,
    request: Request,
    page: int = 1,
    per_page: int = 24,
    ctx: ProxyContext = Depends(get_proxy_context),
    db: Session = Depends(get_db),
):
    """
    Paginated product list for a vendor/brand.

    Query params:
        page     — 1-based (default 1)
        per_page — max 24 (default 24)

    Cache key: proxy:collection:{vendor}:p{page}  TTL 1h
    """
    per_page = min(per_page, 24)
    page = max(page, 1)
    offset = (page - 1) * per_page

    if ctx.allowed_vendors and vendor not in ctx.allowed_vendors:
        raise HTTPException(status_code=403, detail="Vendor not in your brand scope")

    cache_key = f"proxy:collection:{vendor}:p{page}"
    cached = await cache_get(cache_key)
    if cached is not None:
        return JSONResponse(
            content=cached,
            headers={"X-Cache": "HIT", "Cache-Control": "public, max-age=3600"},
        )

    count_row = db.execute(
        text(
            'SELECT COUNT(*) as total FROM "product_db" '
            'WHERE "Vendor" = :vendor AND handle_shopify IS NOT NULL'
        ),
        {"vendor": vendor},
    ).mappings().first()

    total = count_row["total"] if count_row else 0
    if total == 0:
        raise HTTPException(status_code=404, detail="Vendor not found or has no products")

    rows = db.execute(
        text(
            'SELECT '
            '  handle_shopify as handle, '
            '  "Title" as title, '
            '  "Vendor" as vendor, '
            '  all_image_srcs[1] as primary_image '
            'FROM "product_db" '
            'WHERE "Vendor" = :vendor AND handle_shopify IS NOT NULL '
            'ORDER BY id ASC '
            'LIMIT :limit OFFSET :offset'
        ),
        {"vendor": vendor, "limit": per_page, "offset": offset},
    ).mappings().all()

    payload = {
        "vendor":   vendor,
        "page":     page,
        "per_page": per_page,
        "total":    total,
        "has_next": (offset + per_page) < total,
        "products": [
            {
                "handle":        r["handle"],
                "title":         r["title"],
                "vendor":        r["vendor"],
                "primary_image": r["primary_image"],
            }
            for r in rows
        ],
    }

    await cache_set(cache_key, payload)
    return JSONResponse(
        content=payload,
        headers={"X-Cache": "MISS", "Cache-Control": "public, max-age=3600"},
    )