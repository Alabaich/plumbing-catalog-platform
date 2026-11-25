"""
domain.py — SQLAlchemy models for tables this app owns.

We deliberately do NOT model the legacy catalog tables (product_db,
variant_db, pricesheets, parsing_queue) here. Those use quoted column names
("Variant SKU", "List", "MAP", etc.) and are accessed via raw text() SQL
throughout the codebase. Adding a model would be a footgun.

Identity model:
    - `Tenant.id` is our internal UUID — use it in logs, billing exports,
      anywhere we don't want a domain leaking into URLs.
    - `Tenant.shop_domain` is the Shopify-issued handle
      (e.g. example-store.myshopify.com). It's unique, immutable for
      the life of an install, and is what every request carries in the
      X-Shopify-Shop-Domain header. The per-shop tables FK to this column
      so we don't have to translate domain → UUID on every request.
"""

import uuid
from sqlalchemy import (
    Column, String, Text, Boolean, DateTime, Integer, BigInteger, Numeric,
    ForeignKey, Index, PrimaryKeyConstraint, desc, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from .database import Base


# ---------------------------------------------------------------------------
# Tenants — one row per installed Shopify store
# ---------------------------------------------------------------------------

class Tenant(Base):
    """
    Lifecycle:
        - Row created on first authenticated request from a shop
          (or on Shopify OAuth callback if you wire that path).
        - `is_active` flipped to false on the app/uninstalled webhook;
          row is kept for analytics + billing reconciliation.
        - `api_key_hash` rotated on regenerate — stored as sha256(plaintext).
          Plaintext key is shown to the merchant once and never persisted.
    """
    __tablename__ = "tenants"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    shop_domain    = Column(String(255), unique=True, nullable=False)
    api_key_hash   = Column(String(64), nullable=False, index=True)
    plan           = Column(String(32), nullable=False, server_default=text("'free'"))
    is_active      = Column(Boolean, nullable=False, server_default=text("true"))
    installed_at   = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    uninstalled_at = Column(DateTime(timezone=True))
    updated_at     = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    contact_email  = Column(String(255))
    notes          = Column(Text)


class TenantSubscribedBrand(Base):
    """
    Brands a tenant is allowed to see/use. If a tenant has zero rows here,
    treat it as 'unrestricted' (see require_tenant). If they have >=1, only
    those brands are exposed in product listings and discount actions.
    """
    __tablename__ = "tenant_subscribed_brands"

    shop_domain = Column(
        String(255),
        ForeignKey("tenants.shop_domain", ondelete="CASCADE"),
        nullable=False,
    )
    vendor   = Column(String(255), nullable=False)
    added_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("shop_domain", "vendor"),
    )


# ---------------------------------------------------------------------------
# Per-tenant operational state
# ---------------------------------------------------------------------------

class TenantImportedVendor(Base):
    """
    Records which brands a shop has imported via POST /products/import.
    The discounts page (brands/list?scope=imported) filters by this table.

    `imported_count` accumulates across chunks so we know roughly how many
    SKUs each shop has live for each brand.
    """
    __tablename__ = "tenant_imported_vendors"

    shop_domain = Column(
        String(255),
        ForeignKey("tenants.shop_domain", ondelete="CASCADE"),
        nullable=False,
    )
    vendor            = Column(String(255), nullable=False)
    first_imported_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_imported_at  = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    imported_count    = Column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (
        PrimaryKeyConstraint("shop_domain", "vendor"),
    )


class TenantBrandDiscount(Base):
    """
    Standing brand-level discount, set on the discounts page.

    Semantics:
        - `discount_pct = 0` means 'no discount' — when set explicitly, the
          /discount flow reverts variants (price = List, compareAtPrice = NULL).
        - Read by /import so newly-uploaded variants inherit the discount.
        - Read by /brands/list?scope=imported to prefill the input on the UI.

    The price math (price = List * (1 - pct), clamped to MAP) is computed at
    write time inside ShopifyClient.apply_brand_discount, not stored here.
    """
    __tablename__ = "tenant_brand_discounts"

    shop_domain = Column(
        String(255),
        ForeignKey("tenants.shop_domain", ondelete="CASCADE"),
        nullable=False,
    )
    vendor       = Column(String(255), nullable=False)
    discount_pct = Column(Numeric(5, 2), nullable=False)
    set_at       = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at   = Column(DateTime(timezone=True))

    __table_args__ = (
        PrimaryKeyConstraint("shop_domain", "vendor"),
    )


