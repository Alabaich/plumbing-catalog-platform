"""
Tests for the diff engine: hashes must be stable for equal data, change when a
tracked field changes, and ignore what the engine is designed to ignore.
"""

import copy

import pytest

from diff_engine import (
    compute_product_hash, compute_variant_hash, detect_changes, should_update,
)

PRODUCT = {
    "name": "Example Pull-Down Kitchen Faucet",
    "desc": "<p>Solid brass construction.</p>",
    "tags": "kitchen, faucet",
    "opt_1": "Color",
    "opt_2": "",
    "opt_3": None,
    "cert": ["https://example.com/spec.pdf", "https://example.com/install.pdf"],
    "image_url": ["https://example.com/a.jpg", "https://example.com/b.jpg"],
    "variant_skus": ["EX-100-CP", "EX-100-BN"],
}

VARIANT = {
    "opt_1_val": "Chrome",
    "opt_2_val": "",
    "opt_3_val": None,
    "image_url": ["https://example.com/a.jpg"],
}


def changed(base: dict, **overrides) -> dict:
    data = copy.deepcopy(base)
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# Product hash
# ---------------------------------------------------------------------------

def test_product_hash_is_a_stable_sha256():
    first = compute_product_hash(PRODUCT)
    assert first == compute_product_hash(copy.deepcopy(PRODUCT))
    assert len(first) == 64
    assert set(first) <= set("0123456789abcdef")


def test_product_hash_ignores_key_order():
    reordered = dict(reversed(list(PRODUCT.items())))
    assert compute_product_hash(reordered) == compute_product_hash(PRODUCT)


@pytest.mark.parametrize("field", ["image_url", "cert", "variant_skus"])
def test_product_hash_ignores_list_order(field):
    reordered = changed(PRODUCT, **{field: list(reversed(PRODUCT[field]))})
    assert compute_product_hash(reordered) == compute_product_hash(PRODUCT)


@pytest.mark.parametrize("field, value", [
    ("name", "Example Pull-Down Kitchen Faucet, 2nd Generation"),
    ("desc", "<p>Solid brass construction. Now with a ceramic cartridge.</p>"),
    ("tags", "kitchen, faucet, new"),
    ("opt_1", "Finish"),
    ("opt_2", "Handle"),
    ("opt_3", "Size"),
    ("cert", ["https://example.com/spec-v2.pdf"]),
    ("image_url", ["https://example.com/a.jpg"]),
])
def test_product_hash_changes_when_a_tracked_field_changes(field, value):
    assert compute_product_hash(changed(PRODUCT, **{field: value})) != compute_product_hash(PRODUCT)


def test_adding_or_removing_a_variant_changes_the_product_hash():
    added = changed(PRODUCT, variant_skus=PRODUCT["variant_skus"] + ["EX-100-MB"])
    removed = changed(PRODUCT, variant_skus=PRODUCT["variant_skus"][:1])
    assert compute_product_hash(added) != compute_product_hash(PRODUCT)
    assert compute_product_hash(removed) != compute_product_hash(PRODUCT)


@pytest.mark.parametrize("extra", [
    {"handle": "example_faucet_ex-100"},
    {"vendor": "ACME"},
    {"SKU": "EX-100"},
    {"certifications": '[{"text": "Spec Sheet"}]'},     # DB form of `cert`; only `cert` is hashed
    {"updated_at": "2026-01-01T00:00:00Z"},
    {"price": "199.00"},
])
def test_product_hash_ignores_untracked_fields(extra):
    assert compute_product_hash({**PRODUCT, **extra}) == compute_product_hash(PRODUCT)


@pytest.mark.parametrize("field", ["opt_2", "opt_3", "tags", "desc"])
def test_none_empty_string_and_missing_key_hash_the_same(field):
    as_none = changed(PRODUCT, **{field: None})
    as_empty = changed(PRODUCT, **{field: ""})
    missing = {k: v for k, v in PRODUCT.items() if k != field}
    assert compute_product_hash(as_none) == compute_product_hash(as_empty) == compute_product_hash(missing)


