document.addEventListener("DOMContentLoaded", () => {
    const enrichmentBlocks = document.querySelectorAll(".saas-product-enrichment");
    
    // Get the shop domain from Shopify object or hostname
    const shopDomain = window.Shopify?.shop || window.location.hostname;

    enrichmentBlocks.forEach(block => {
        const sku = block.getAttribute("data-sku");
        if (sku) {
            block.innerHTML = `<div style="text-align:center; padding: 20px;">Loading rich data...</div>`;
            
            // Pass the shop domain to the API for authorization check
            const apiUrl = `https://api.example.com/api/v1/storefront/product/${encodeURIComponent(sku)}?shop=${encodeURIComponent(shopDomain)}`;
            
            fetch(apiUrl)
                .then(response => {
                    // If backend returns 403 (Forbidden) or 404, throw error to hide block
                    if (!response.ok) throw new Error(`API returned ${response.status}`);
                    return response.json();
                })
                .then(json => {
                    if (json.status === "success" && json.data) {
                        const data = json.data;
                        block.innerHTML = `
                            <div class="saas-enriched-content" style="margin-top: 20px;">
                                ${data.image_url ? `<img src="${data.image_url}" alt="${data.title}" style="max-width: 100%; height: auto; border-radius: 8px; margin-bottom: 20px;" />` : ''}
                                <div class="saas-description">
                                    ${data.description || ''}
                                </div>
                            </div>
                        `;
                    } else {
                        throw new Error("Invalid data payload");
                    }
                })
                .catch(error => {
                    console.warn("[SaaS Enrichment] Skipped:", error.message);
                    // Hide the block entirely if the store is not allowed or product not found
                    block.style.display = 'none';
                });
        }
    });
});