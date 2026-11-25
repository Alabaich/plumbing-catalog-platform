import sys
import os
from pathlib import Path

# Add services/api to the path so Python can import `app`
sys.path.append(str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from app.dependencies import SessionLocal
from app.services.search_svc import ensure_collection, TypesenseClient

def sync(shop_domain: str):
    collection_name = ensure_collection(shop_domain)
    client = TypesenseClient.get_client()
    
    print(f"Syncing products for {shop_domain} into Typesense collection '{collection_name}'...")
    
    with SessionLocal() as session:
        # Select all products with raw SQL to avoid importing app.models.domain (asyncpg import error)
        result = session.execute(text('SELECT id, handle, "Title", "Vendor" FROM product_db'))
        products = result.fetchall()
        
        docs_to_index = []
        for p in products:
            # p._mapping can be used if it's a row object
            doc = {
                "id": str(p.id),
                "sku": p.handle if p.handle else "",
                "title": getattr(p, 'Title', 'Untitled') if getattr(p, 'Title', None) else "Untitled",
                "vendor": getattr(p, 'Vendor', 'Unknown Vendor') if getattr(p, 'Vendor', None) else "Unknown Vendor",
                "source_type": "saas"
            }
            docs_to_index.append(doc)
            
        if docs_to_index:
            try:
                response = client.collections[collection_name].documents.import_(docs_to_index, {'action': 'upsert'})
                
                success_count = sum(1 for item in response if item.get('success'))
                print(f"Successfully imported {success_count} out of {len(docs_to_index)} products.")
                
                # Print errors if any
                errors = [item for item in response if not item.get('success')]
                if errors:
                    print(f"Encountered {len(errors)} errors. First error: {errors[0]}")
            except Exception as e:
                print(f"Error during import: {e}")
        else:
            print("No products found in the database to sync.")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/sync_typesense.py <shop_domain>")
        sys.exit(1)
    
    shop_domain = sys.argv[1]
    sync(shop_domain)
