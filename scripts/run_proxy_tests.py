#!/usr/bin/env python3
"""
run_proxy_tests.py — Proxy smoke test orchestrator.
Run from project root: python3 scripts/run_proxy_tests.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_proxy import test_auth, test_collection, test_product

print("=" * 50)
print("  App Proxy smoke tests")
print("=" * 50)

test_auth.run()
test_product.run()
test_collection.run()

print("\n" + "=" * 50)
print("  Done.")
print("=" * 50)