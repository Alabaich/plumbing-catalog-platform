import os
from sqlalchemy import create_engine, text
from app.config import settings


class TenantService:
    """
    Reads Shopify access tokens from the Prisma-managed `Session` table
    in Postgres (written by @shopify/shopify-app-session-storage-prisma
    on the Remix side). Single source of truth — no SQLite file in sight.
    """

    def __init__(self):
        # Prefer a dedicated URL if Sessions live in a different DB, fall back
        # to the main app DB. Most setups co-locate them in plumbing_products.
        db_url = os.getenv("SESSION_DATABASE_URL") or os.getenv("DATABASE_URL")
        if not db_url:
            raise RuntimeError(
                "Neither SESSION_DATABASE_URL nor DATABASE_URL is set"
            )
        self.engine = create_engine(db_url, pool_pre_ping=True)

    def get_shopify_credentials(self, shop_domain: str) -> str | None:
        """
        Returns the most recent non-expired access token for a shop, or None.

        Note on quoting: Prisma generates PascalCase table/column names as
        case-sensitive identifiers in Postgres, so they MUST be double-quoted.
        Unquoted `session.accessToken` would be folded to lowercase and fail.
        """
        query = text("""
            SELECT "accessToken"
            FROM "Session"
            WHERE "shop" = :shop
              AND ("expires" IS NULL OR "expires" > now())
            ORDER BY
              CASE WHEN "isOnline" = false THEN 0 ELSE 1 END,  -- prefer offline tokens
              "expires" DESC NULLS FIRST
            LIMIT 1
        """)
        try:
            with self.engine.connect() as conn:
                row = conn.execute(query, {"shop": shop_domain}).fetchone()
                if row:
                    return row[0]
                print(f"--- ERROR: No Session row for shop {shop_domain} ---")
                return None
        except Exception as e:
            print(f"--- DATABASE ERROR in TenantService: {e} ---")
            return None