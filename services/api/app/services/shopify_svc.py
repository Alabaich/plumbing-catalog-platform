import asyncio
import httpx
import json
from app.config import settings
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from sqlalchemy import text

class ShopifyClient:
    def __init__(self, shop_url: str, access_token: str):
        self.shop_url = shop_url
        self.api_url = f"https://{shop_url}/admin/api/{settings.SHOPIFY_API_VERSION}"
        self.graphql_url = f"https://{shop_url}/admin/api/{settings.SHOPIFY_API_VERSION}/graphql.json"
        self.headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json"
        }

    async def get_variant_by_sku(self, sku: str) -> dict:
        if not sku:
            return None
        query = """
        query($query: String!) {
          productVariants(first: 1, query: $query) {
            edges {
              node {
                id
                price
              }
            }
          }
        }
        """
        payload = {
            "query": query,
            "variables": {"query": f"sku:{sku}"}
        }
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(self.graphql_url, json=payload, headers=self.headers, timeout=10.0)
                if response.status_code == 200:
                    data = response.json()
                    edges = data.get("data", {}).get("productVariants", {}).get("edges", [])
                    if edges:
                        node = edges[0]["node"]
                        return {
                            "variant_id": node["id"],
                            "price": node["price"]
                        }
            except Exception as e:
                print(f"GraphQL Error: {str(e)}")
            return None

    async def create_product(
        self,
        product_data: dict,
        variants_data: list,
        discount_pct: float = 0.0,
    ) -> dict | None:
        """
        Create a Shopify product whose variants are priced from variant_db.

        Pricing rules:
          - price            = max(List * (1 - discount_pct/100), MAP)
          - compare_at_price = List   (when discount_pct > 0)
                             = None   (when no discount)
          - Variants without a numeric `List` are skipped — better to leave them
            off the upload than to push a $0.00 product live.

        `discount_pct` is the standing brand discount looked up by the caller in
        tenant_brand_discounts. Pass 0.0 (default) to import at full list price.
        """
        title = product_data.get("Title") or "Untitled Product"
        handle = (
            product_data.get("clean slug")
            or product_data.get("clean_slug")
            or product_data.get("handle")
            or ""
        )
        vendor = product_data.get("Vendor") or product_data.get("vendor") or ""

        variants = []
        for v in variants_data:
            list_raw = v.get("List")
            if list_raw is None:
                continue
            try:
                list_price = float(list_raw)
            except (TypeError, ValueError):
                continue
            if list_price <= 0:
                continue

            try:
                map_floor = float(v.get("MAP")) if v.get("MAP") is not None else 0.0
            except (TypeError, ValueError):
                map_floor = 0.0

            if discount_pct and discount_pct > 0:
                target = list_price * (1 - discount_pct / 100.0)
                price = max(target, map_floor)
                compare_at = list_price
            else:
                price = list_price
                compare_at = None

            v_dict: dict = {"price": "{:.2f}".format(price)}
            if compare_at is not None:
                v_dict["compare_at_price"] = "{:.2f}".format(compare_at)

            sku = v.get("Variant SKU") or v.get("Variant_SKU")
            if sku:
                v_dict["sku"] = str(sku).strip()

            opt1 = v.get("Option1 Value") or v.get("Option1_Value")
            v_dict["option1"] = str(opt1).strip() if opt1 else "Default Title"

            variants.append(v_dict)

        if not variants:
            print(f"Skipping product '{title}': no variants with usable List price")
            return None

        payload = {
            "product": {
                "title": str(title),
                "handle": str(handle),
                "vendor": str(vendor),
                "status": "draft",
                "variants": variants,
            }
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self.api_url}/products.json",
                    json=payload,
                    headers=self.headers,
                    timeout=45.0,
                )
                return response.json() if response.status_code == 201 else None
            except Exception as e:
                print(f"Error creating product: {e}")
                return None

    async def sync_vendor_prices_for_discount(self, db, vendor: str, discount_pct: float) -> int:
        """
        Re-price every Shopify variant for `vendor` using catalog List/MAP when the SKU
        exists in variant_db; otherwise falls back to compareAtPrice or current price as list.
        Matches create_product math so saved discount_pct stays consistent after import.
        """
        q_vendor = str(vendor).replace("'", "\\'")
        search_query = f"vendor:'{q_vendor}'"
        gql = """
        query($q: String!, $cursor: String) {
          productVariants(first: 50, query: $q, after: $cursor) {
            pageInfo { hasNextPage }
            edges {
              cursor
              node {
                id
                sku
                price
                compareAtPrice
              }
            }
          }
        }
        """
        update_mutation = """
        mutation productVariantUpdate($input: ProductVariantInput!) {
          productVariantUpdate(input: $input) {
            userErrors { field message }
          }
        }
        """
        updated = 0
        async with httpx.AsyncClient() as client:
            cursor = None
            has_next = True
            while has_next:
                variables: Dict = {"q": search_query, "cursor": cursor}
                resp = await client.post(
                    self.graphql_url,
                    json={"query": gql, "variables": variables},
                    headers=self.headers,
                    timeout=60.0,
                )
                payload = resp.json()
                if payload.get("errors"):
                    print(f"sync_vendor_prices GraphQL errors: {payload['errors']}")
                    break
                pv = (payload.get("data") or {}).get("productVariants") or {}
                edges = pv.get("edges") or []
                for edge in edges:
                    node = edge.get("node") or {}
                    vid = node.get("id")
                    sku = (node.get("sku") or "").strip()
                    if not vid:
                        continue
                    try:
                        cur_price = float(node.get("price") or 0)
                    except (TypeError, ValueError):
                        cur_price = 0.0
                    cap = node.get("compareAtPrice")
                    try:
                        compare_val = float(cap) if cap is not None else None
                    except (TypeError, ValueError):
                        compare_val = None

                    list_price: Optional[float] = None
                    map_floor = 0.0
                    if sku:
                        row = db.execute(
                            text(
                                'SELECT "List", "MAP" FROM "variant_db" '
                                'WHERE TRIM("Variant SKU") = :sku LIMIT 1'
                            ),
                            {"sku": sku},
                        ).mappings().first()
                        if row and row.get("List") is not None:
                            try:
                                list_price = float(row["List"])
                            except (TypeError, ValueError):
                                list_price = None
                            try:
                                map_floor = float(row["MAP"]) if row.get("MAP") is not None else 0.0
                            except (TypeError, ValueError):
                                map_floor = 0.0

                    if list_price is None:
                        if compare_val is not None:
                            list_price = compare_val
                        else:
                            list_price = cur_price

                    if list_price is None or list_price <= 0:
                        continue

                    d = max(0.0, min(100.0, float(discount_pct)))
                    if d > 0:
                        target = list_price * (1.0 - d / 100.0)
                        new_price = max(target, map_floor)
                        new_compare: Optional[float] = list_price
                    else:
                        new_price = list_price
                        new_compare = None

                    cap_empty = cap is None or (isinstance(cap, str) and not str(cap).strip())
                    if abs(new_price - cur_price) < 0.005 and (
                        (new_compare is None and cap_empty)
                        or (
                            new_compare is not None
                            and compare_val is not None
                            and abs(new_compare - compare_val) < 0.005
                        )
                    ):
                        continue

                    inp: Dict = {"id": vid, "price": "{:.2f}".format(new_price)}
                    if new_compare is not None:
                        inp["compareAtPrice"] = "{:.2f}".format(new_compare)
                    else:
                        inp["compareAtPrice"] = None

                    up = await client.post(
                        self.graphql_url,
                        json={"query": update_mutation, "variables": {"input": inp}},
                        headers=self.headers,
                        timeout=30.0,
                    )
                    body = up.json()
                    errs = (
                        (body.get("data") or {})
                        .get("productVariantUpdate", {})
                        .get("userErrors")
                        or []
                    )
                    if errs:
                        print(f"sync_vendor_prices userErrors sku={sku}: {errs}")
                    else:
                        updated += 1
                    await asyncio.sleep(0.12)

                has_next = pv.get("pageInfo", {}).get("hasNextPage", False)
                if has_next and edges:
                    cursor = edges[-1]["cursor"]
                else:
                    has_next = False

        return updated

    async def update_variant_prices_by_vendor(self, vendor: str, discount_percent: float):
        query = """
        query($query: String!, $cursor: String) {
          productVariants(first: 100, query: $query, after: $cursor) {
            pageInfo { hasNextPage }
            edges {
              cursor
              node {
                id
                price
                compareAtPrice
              }
            }
          }
        }
        """
        variables = {"query": f"vendor:'{vendor}'"}
        all_variants = []
        has_next = True
        cursor = None

        async with httpx.AsyncClient() as client:
            while has_next:
                variables["cursor"] = cursor
                resp = await client.post(self.graphql_url, json={"query": query, "variables": variables}, headers=self.headers)
                res_data = resp.json().get("data", {}).get("productVariants", {})
                
                for edge in res_data.get("edges", []):
                    all_variants.append(edge["node"])
                
                has_next = res_data.get("pageInfo", {}).get("hasNextPage", False)
                if has_next:
                    cursor = res_data["edges"][-1]["cursor"]

            update_mutation = """
            mutation productVariantUpdate($input: ProductVariantInput!) {
              productVariantUpdate(input: $input) {
                userErrors { message }
              }
            }
            """
            for variant in all_variants:
                current_price = float(variant["price"])
                original_price = float(variant["compareAtPrice"]) if variant["compareAtPrice"] else current_price
                new_price = original_price * (1 - (discount_percent / 100))
                
                input_data = {
                    "id": variant["id"],
                    "price": "{:.2f}".format(new_price),
                    "compareAtPrice": "{:.2f}".format(original_price)
                }
                await client.post(self.graphql_url, json={"query": update_mutation, "variables": {"input": input_data}}, headers=self.headers)
        return len(all_variants)

    async def get_brand_discount(self, vendor: str) -> Optional[float]:
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    f"{self.api_url}/price_rules.json",
                    params={"status": "active"},
                    headers=self.headers,
                    timeout=10.0
                )
                
                if response.status_code == 200:
                    price_rules = response.json().get("price_rules", [])
                    
                    for rule in price_rules:
                        title = rule.get("title", "")
                        if vendor.lower() in title.lower():
                            value = rule.get("value", "0")
                            ends_at = rule.get("ends_at")
                            
                            if ends_at:
                                end_date = datetime.fromisoformat(ends_at.replace("Z", "+00:00"))
                                if end_date < datetime.now(end_date.tzinfo):
                                    continue
                            
                            return float(value)
                return None
                
            except Exception as e:
                print(f"Error checking discount: {e}")
                return None

    async def _get_or_create_vendor_collection(self, vendor: str) -> Optional[str]:
        title = f"Auto Discount: {vendor}"
        query = """
        query($query: String!) {
          collections(first: 1, query: $query) {
            edges {
              node {
                id
              }
            }
          }
        }
        """
        variables = {"query": f"title:'{title}'"}
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    self.graphql_url,
                    json={"query": query, "variables": variables},
                    headers=self.headers,
                    timeout=10.0
                )
                data = response.json()
                edges = data.get("data", {}).get("collections", {}).get("edges", [])
                if edges:
                    return edges[0]["node"]["id"]
            except Exception as e:
                print(f"Error finding collection: {e}")

            mutation = """
            mutation collectionCreate($input: CollectionInput!) {
              collectionCreate(input: $input) {
                collection {
                  id
                }
                userErrors {
                  message
                }
              }
            }
            """
            input_data = {
                "title": title,
                "ruleSet": {
                    "appliedDisjunctively": False,
                    "rules": [
                        {
                            "column": "VENDOR",
                            "relation": "EQUALS",
                            "condition": vendor
                        }
                    ]
                }
            }
            try:
                response = await client.post(
                    self.graphql_url,
                    json={"query": mutation, "variables": {"input": input_data}},
                    headers=self.headers,
                    timeout=10.0
                )
                data = response.json()
                mutation_obj = data.get("data", {}).get("collectionCreate", {})
                if mutation_obj and mutation_obj.get("collection"):
                    return mutation_obj["collection"]["id"]
                else:
                    print(f"Failed to create collection: {mutation_obj.get('userErrors') if mutation_obj else 'Unknown error'}")
                    return None
            except Exception as e:
                print(f"Error creating collection: {e}")
                return None

    async def apply_brand_discount(self, vendor: str, discount_percent: float, duration_days: Optional[int] = None) -> int:
        collection_gid = await self._get_or_create_vendor_collection(vendor)
        if not collection_gid:
            print(f"Failed to get or create collection for vendor '{vendor}'")
            return 0

        mutation = """
        mutation discountAutomaticBasicCreate($automaticBasicDiscount: DiscountAutomaticBasicInput!) {
          discountAutomaticBasicCreate(automaticBasicDiscount: $automaticBasicDiscount) {
            automaticDiscountNode {
              id
            }
            userErrors {
              field
              message
            }
          }
        }
        """
        
        starts_at = datetime.utcnow().isoformat() + "Z"
        
        discount_input = {
            "title": f"Auto Discount {vendor}",
            "startsAt": starts_at,
            "minimumRequirement": {
                "quantity": {
                    "greaterThanOrEqualToQuantity": "1"
                }
            },
            "customerGets": {
                "value": {
                    "percentage": float(abs(discount_percent)) / 100.0
                },
                "items": {
                    "collections": {
                        "add": [collection_gid]
                    }
                }
            }
        }
        
        if duration_days:
            ends_at = (datetime.utcnow() + timedelta(days=duration_days)).isoformat() + "Z"
            discount_input["endsAt"] = ends_at
            
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    self.graphql_url,
                    json={"query": mutation, "variables": {"automaticBasicDiscount": discount_input}},
                    headers=self.headers,
                    timeout=30.0
                )
                
                res_data = response.json()
                
                if "errors" in res_data:
                    print(f"Top-Level GraphQL Errors: {res_data['errors']}")
                    return 0
                    
                data_obj = res_data.get("data") or {}
                mutation_obj = data_obj.get("discountAutomaticBasicCreate") or {}
                
                errors = mutation_obj.get("userErrors") or []
                if errors:
                    print(f"GraphQL Mutation Errors: {errors}")
                    # If title taken, we can consider it success or we should handle it. 
                    # For now just return 0
                    return 0
                    
                node = mutation_obj.get("automaticDiscountNode")
                if node:
                    print(f"Success Created Automatic Discount ID: {node['id']}")
                    return 1  # Return >0 to indicate success
                else:
                    print(f"Failed to create discount: {res_data}")
                    return 0
                    
            except Exception as e:
                print(f"GraphQL Exception: {str(e)}")
                return 0

    async def get_all_automatic_discount_titles(self) -> List[str]:
        titles = []
        query = """
        query($cursor: String) {
          automaticDiscountNodes(first: 250, after: $cursor) {
            pageInfo { hasNextPage }
            edges {
              cursor
              node {
                automaticDiscount {
                  ... on DiscountAutomaticBasic {
                    title
                  }
                }
              }
            }
          }
        }
        """
        has_next = True
        cursor = None
        
        async with httpx.AsyncClient() as client:
            while has_next:
                variables = {"cursor": cursor} if cursor else {}
                try:
                    response = await client.post(
                        self.graphql_url,
                        json={"query": query, "variables": variables},
                        headers=self.headers,
                        timeout=10.0
                    )
                    
                    if response.status_code != 200:
                        break
                        
                    data = response.json().get("data", {}).get("automaticDiscountNodes", {})
                    
                    for edge in data.get("edges", []):
                        discount = edge.get("node", {}).get("automaticDiscount")
                        if discount and "title" in discount:
                            titles.append(discount["title"])
                            
                    has_next = data.get("pageInfo", {}).get("hasNextPage", False)
                    if has_next:
                        cursor = data["edges"][-1]["cursor"]
                        
                except Exception:
                    break
                    
        return titles

    async def get_all_vendors(self) -> List[str]:
        vendors = set()
        query = """
        query($cursor: String) {
          products(first: 250, after: $cursor) {
            pageInfo { hasNextPage }
            edges {
              cursor
              node {
                vendor
              }
            }
          }
        }
        """
        has_next = True
        cursor = None
        
        async with httpx.AsyncClient() as client:
            while has_next:
                variables = {"cursor": cursor} if cursor else {}
                try:
                    response = await client.post(
                        self.graphql_url,
                        json={"query": query, "variables": variables},
                        headers=self.headers,
                        timeout=30.0
                    )
                    
                    if response.status_code != 200:
                        break
                        
                    data = response.json().get("data", {}).get("products", {})
                    
                    for edge in data.get("edges", []):
                        v = edge.get("node", {}).get("vendor")
                        if v:
                            vendors.add(v)
                            
                    has_next = data.get("pageInfo", {}).get("hasNextPage", False)
                    if has_next:
                        cursor = data["edges"][-1]["cursor"]
                        
                except Exception:
                    break
                    
        return list(vendors)

    async def get_vendor_product_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        query = """
        query($cursor: String) {
          products(first: 250, after: $cursor) {
            pageInfo { hasNextPage }
            edges {
              cursor
              node {
                vendor
              }
            }
          }
        }
        """
        has_next = True
        cursor = None

        async with httpx.AsyncClient() as client:
            while has_next:
                variables = {"cursor": cursor} if cursor else {}
                try:
                    response = await client.post(
                        self.graphql_url,
                        json={"query": query, "variables": variables},
                        headers=self.headers,
                        timeout=30.0
                    )

                    if response.status_code != 200:
                        break

                    data = response.json().get("data", {}).get("products", {})

                    for edge in data.get("edges", []):
                        vendor = (edge.get("node", {}) or {}).get("vendor")
                        if vendor:
                            counts[vendor] = counts.get(vendor, 0) + 1

                    has_next = data.get("pageInfo", {}).get("hasNextPage", False)
                    if has_next and data.get("edges"):
                        cursor = data["edges"][-1]["cursor"]

                except Exception:
                    break

        return counts