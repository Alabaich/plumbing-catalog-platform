"""
routers/proxy/__init__.py — App Proxy router orchestrator.

Registers all sub-routers and exposes a single `router` that main.py mounts
at prefix="/proxy/catalog".

Sub-modules:
    _auth.py       — HMAC, nonce, ProxyContext (shared, no routes)
    _cache.py      — Redis helpers (shared, no routes)
    product.py     — /product/{handle}  +  /product/{handle}/description
    collection.py  — /collection/{vendor}
    variant.py     — /product/{handle}/variant/{sku}        (Phase 2.7, TODO)
    batch.py       — /products?handles=a,b,c                (Phase 2.9, TODO)
    invalidate.py  — /cache/invalidate                      (Phase 3, TODO)
"""

from fastapi import APIRouter

from . import collection, product

router = APIRouter()

# Health — unauthenticated, registered directly here
@router.get("/health")
async def proxy_health():
    """Unauthenticated liveness probe — Shopify can ping this."""
    return {"status": "ok"}


# Sub-routers
router.include_router(product.router)
router.include_router(collection.router)