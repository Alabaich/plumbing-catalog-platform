"""
test_product.py — Product endpoint tests (2.5 + 2.6).
"""

from ._helpers import TEST_HANDLE, TEST_SHOP, make_params, req, test


def run():
    print("\n── Product ───────────────────────────────────────")

    # 2.5 happy path
    s, b = req(f"/proxy/catalog/product/{TEST_HANDLE}", make_params())
    test("Product — real handle → 200", s, b, 200)
    if s == 200 and isinstance(b, dict):
        img = b.get("primary_image") or ""
        print(f"     handle:   {b.get('handle')}")
        print(f"     variants: {len(b.get('variants', []))} pcs")
        print(f"     image:    {img[:60]}")

    # 2.5 bad handle
    s, b = req("/proxy/catalog/product/non-existent-handle-xyz", make_params())
    test("Product — bad handle → 404", s, b, 404)

    # 2.6 description happy path
    s, b = req(f"/proxy/catalog/product/{TEST_HANDLE}/description", make_params())
    test("Description — real handle → 200", s, b, 200)
    if s == 200 and isinstance(b, dict):
        desc = (b.get("description") or "")[:80].replace("\n", " ")
        print(f"     description: {desc}...")

    # 2.6 bad handle
    s, b = req("/proxy/catalog/product/non-existent-xyz/description", make_params())
    test("Description — bad handle → 404", s, b, 404)