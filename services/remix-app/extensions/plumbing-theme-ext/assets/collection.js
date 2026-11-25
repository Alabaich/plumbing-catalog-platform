/**
 * collection.js — Plumbing Catalog Collection Page Widget
 *
 * Fetches products for the current Shopify collection from our API
 * (Typesense fast path, Postgres fallback) and renders a product grid
 * inside the `.saas-collection-grid` container placed by the merchant
 * via Theme Editor.
 *
 * URL pattern:
 *   GET /api/v1/storefront/collection/{handle}?shop=...&page=1&per_page=24
 */

(function () {
  const API_BASE = "https://api.example.com/api/v1/storefront";

  const grid = document.querySelector(".saas-collection-grid");
  if (!grid) return;

  // Read from data attributes set by Liquid — reliable in all environments
  const collectionHandle = grid.dataset.collectionHandle;
  const shopDomain =
    grid.dataset.shopDomain ||          // from Liquid: shop.permanent_domain
    window.Shopify?.shop ||             // fallback: Shopify JS global
    window.location.hostname;           // last resort

  console.log("[SaaS Collection] handle:", collectionHandle, "| shop:", shopDomain);

  if (!collectionHandle || !shopDomain) {
    console.warn("[SaaS Collection] Missing handle or shop domain. Is this block on a Collection page?");
    return;
  }

  // ─── State ───────────────────────────────────────────────────────────────
  let currentPage = 1;
  let isLoading = false;
  let hasNext = false;

  // ─── Render helpers ───────────────────────────────────────────────────────

  function renderSkeleton(count) {
    return Array.from({ length: count })
      .map(
        () => `
      <div class="saas-card saas-card--skeleton">
        <div class="saas-card__image-wrap saas-skeleton"></div>
        <div class="saas-card__body">
          <div class="saas-skeleton saas-skeleton--title"></div>
          <div class="saas-skeleton saas-skeleton--vendor"></div>
        </div>
      </div>`
      )
      .join("");
  }

  function renderCard(product) {
    const imgSrc = product.image_url
      ? `<img src="${escapeHtml(product.image_url)}" alt="${escapeHtml(product.title)}" loading="lazy" />`
      : `<div class="saas-card__no-image">No Image</div>`;

    return `
      <a class="saas-card" href="${escapeHtml(product.url)}" title="${escapeHtml(product.title)}">
        <div class="saas-card__image-wrap">
          ${imgSrc}
        </div>
        <div class="saas-card__body">
          <p class="saas-card__title">${escapeHtml(product.title)}</p>
          <p class="saas-card__vendor">${escapeHtml(product.vendor || "")}</p>
        </div>
      </a>`;
  }

  function renderError(message) {
    return `<p class="saas-collection-error">${escapeHtml(message)}</p>`;
  }

  function renderLoadMore() {
    return `
      <div class="saas-load-more-wrap">
        <button class="saas-load-more-btn" id="saas-load-more">
          Load more products
        </button>
      </div>`;
  }

  function escapeHtml(str) {
    if (!str) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // ─── API call ─────────────────────────────────────────────────────────────

  async function fetchProducts(page) {
    const url = new URL(
      `${API_BASE}/collection/${encodeURIComponent(collectionHandle)}`
    );
    url.searchParams.set("shop", shopDomain);
    url.searchParams.set("page", page);
    url.searchParams.set("per_page", "24");

    const res = await fetch(url.toString());
    if (!res.ok) throw new Error(`API error: ${res.status}`);
    return res.json();
  }

  // ─── Init / Load ─────────────────────────────────────────────────────────

  async function loadPage(page) {
    if (isLoading) return;
    isLoading = true;

    if (page === 1) {
      grid.innerHTML = renderSkeleton(8);
    } else {
      // Append skeleton below existing cards
      const skeletonWrap = document.createElement("div");
      skeletonWrap.classList.add("saas-skeleton-append");
      skeletonWrap.innerHTML = renderSkeleton(8);
      grid.insertBefore(skeletonWrap, document.getElementById("saas-load-more")?.parentElement || null);
    }

    try {
      const json = await fetchProducts(page);

      // Remove skeleton
      grid.querySelectorAll(".saas-card--skeleton, .saas-skeleton-append").forEach((el) => el.remove());
      // Remove old load-more button
      document.querySelector(".saas-load-more-wrap")?.remove();

      const products = json.data || [];
      if (products.length === 0 && page === 1) {
        grid.innerHTML = renderError("No products found for this collection.");
        return;
      }

      const fragment = document.createDocumentFragment();
      const wrapper = document.createElement("div");
      wrapper.innerHTML = products.map(renderCard).join("");
      while (wrapper.firstChild) fragment.appendChild(wrapper.firstChild);
      grid.appendChild(fragment);

      hasNext = json.pagination?.has_next ?? false;
      currentPage = page;

      if (hasNext) {
        const loadMoreEl = document.createElement("div");
        loadMoreEl.innerHTML = renderLoadMore();
        grid.after(loadMoreEl.firstElementChild);
        document
          .getElementById("saas-load-more")
          ?.addEventListener("click", () => loadPage(currentPage + 1));
      }
    } catch (err) {
      console.error("[SaaS Collection] Fetch failed:", err);
      grid.querySelectorAll(".saas-card--skeleton, .saas-skeleton-append").forEach((el) => el.remove());
      if (page === 1) {
        grid.innerHTML = renderError("Could not load products. Please try again later.");
      }
    } finally {
      isLoading = false;
    }
  }

  // Kick off first page
  loadPage(1);
})();
