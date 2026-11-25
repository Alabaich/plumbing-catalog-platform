"""
test_collection.py — Collection endpoint tests (2.8).
"""

from ._helpers import TEST_VENDOR, make_params, req, test


def run():
    print("\n── Collection ────────────────────────────────────")

    # happy path
    s, b = req(f"/proxy/catalog/collection/{TEST_VENDOR}", make_params())
    test(f"Collection — {TEST_VENDOR} → 200", s, b, 200)
    if s == 200 and isinstance(b, dict):
        total    = b.get("total", 0)
        per_page = b.get("per_page", 24)
        pages    = -(-total // per_page)  # ceiling division
        print(f"     total:    {total} products")
        print(f"     pages:    {pages}")
        print(f"     has_next: {b.get('has_next')}")

    # unknown vendor
    s, b = req("/proxy/catalog/collection/NonExistentBrandXYZ", make_params())
    test("Collection — unknown vendor → 404", s, b, 404)