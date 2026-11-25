from typing import Optional
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_db, require_tenant

router = APIRouter()

class DiscountRequest(BaseModel):
    vendor: str
    discount: float
    shop: str
    duration_days: Optional[int] = None

@router.post("")
async def apply_discount(
    payload: DiscountRequest, 
    db=Depends(get_db),
    tenant=Depends(require_tenant)
):
    from app.services.tenant_svc import TenantService
    from app.services.shopify_svc import ShopifyClient

    allowed_brands = tenant.get("subscribed_brands")
    
    if allowed_brands and payload.vendor not in allowed_brands:
        return {"status": "error", "message": "Unauthorized brand"}

    tenant_svc = TenantService()
    access_token = tenant_svc.get_shopify_credentials(payload.shop)

    if not access_token:
        raise HTTPException(status_code=401, detail="Shop session not found")

    shopify = ShopifyClient(shop_url=payload.shop, access_token=access_token)
    
    updated_count = await shopify.apply_brand_discount(
        payload.vendor, 
        payload.discount,
        payload.duration_days
    )
    
    if updated_count == 0:
        return {"status": "error", "message": "Failed to create discount or no products found"}
        
    return {"status": "success", "updated_variants": updated_count}