@pytest.mark.parametrize("field", ["cert", "image_url", "variant_skus"])
def test_none_and_empty_list_hash_the_same(field):
    as_none = changed(PRODUCT, **{field: None})
    as_empty = changed(PRODUCT, **{field: []})
    assert compute_product_hash(as_none) == compute_product_hash(as_empty)


def test_tags_key_is_case_sensitive():
    # The parser must normalize to lowercase "tags"; "Tags" is not read.
    with_capital = {k: v for k, v in PRODUCT.items() if k != "tags"}
    with_capital["Tags"] = PRODUCT["tags"]
    assert compute_product_hash(with_capital) != compute_product_hash(PRODUCT)


def test_empty_product_still_hashes():
    assert len(compute_product_hash({})) == 64


# ---------------------------------------------------------------------------
# Variant hash
# ---------------------------------------------------------------------------

def test_variant_hash_is_stable():
    assert compute_variant_hash(VARIANT) == compute_variant_hash(copy.deepcopy(VARIANT))


@pytest.mark.parametrize("field, value", [
    ("opt_1_val", "Brushed Nickel"),
    ("opt_2_val", "Lever"),
    ("opt_3_val", "8 in"),
    ("image_url", ["https://example.com/other.jpg"]),
])
def test_variant_hash_changes_when_a_tracked_field_changes(field, value):
    assert compute_variant_hash(changed(VARIANT, **{field: value})) != compute_variant_hash(VARIANT)


@pytest.mark.parametrize("extra", [
    {"variant_price": "249.00"},        # prices are maintained by a separate pipeline
    {"list_price": "299.00"},
    {"SKU": "EX-100-CP"},
    {"name": "ignored at variant level"},
])
def test_variant_hash_ignores_price_and_untracked_fields(extra):
    assert compute_variant_hash({**VARIANT, **extra}) == compute_variant_hash(VARIANT)


def test_variant_hash_ignores_image_order():
    two_images = changed(VARIANT, image_url=["https://example.com/a.jpg", "https://example.com/b.jpg"])
    swapped = changed(VARIANT, image_url=["https://example.com/b.jpg", "https://example.com/a.jpg"])
    assert compute_variant_hash(two_images) == compute_variant_hash(swapped)


def test_product_and_variant_hashes_are_independent():
    # A variant-only change must not look like a product change, and vice versa.
    assert compute_product_hash(changed(PRODUCT, opt_1_val="Chrome")) == compute_product_hash(PRODUCT)
    assert compute_variant_hash(changed(VARIANT, desc="new copy")) == compute_variant_hash(VARIANT)


# ---------------------------------------------------------------------------
# should_update / detect_changes
# ---------------------------------------------------------------------------

def test_should_update_for_a_new_record():
    assert should_update("a" * 64, None) is True


def test_should_update_is_false_for_an_equal_hash():
    assert should_update("a" * 64, "a" * 64) is False


def test_should_update_is_true_for_a_different_hash():
    assert should_update("a" * 64, "b" * 64) is True


def test_should_update_strips_char_column_padding():
    # data_hash is CHAR(64); some drivers return it padded.
    assert should_update("a" * 64, "a" * 64 + "   ") is False


def test_should_update_end_to_end():
    stored = compute_product_hash(PRODUCT)
    assert should_update(compute_product_hash(copy.deepcopy(PRODUCT)), stored) is False
    assert should_update(compute_product_hash(changed(PRODUCT, desc="<p>New.</p>")), stored) is True


def test_detect_changes_lists_differing_fields():
    old = {"title": "A", "list_price": "10", "tags": "x", "id": 1, "updated_at": "then", "data_hash": "h1"}
    new = {"title": "A", "list_price": "12", "vendor": "ACME", "id": 2, "updated_at": "now", "data_hash": "h2"}
    # id, timestamps and the hash itself are excluded by default
    assert detect_changes(new, old) == {"list_price", "tags", "vendor"}


def test_detect_changes_with_custom_exclusions():
    assert detect_changes({"a": 1, "b": 2}, {"a": 9, "b": 8}, exclude_keys={"a"}) == {"b"}
