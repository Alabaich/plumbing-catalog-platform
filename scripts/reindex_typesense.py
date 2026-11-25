#!/usr/bin/env python3
"""
Bulk reindex of product_db → Typesense `products_v1`.

Standalone script, run by hand or from cron after a scrape cycle.
Published LLM enrichment (product_enrichment) replaces the scraped
description, product type and category in the indexed document.

Usage:
    # Validate query + show counters, no writes
    python reindex_typesense.py --dry-run

    # Drop collection and rebuild from scratch
    python reindex_typesense.py --recreate

    # Upsert into existing collection (won't drop)
    python reindex_typesense.py

Required env vars:
    DATABASE_URL          Postgres connection string (see .env.example)
    TYPESENSE_API_KEY     from .env
    TYPESENSE_HOST        default 127.0.0.1
    TYPESENSE_PORT        default 8108
    TYPESENSE_PROTOCOL    default http

Dependencies:
    pip install psycopg2-binary typesense beautifulsoup4
"""
import os
import re
import sys
import json
import argparse
from pathlib import Path
from typing import Optional, List

import psycopg2
import psycopg2.extras
import typesense
from bs4 import BeautifulSoup

COLLECTION_NAME = "products_v1"
BATCH_SIZE = 500
PRICE_RE = re.compile(r"^[0-9.]+$")
SCHEMA_PATH = Path(__file__).parent.parent / "infra" / "typesense" / "schema.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_price(raw) -> Optional[float]:
    """Parse a varchar price to float. Returns None if empty or non-numeric."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or not PRICE_RE.match(s):
        return None
    try:
        v = float(s)
        return v if v > 0 else None
    except ValueError:
        return None


def strip_html(html: Optional[str], max_len: int = 5000) -> str:
    """Strip HTML tags, decode entities, collapse whitespace, cap length."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return text[:max_len]


def split_tags(tags: Optional[str]) -> List[str]:
    if not tags:
        return []
    return [t.strip() for t in tags.split(",") if t.strip()]


# ---------------------------------------------------------------------------
# Aggregation query
# Single round-trip per product using LATERAL join with rolled-up variants.
# ---------------------------------------------------------------------------

SQL = """
SELECT
    p.handle,
    p.handle_shopify,
    COALESCE(NULLIF(e.title, ''), p."Title")        AS title,
    COALESCE(
        e.html_description || ' ' || COALESCE(e.html_specs_table, ''),
        p."Body (HTML)"
    )                                               AS body_html,
    p."Vendor"                                      AS vendor,
    p."Tags"                                        AS tags,
    COALESCE(e.product_type, p."Product Type")      AS product_type,
    COALESCE(e.category, p."Category")              AS category,
    p."Status"                                      AS status,
    p."Option1 Name"                                AS option1_name,
    p."Option2 Name"                                AS option2_name,
    p."Option3 Name"                                AS option3_name,
    p.all_image_srcs,
    p.all_image_srcs_new,
    p.components,
    EXTRACT(EPOCH FROM p.updated_at)::bigint        AS updated_at_unix,
    COALESCE(v.skus,            ARRAY[]::text[])    AS skus,
    COALESCE(v.option1_values,  ARRAY[]::text[])    AS option1_values,
    COALESCE(v.option2_values,  ARRAY[]::text[])    AS option2_values,
    COALESCE(v.option3_values,  ARRAY[]::text[])    AS option3_values,
    COALESCE(v.variant_prices,  ARRAY[]::text[])    AS variant_prices,
    COALESCE(v.list_prices,     ARRAY[]::numeric[]) AS list_prices,
    COALESCE(v.variant_count, 0)                    AS variant_count
FROM product_db p
-- LLM-enriched content wins over raw scraped fields, but only once published;
-- rows held as 'needs_review' are ignored and the scraped values are indexed.
LEFT JOIN product_enrichment e
    ON e.handle = p.handle AND e.status = 'published'
LEFT JOIN LATERAL (
    SELECT
        ARRAY_AGG(vd."Variant SKU")
            FILTER (WHERE vd."Variant SKU" IS NOT NULL AND vd."Variant SKU" != '')
            AS skus,
        ARRAY_AGG(DISTINCT vd."Option1 Value")
            FILTER (WHERE vd."Option1 Value" IS NOT NULL AND vd."Option1 Value" != '')
            AS option1_values,
        ARRAY_AGG(DISTINCT vd."Option2 Value")
            FILTER (WHERE vd."Option2 Value" IS NOT NULL AND vd."Option2 Value" != '')
            AS option2_values,
        ARRAY_AGG(DISTINCT vd."Option3 Value")
            FILTER (WHERE vd."Option3 Value" IS NOT NULL AND vd."Option3 Value" != '')
            AS option3_values,
        ARRAY_AGG(vd."Variant Price")
            FILTER (WHERE vd."Variant Price" ~ '^[0-9.]+$')
            AS variant_prices,
        ARRAY_AGG(vd."List")
            FILTER (WHERE vd."List" IS NOT NULL)
            AS list_prices,
        COUNT(*) AS variant_count
    FROM variant_db vd
    WHERE vd.handle = p.handle
) v ON TRUE
WHERE p."Status" = 'active'
ORDER BY p.id
"""


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------

