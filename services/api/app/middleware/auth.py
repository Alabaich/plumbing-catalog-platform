import hashlib
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from app.config import settings


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Public endpoints — no X-API-Key required.
        # /proxy/catalog is Shopify App Proxy traffic; it authenticates itself
        # via the `signature` query param (verified inside the proxy router).
        path = request.url.path
        if (
            path in ["/health", "/ready", "/api/webhooks"]
            or path.startswith("/api/v1/storefront")
            or path.startswith("/proxy/catalog")
        ):
            return await call_next(request)

        api_key = request.headers.get("X-API-Key")

        if not api_key:
            return JSONResponse(
                status_code=401,
                content={"error": "Unauthorized", "message": "Missing X-API-Key header"}
            )

        import hmac as _hmac
        expected = settings.API_KEY or settings.BACKEND_API_KEY
        if not expected:
            return JSONResponse(
                status_code=500,
                content={"error": "Server misconfiguration", "message": "API_KEY not set"}
            )

        key_valid = _hmac.compare_digest(api_key.strip(), expected.strip())

        if not key_valid:
            return JSONResponse(
                status_code=401,
                content={"error": "Unauthorized", "message": "Invalid API Key"}
            )

        request.state.tenant_id = "default_tenant"
        request.state.shop_domain = request.headers.get("X-Shopify-Shop-Domain", "")

        return await call_next(request)