class TenantVariantPrice(Base):
    """
    Append-only price history. One row per variant per change.

    This is the analytics asset. Sample queries it makes easy:
        - 'Show price drift for Vendor X across all shops in the last 30 days'
        - 'Which shops have variants currently clamped at MAP for Vendor Y'
        - 'Mean discount-on-list per brand'
        - 'How often does shop Z re-discount the same SKU'

    Do not update rows in place — always insert new ones. The current state
    of any (shop_domain, variant_sku) is the row with the latest applied_at.
    """
    __tablename__ = "tenant_variant_prices"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    shop_domain = Column(
        String(255),
        ForeignKey("tenants.shop_domain", ondelete="CASCADE"),
        nullable=False,
    )
    variant_sku    = Column(String(255), nullable=False)
    vendor         = Column(String(255))
    list_price     = Column(Numeric(10, 2))
    new_price      = Column(Numeric(10, 2))
    map_price      = Column(Numeric(10, 2))
    discount_pct   = Column(Numeric(5, 2))
    clamped_to_map = Column(Boolean, nullable=False, server_default=text("false"))
    source         = Column(String(32))  # 'brand_discount' | 'import' | 'manual' | 'revert'
    applied_at     = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_tvp_shop_sku_time", "shop_domain", "variant_sku", desc("applied_at")),
        Index("ix_tvp_vendor_time", "vendor", desc("applied_at")),
    )


# ---------------------------------------------------------------------------
# LLM enrichment — written by services/scrapers/enrichment.py (raw SQL)
# ---------------------------------------------------------------------------

class ProductEnrichment(Base):
    """
    LLM-enriched content for one product, kept apart from the scraped data in
    product_db so a bad model response can never overwrite source data.

    `handle` matches product_db.handle. There is no FK because product_db is
    a legacy table this app does not manage (see module docstring).

    Lifecycle:
        - `source_hash` is product_db.data_hash at the time of enrichment.
          A product is re-enriched only when the two differ.
        - `status = 'published'`    passed validation; readers may use it.
        - `status = 'needs_review'` invalid or suspicious; readers ignore the
          row until a human approves it (`reviewed_at` is set on approval).
        - `review_reasons` lists why it was held, e.g. ["unknown_product_type"].
    """
    __tablename__ = "product_enrichment"

    handle           = Column(Text, primary_key=True)
    vendor           = Column(String(255))
    source_hash      = Column(String(64), nullable=False)
    content_hash     = Column(String(64), nullable=False)
    status           = Column(String(32), nullable=False)  # 'published' | 'needs_review'
    title            = Column(Text)          # cleaned product title
    html_description = Column(Text)
    html_specs_table = Column(Text)
    product_type     = Column(String(255))   # taxonomy leaf
    category         = Column(Text)          # full taxonomy path
    review_reasons   = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    llm_provider     = Column(String(32))
    llm_model        = Column(String(128))
    reviewed_at      = Column(DateTime(timezone=True))
    created_at       = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at       = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_product_enrichment_status", "status"),
        Index("ix_product_enrichment_vendor", "vendor"),
    )


class LlmEnrichmentCache(Base):
    """
    Model results keyed by a hash of the exact request (prompts, provider,
    model, cleaned input). Insert-only: the same input is never sent twice.
    """
    __tablename__ = "llm_enrichment_cache"

    content_hash = Column(String(64), primary_key=True)
    result       = Column(JSONB, nullable=False)
    llm_provider = Column(String(32))
    llm_model    = Column(String(128))
    created_at   = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