def row_to_doc(row) -> Optional[dict]:
    """Map one Postgres row to a Typesense document. Returns None to skip."""
    handle = row["handle"]
    title = (row["title"] or "").strip()
    if not title:
        return None  # Untitled product → not searchable, skip

    # Images: prefer the new array, fall back to the old one
    images_new = row["all_image_srcs_new"] or []
    images_old = row["all_image_srcs"] or []
    images = images_new if images_new else images_old
    image_primary = images[0] if images else None

    # Pricing:
    #   1. Variant Price (preferred — actual scraped retail)
    #   2. List          (fallback — manufacturer MSRP, only if no Variant Price)
    #   MAP intentionally excluded (it's a legal floor, not a price).
    variant_floats = [
        p for p in (parse_price(x) for x in (row["variant_prices"] or [])) if p
    ]
    if variant_floats:
        prices = variant_floats
    else:
        prices = [
            float(lp) for lp in (row["list_prices"] or [])
            if lp is not None and float(lp) > 0
        ]

    has_price = bool(prices)
    price_min = min(prices) if has_price else None
    price_max = max(prices) if has_price else None

    doc = {
        "id":              handle,
        "handle":          handle,
        "handle_shopify":  row["handle_shopify"] or handle,
        "title":           title,
        "description_text": strip_html(row["body_html"]),
        "vendor":          row["vendor"] or "Unknown",
        "product_type":    row["product_type"],
        "category":        row["category"],
        "tags":            split_tags(row["tags"]),
        "components":      row["components"] or [],
        "option1_name":    row["option1_name"],
        "option2_name":    row["option2_name"],
        "option3_name":    row["option3_name"],
        "option1_values":  row["option1_values"],
        "option2_values":  row["option2_values"],
        "option3_values":  row["option3_values"],
        "skus":            row["skus"],
        "image_primary":   image_primary,
        "image_count":     len(images),
        "has_price":       has_price,
        "variant_count":   int(row["variant_count"]),
        "updated_at_unix": int(row["updated_at_unix"] or 0),
    }
    if has_price:
        doc["price_min"] = price_min
        doc["price_max"] = price_max

    # Strip None/empty optional fields — Typesense doesn't want them
    return {
        k: v for k, v in doc.items()
        if v is not None and v != "" and v != []
    }


# ---------------------------------------------------------------------------
# Typesense client
# ---------------------------------------------------------------------------

def get_typesense_client() -> typesense.Client:
    return typesense.Client({
        "api_key": os.environ["TYPESENSE_API_KEY"],
        "nodes": [{
            "host":     os.environ.get("TYPESENSE_HOST", "127.0.0.1"),
            "port":     os.environ.get("TYPESENSE_PORT", "8108"),
            "protocol": os.environ.get("TYPESENSE_PROTOCOL", "http"),
        }],
        "connection_timeout_seconds": 10,
    })


def ensure_collection(ts: typesense.Client, recreate: bool = False) -> None:
    schema = json.loads(SCHEMA_PATH.read_text())
    try:
        existing = ts.collections[COLLECTION_NAME].retrieve()
        if recreate:
            print(f"  Dropping existing collection ({existing['num_documents']} docs)…")
            ts.collections[COLLECTION_NAME].delete()
            ts.collections.create(schema)
            print(f"  Created fresh: {COLLECTION_NAME}")
        else:
            print(f"  Existing collection: {existing['num_documents']} docs (will upsert)")
    except typesense.exceptions.ObjectNotFound:
        ts.collections.create(schema)
        print(f"  Created: {COLLECTION_NAME}")


def flush_batch(ts: typesense.Client, batch: list) -> int:
    """Send a batch to Typesense, return count of failures."""
    raw = ts.collections[COLLECTION_NAME].documents.import_(
        batch, {"action": "upsert"}
    )
    # `import_` returns one JSON object per doc, separated by newlines, as a string
    if isinstance(raw, str):
        results = [json.loads(line) for line in raw.strip().split("\n") if line]
    else:
        results = raw
    failures = [r for r in results if not r.get("success")]
    if failures:
        print(f"  WARNING: {len(failures)}/{len(batch)} docs failed.")
        print(f"  First failure: {failures[0]}")
    return len(failures)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't write to Typesense, just count and validate")
    parser.add_argument("--recreate", action="store_true",
                        help="Drop and recreate the collection before indexing")
    args = parser.parse_args()

    print("=== Typesense reindex ===")
    print(f"  Mode: {'DRY RUN' if args.dry_run else ('RECREATE' if args.recreate else 'UPSERT')}")

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 2

    conn = psycopg2.connect(db_url, cursor_factory=psycopg2.extras.RealDictCursor)

    ts = None
    if not args.dry_run:
        if not os.environ.get("TYPESENSE_API_KEY"):
            print("ERROR: TYPESENSE_API_KEY not set", file=sys.stderr)
            return 2
        ts = get_typesense_client()
        ensure_collection(ts, recreate=args.recreate)

    total_in = 0
    total_skipped = 0
    total_indexed = 0
    no_price = 0
    failures = 0

    # Server-side cursor — won't load 40k rows into memory at once
    with conn.cursor(name="reindex_cursor") as cur:
        cur.itersize = BATCH_SIZE
        cur.execute(SQL)

        batch = []
        for row in cur:
            total_in += 1
            doc = row_to_doc(row)
            if doc is None:
                total_skipped += 1
                continue
            if not doc.get("has_price"):
                no_price += 1
            batch.append(doc)

            if len(batch) >= BATCH_SIZE:
                if not args.dry_run:
                    failures += flush_batch(ts, batch)
                total_indexed += len(batch)
                print(f"  {total_in:>6} read | {total_indexed:>6} indexed | "
                      f"{total_skipped} skipped | {no_price} no-price")
                batch.clear()

        if batch:
            if not args.dry_run:
                failures += flush_batch(ts, batch)
            total_indexed += len(batch)

    conn.close()

    print()
    print(f"=== Summary ===")
    print(f"  Read:           {total_in}")
    print(f"  Indexed:        {total_indexed}")
    print(f"  Skipped (no title): {total_skipped}")
    print(f"  Without price:  {no_price}")
    print(f"  Failures:       {failures}")
    if args.dry_run:
        print("  (DRY RUN — no writes performed)")

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())