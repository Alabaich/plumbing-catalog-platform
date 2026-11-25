from fastapi import APIRouter, Depends, HTTPException, Request, Query
from sqlalchemy import text
from app.dependencies import get_db

router = APIRouter()

@router.get("/product/{sku}")
async def get_storefront_product(
    sku: str,
    request: Request,
    shop: str = Query(..., description="Shopify shop domain"),
    db=Depends(get_db)
):
    """
    Public endpoint for Shopify Storefront widgets to fetch rich data.
    Validates if the requesting shop has an active subscription/access.
    """
    
    # 1. Verify if the shop is allowed (pseudo-code, adapt to your DB structure)
    # is_allowed = verify_shop_access(shop, db)
    # if not is_allowed:
    #     raise HTTPException(status_code=403, detail="Shop not authorized")

    # 2. Find the product handle by SKU
    v_query = text('SELECT "handle" FROM "variant_db" WHERE TRIM("Variant SKU") ILIKE :sku LIMIT 1')
    variant = db.execute(v_query, {"sku": sku.strip()}).mappings().first()
    
    if not variant:
        raise HTTPException(status_code=404, detail="SKU not found in our database")
        
    handle = variant["handle"]
    
    # 3. Fetch rich data from product_db
    p_query = text('''
        SELECT "Title", "Body (HTML)" as description, "Vendor" as vendor,
        all_image_srcs[1] as image_url
        FROM "product_db" 
        WHERE COALESCE("clean slug", "clean_slug", "handle") = :handle
        LIMIT 1
    ''')
    product = db.execute(p_query, {"handle": handle}).mappings().first()
    
    if not product:
        raise HTTPException(status_code=404, detail="Product rich data not found")
        
    return {
        "status": "success",
        "data": {
            "sku": sku,
            "title": product.get("Title"),
            "description": product.get("description"),
            "image_url": product.get("image_url"),
            "vendor": product.get("vendor")
        }
    }