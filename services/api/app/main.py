"""
main.py — FastAPI entry point

Middleware order (added bottom-up, executed top-down on request):
  1. AuthMiddleware        → validate X-API-Key, set tenant on request.state
  2. AntiScrapeMiddleware  → fingerprint + anomaly score
  3. RateLimitMiddleware   → per-tenant Redis throttle using anomaly score
  → Router handlers        → business logic + poisoning + watermarking

Docs disabled in production so scrapers can't map the API structure.
"""

import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.middleware.auth import AuthMiddleware
from app.middleware.anti_scrape import AntiScrapeMiddleware
from app.middleware.rate_limit import RateLimitMiddleware

from app.routers import products, storefront, proxy
from app.dependencies import get_db


IS_PROD = os.getenv("API_ENV", "development") == "production"

app = FastAPI(
    title="Plumbing Catalog API",
    version="1.0.0",
    docs_url=None if IS_PROD else "/docs",
    redoc_url=None,
    openapi_url=None if IS_PROD else "/openapi.json",
)

@app.on_event("startup")
async def startup_event():
    print("Plumbing Catalog API started")

@app.on_event("shutdown")
async def shutdown_event():
    print("Plumbing Catalog API shutting down")

CORS_ORIGINS = os.getenv(
    "CORS_ORIGINS",
    "https://*.myshopify.com"
).split(",")

# Extend with your own domain via env, e.g. https://[a-z0-9\-]+\.(myshopify\.com|example\.com)
cors_regex = os.getenv("CORS_ORIGIN_REGEX", r"https://[a-zA-Z0-9\-]+\.myshopify\.com")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_origin_regex=cors_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Security middleware stack (order matters — last added = first executed)
# ---------------------------------------------------------------------------
app.add_middleware(RateLimitMiddleware)     # 3rd — uses anomaly score from #2
app.add_middleware(AntiScrapeMiddleware)    # 2nd — fingerprints request
app.add_middleware(AuthMiddleware)          # 1st — validates X-API-Key, sets tenant

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(
    products.router,
    prefix="/api/v1/products",
    tags=["Products"],
)

app.include_router(
    storefront.router,
    prefix="/api/v1/storefront",
    tags=["Storefront"],
)

app.include_router(proxy.router, prefix="/proxy/catalog", tags=["Proxy"])

# Brands is a sub-route on the products router (GET /api/v1/products/brands/list)
# No separate /brands prefix to keep URL surface minimal


# ---------------------------------------------------------------------------
# Health checks (excluded from auth + rate limit)
# ---------------------------------------------------------------------------
@app.get("/health", tags=["System"], include_in_schema=False)
async def health():
    return {"status": "ok"}


@app.get("/ready", tags=["System"], include_in_schema=False)
async def ready():
    # Add DB + Redis ping here in production
    return {"status": "ready"}