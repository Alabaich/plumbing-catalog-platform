"""
Diff Engine — hash comparison so scrapers only write changes.

How it works:
1. Scraper builds a product dict
2. compute_product_hash() / compute_variant_hash() creates a deterministic SHA-256
3. Compare new hash vs stored hash in DB
4. If different → update DB + trigger sync
5. If same → skip entirely, zero DB writes

The hash column (data_hash CHAR(64)) lives on BOTH tables:
- product_db.data_hash  — hash of product-level fields
- variant_db.data_hash  — hash of variant-level fields

This means:
- If only a variant price changes → product hash stays same, variant hash changes
- If description changes → product hash changes, variants untouched
- If new variant appears → it's a new SKU, gets INSERT + product images updated
"""

import hashlib
import json
import logging

logger = logging.getLogger("scraper.diff_engine")


def _compute_hash(data: dict) -> str:
    """
    Compute a deterministic SHA-256 hash from a dict.
    Filters out None, empty lists, and empty strings for consistency.

    Normalization note:
        None and [] are treated as equivalent — both are filtered out before hashing.
        This is intentional: for our purposes "no images" (None) and "empty image list" ([])
        represent the same state in the DB. This prevents spurious updates if a parser
        changes its return type without actual data changing.
        Side effect: a transition from None → [] (or vice versa) will NOT trigger an update.
        If parsers are not guaranteed to be consistent about this, add explicit normalization
        upstream before calling compute_product_hash() / compute_variant_hash().
    """
    filtered = {
        k: v for k, v in data.items()
        if v is not None and v != [] and v != ""
    }
    canonical = json.dumps(filtered, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_product_hash(product_data: dict) -> str:
    """
    Computes hash for product-level fields.
    Uses standardized naming.
    Includes variant_skus list to catch additions/removals of variants reliably.
    """
    fields_to_hash = {
        "name": product_data.get("name"),
        "desc": product_data.get("desc"),
        # Tags key must be normalized to lowercase "tags" by the parser before reaching here.
        # No fallback to "Tags" — silent fallback caused unpredictable hashing when
        # "tags" was present but falsy (e.g. ""), causing "Tags" to win unexpectedly.
        "tags": product_data.get("tags"),
        "opt_1": product_data.get("opt_1"),
        "opt_2": product_data.get("opt_2"),
        "opt_3": product_data.get("opt_3"),

        # Sorted fixes for list-type fields to prevent false positives from order changes
        "certifications": sorted(product_data.get("cert") or []),
        "image_url": sorted(product_data.get("image_url") or []),

        # variant_skus is included intentionally so that adding or removing a variant
        # invalidates the product hash and triggers a product-level sync.
        # Trade-off: this may cause minor extra work (e.g. re-syncing product metadata)
        # when only a variant changes. Accepted because the alternative — a separate
        # variant-set diff orchestrator — is significantly more complex.
        "variant_skus": sorted(product_data.get("variant_skus") or []),
    }
    return _compute_hash(fields_to_hash)


def compute_variant_hash(variant_data: dict) -> str:
    """
    Computes hash for variant-level fields.
    Does NOT include price (price/stock updates are handled in a separate pipeline).
    """
    fields_to_hash = {
        "opt_1_val": variant_data.get("opt_1_val"),
        "opt_2_val": variant_data.get("opt_2_val"),
        "opt_3_val": variant_data.get("opt_3_val"),
        "image_url": sorted(variant_data.get("image_url") or []),
        # variant_price REMOVED intentionally — prices are updated separately
    }
    return _compute_hash(fields_to_hash)


def should_update(new_hash: str, stored_hash: str | None) -> bool:
    """
    Check if data has changed.
    Returns True if:
    - No stored hash (new record)
    - Hash differs (data changed)
    """
    if stored_hash is None:
        return True
    return new_hash != stored_hash.strip()


def detect_changes(new_data: dict, old_data: dict, exclude_keys: set | None = None) -> set:
    """
    Find which fields changed between old and new data.
    Returns set of field names that differ.

    Useful for priority routing:
    - 'list_price' in changes → high priority sync
    - 'variant_image_src' in changes → re-upload images
    - 'description_html' in changes → normal sync
    """
    if exclude_keys is None:
        exclude_keys = {"updated_at", "created_at", "id", "data_hash"}

    changes = set()
    all_keys = set(new_data.keys()) | set(old_data.keys())

    for key in all_keys:
        if key in exclude_keys:
            continue
        new_val = new_data.get(key)
        old_val = old_data.get(key)
        if new_val != old_val:
            changes.add(key)

    return changes