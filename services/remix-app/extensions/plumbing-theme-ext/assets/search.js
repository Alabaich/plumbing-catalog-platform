/**
 * search.js — Plumbing Catalog Hybrid Search Widget
 *
 * Strategy: hook into the EXISTING Shopify search input on the page
 * (input[name="q"], input[type="search"]) rather than rendering our own.
 * This makes the widget theme-agnostic — it works on Dawn, Impulse, any theme.
 *
 * Behaviour:
 *   - Intercepts keystrokes on the native Shopify search field(s)
 *   - Shows a dropdown of our SaaS products below the native input
 *   - Does NOT replace or hide native Shopify search results
 *   - Dropdown dismissed on Escape or click-outside
 */

(function () {
  const API_BASE = "https://api.example.com/api/v1/storefront";
  const DEBOUNCE_MS = 280;
  const MIN_QUERY_LEN = 2;

  const shopDomain =
    window.Shopify?.shop || window.location.hostname;

  // ─── Find native Shopify search inputs ───────────────────────────────────
  // Themes use different selectors; try the most common ones.
  const NATIVE_SELECTORS = [
    'input[name="q"][type="search"]',
    'input[name="q"]',
    'input[type="search"]',
    "#Search-In-Modal",
    "#Search-In-Template",
  ];

  function findSearchInputs() {
    const found = new Set();
    for (const sel of NATIVE_SELECTORS) {
      document.querySelectorAll(sel).forEach((el) => found.add(el));
    }
    return [...found];
  }

  // ─── Debounce helper ─────────────────────────────────────────────────────
  function debounce(fn, ms) {
    let timer;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), ms);
    };
  }

  // ─── Dropdown ─────────────────────────────────────────────────────────────

  function createDropdown(anchorInput) {
    const existing = document.getElementById("saas-search-dropdown");
    if (existing) existing.remove();

    const dropdown = document.createElement("div");
    dropdown.id = "saas-search-dropdown";
    dropdown.className = "saas-search-dropdown";

    // Position below the input
    const rect = anchorInput.getBoundingClientRect();
    const scrollTop = window.scrollY || document.documentElement.scrollTop;
    dropdown.style.cssText = `
      position: absolute;
      top: ${rect.bottom + scrollTop}px;
      left: ${rect.left}px;
      width: ${rect.width}px;
      z-index: 99999;
    `;

    document.body.appendChild(dropdown);
    return dropdown;
  }

  function hideDropdown() {
    const d = document.getElementById("saas-search-dropdown");
    if (d) d.remove();
  }

  function renderResults(dropdown, items, query) {
    if (!items.length) {
      dropdown.innerHTML = `<div class="saas-search-empty">No products found for "<strong>${escapeHtml(query)}</strong>"</div>`;
      return;
    }

    dropdown.innerHTML = `
      <ul class="saas-search-list" role="listbox">
        ${items
          .map(
            (item) => `
          <li class="saas-search-item" role="option">
            <a href="https://${shopDomain}/products/${escapeHtml(item.sku || "")}" class="saas-search-link">
              <div class="saas-search-thumb">
                ${
                  item.image_url
                    ? `<img src="${escapeHtml(item.image_url)}" alt="${escapeHtml(item.title)}" />`
                    : `<div class="saas-search-thumb--empty"></div>`
                }
              </div>
              <div class="saas-search-meta">
                <span class="saas-search-title">${escapeHtml(item.title)}</span>
                <span class="saas-search-vendor">${escapeHtml(item.vendor || "")}</span>
              </div>
              ${item.source_type === "saas" ? '<span class="saas-search-badge">Our Catalog</span>' : ""}
            </a>
          </li>`
          )
          .join("")}
      </ul>`;
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

  async function searchProducts(query) {
    const url = `${API_BASE}/search?q=${encodeURIComponent(query)}&shop=${encodeURIComponent(shopDomain)}`;
    const res = await fetch(url);
    if (!res.ok) throw new Error(`API ${res.status}`);
    const json = await res.json();
    return json.data || [];
  }

  // ─── Attach to a single input ─────────────────────────────────────────────

  function attachToInput(input) {
    if (input.dataset.saasAttached) return;
    input.dataset.saasAttached = "1";

    const doSearch = debounce(async (query) => {
      if (query.length < MIN_QUERY_LEN) {
        hideDropdown();
        return;
      }

      let dropdown = document.getElementById("saas-search-dropdown");
      if (!dropdown) {
        dropdown = createDropdown(input);
        dropdown.innerHTML = '<div class="saas-search-loading">Searching…</div>';
      } else {
        dropdown.innerHTML = '<div class="saas-search-loading">Searching…</div>';
      }

      try {
        const items = await searchProducts(query);
        renderResults(dropdown, items, query);
      } catch (err) {
        console.warn("[SaaS Search] Error:", err);
        hideDropdown();
      }
    }, DEBOUNCE_MS);

    input.addEventListener("input", (e) => doSearch(e.target.value.trim()));

    input.addEventListener("keydown", (e) => {
      if (e.key === "Escape") hideDropdown();
    });
  }

  // ─── Click outside to close ───────────────────────────────────────────────

  document.addEventListener("click", (e) => {
    if (!e.target.closest("#saas-search-dropdown") && !e.target.closest('input[name="q"]')) {
      hideDropdown();
    }
  });

  // ─── Re-position on resize ───────────────────────────────────────────────
  window.addEventListener("resize", hideDropdown);

  // ─── Init ─────────────────────────────────────────────────────────────────
  // Run immediately + watch for dynamically injected inputs (modals, drawers)

  function init() {
    findSearchInputs().forEach(attachToInput);
  }

  // Some themes inject search inputs after DOMContentLoaded (modal)
  init();
  document.addEventListener("DOMContentLoaded", init);

  // MutationObserver for themes that dynamically show search modals
  const observer = new MutationObserver(() => {
    findSearchInputs().forEach(attachToInput);
  });
  observer.observe(document.body, { childList: true, subtree: true });
})();
