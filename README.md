# Plumbing Catalog Platform

[![Release](https://github.com/Alabaich/plumbing-catalog-platform/actions/workflows/deploy.yml/badge.svg?branch=main)](https://github.com/Alabaich/plumbing-catalog-platform/actions/workflows/deploy.yml)

## Overview

A data pipeline and Shopify app for plumbing product catalogs. It scrapes manufacturer websites, detects what changed, enriches new and changed products with an LLM, holds doubtful results for human review, and serves the catalog to Shopify stores.

The repository contains:

- **A scraper framework** for a multi-brand catalog (Python, Selenium), run in a loop by an orchestrator. The production scrapers are kept in a private repository. This one ships the template and three example scrapers (`brand_a`, `brand_b`, `brand_c`) whose brand names and URLs come from environment variables.
- **A diff engine** that hashes scraped data so unchanged products cause no database writes.
- **LLM enrichment** as an RQ job: clean description, specification table and category for each new or changed product, with validation, sanitization and a review queue.
- **A FastAPI backend** that the Shopify app and storefront call.
- **A Shopify embedded admin app** (React Router / Remix, Polaris) and a theme app extension.
- **Infrastructure**: Docker Compose, Caddy, Redis, Typesense, Alembic migrations, deploy and rollback scripts, CI.

## Architecture

```
 manufacturer websites (one scraper per brand)
        │
        ▼
 ┌──────────────┐  collect_urls()   ┌────────────────┐
 │   scrapers   │ ────────────────▶ │ parsing_queue  │  one row per product URL
 │  (Selenium)  │ ◀──────────────── │   (Postgres)   │  status: pending → processed / failed
 └──────┬───────┘  scrape_products()└────────────────┘
        │               ▲
        │               │ runs every brand in turn, pauses, repeats
        │        ┌──────┴───────┐
        │        │ orchestrator │ ───▶ Telegram: status and errors
        │        └──────┬───────┘
        ▼               │
 ┌──────────────┐       │ after each brand: enqueue products
 │ diff_engine  │       │ whose hash changed
 │   SHA-256    │       ▼
 └──────┬───────┘  ┌─────────────┐      ┌─────────────────────────────────────┐
        │ changed  │ Redis / RQ  │ ───▶ │ enrichment worker                   │
        │ only     └─────────────┘      │  1. clean HTML, read spec-sheet PDF │
        ▼                               │  2. poison screen (regex, then LLM) │
 ┌──────────────┐                       │  3. LLM → JSON                      │
 │   Postgres   │                       │  4. validate, sanitize, taxonomy    │
 │  product_db  │                       └───────┬───────────────────┬─────────┘
 │  variant_db  │                               │ ok                │ invalid / suspicious
 └──────┬───────┘                               ▼                   ▼
        │                              status = published   status = needs_review
        │                                       │            (Telegram alert, review CLI)
        │        ┌────────────────────┐         │                   │ approve
        ├──────▶ │ product_enrichment │ ◀───────┴───────────────────┘
        │        └─────────┬──────────┘
        │                  │ published rows only
        ▼                  ▼
 ┌───────────────────────────────┐        ┌───────────────────────────┐
 │ Typesense (reindex script)    │        │ FastAPI                   │
 └───────────────────────────────┘        │  /api/v1/products         │
                                          │  /api/v1/storefront       │
                                          │  /proxy/catalog           │
                                          └─────────────┬─────────────┘
                                                        ▼
                                          ┌───────────────────────────┐
                                          │ Shopify embedded app      │
                                          │ + theme app extension     │
                                          └───────────────────────────┘
```

### Key decisions

#### Idempotent ingestion

Every write in the pipeline is an upsert on a natural key, so any step can be re-run or crash halfway without leaving duplicates.

| Table | Key | Statement |
|---|---|---|
| `parsing_queue` | `link` | `ON CONFLICT (link) DO UPDATE SET status = 'pending'` |
| `product_db` | `handle` | `ON CONFLICT (handle) DO UPDATE`, run only when the hash changed |
| `variant_db` | `"Variant SKU"` | `ON CONFLICT ("Variant SKU") DO UPDATE`, run only when the hash changed |
| `product_enrichment` | `handle` | `ON CONFLICT (handle) DO UPDATE` |
| `llm_enrichment_cache` | `content_hash` | `ON CONFLICT (content_hash) DO NOTHING` |

A URL collected twice is still one queue row. Collecting it again sets it back to `pending`, which is how each cycle revisits every product and notices changes. Each product is committed in its own transaction together with its queue status, so a crash loses at most the product in progress.

#### Change detection

`services/scrapers/diff_engine.py` computes a SHA-256 over a canonical form of the scraped fields and stores it in `data_hash` on both `product_db` and `variant_db`.

- Product and variant hashes are separate, so a new finish image updates one variant row and leaves the product alone.
- List fields are sorted before hashing, and `None`, `""` and `[]` are treated as equal. A parser that changes ordering or return type does not cause false updates.
- The product hash includes the sorted list of variant SKUs, so adding or removing a variant counts as a product change.
- Prices are deliberately left out of the variant hash; they are maintained separately.

A second run over an unchanged site produces zero product and variant writes.

#### LLM enrichment only on diffs

Enrichment costs money per product, so it runs only where the diff engine saw a change.

1. After a brand finishes, the orchestrator calls `enqueue_changed(brand)`. It compares each product's `data_hash` with the hash recorded at its last enrichment, using the diff engine's `should_update`.
2. New and changed products go to an RQ queue, capped per brand per cycle by `ENRICH_MAX_PER_BRAND`.
3. The worker cleans the scraped HTML locally (selectolax and ftfy remove scripts, styles and all attributes) and extracts text from the first pages of the spec-sheet PDF (pdfplumber).
4. The model returns JSON with `html_description`, `html_specs_table` and `product_type`. The locally cleaned title (encoding fixed, repeated words removed) is stored with the result. If the product already has a category, the model is asked only to confirm or correct it.

**Cache.** Each request is hashed over the prompts, provider, model and the exact cleaned input. The result is stored in `llm_enrichment_cache` under that hash. A product whose hash changed only because of a new image produces the same request hash and costs nothing. Editing a prompt file or the taxonomy changes the hash, so the cache needs no manual flushing.

**Providers.** `LLM_PROVIDER` selects Gemini or any OpenAI-compatible API. Both are called over HTTPS with `requests`. Without `LLM_API_KEY` nothing is enqueued and the rest of the pipeline runs as before. A Redis outage or a missing table is logged and does not stop the scrape cycle.

**Prompts** live in `services/scrapers/prompts/`:

| File | Purpose |
|---|---|
| `description_clean.txt` | Role, handling of untrusted input, rules for the description |
| `specs_structure.txt` | Rules for the specification table |
| `poison_detect.txt` | Classifier that screens scraped text before it is rewritten |
| `categories.json` | Category taxonomy; `product_type` must be one of its leaf paths |

#### Validation and human review

Scraped pages are untrusted input, and the theme extension inserts descriptions into storefront pages as HTML. A model response is therefore checked before anything is published.

| Check | Reason code |
|---|---|
| Source has no text at all | `empty_source` |
| Regex screen for prompt injection, hidden characters and error or CAPTCHA pages. Runs before any LLM call. | `poison_heuristic:*` |
| LLM classifier marks the text as suspicious, or its answer cannot be parsed | `poison_llm`, `poison_check_invalid_response` |
| Response is not JSON or does not match the pydantic schema | `invalid_json`, `schema:<field>` |
| Response contains scripts, event handlers, links or images | `unsafe_html:*` |
| Description is empty or much shorter than the source | `empty_description`, `description_truncated` |
| Specs field is not a table | `specs_not_a_table` |
| Output contains injection phrases | `poison_in_output:*` |
| `product_type` is not in the taxonomy | `unknown_product_type` |

A response that passes every check is stored with `status = 'published'`. One failed check stores it with `status = 'needs_review'` and the reason codes, and sends a Telegram message to the changes channel. The Typesense reindex and the proxy description endpoint join `product_enrichment` only `WHERE status = 'published'`, so a held result is never served and the scraped description stays in place. The proxy endpoint appends the specification table to the description under a "Technical Specifications" heading.

HTML is always passed through a tag whitelist (nh3) before it is stored, including results held for review. Enrichment never writes to `product_db`.

Provider and network errors are not treated as a verdict about the product. The job raises, RQ retries it twice (after 1 and 5 minutes), and if it still fails the product is picked up again on the next cycle.

Review is done from the command line:

```bash
docker compose -f docker-compose.prod.yml run --rm enrichment-worker python enrichment.py list
docker compose -f docker-compose.prod.yml run --rm enrichment-worker python enrichment.py approve <handle> --product-type "Kitchen Sink Faucets"
docker compose -f docker-compose.prod.yml run --rm enrichment-worker python enrichment.py retry <handle>
```

#### Telegram alerts

`services/scrapers/utils/logger.py` gives every scraper a logger that writes JSON lines to a file, colored text to the console, and every `ERROR` or `CRITICAL` record to Telegram. One bot posts to three chats:

| Channel | Variable | Content |
|---|---|---|
| Errors | `TG_CHAT_ERRORS` | Scraper crashes, failed brands, several failures in a row |
| Status | `TG_CHAT_STATUS` | Cycle start and end, per-brand result |
| Changes | `TG_CHAT_CHANGES` | Enrichment results held for review |

If only `TELEGRAM_CHAT_ID` is set, everything goes to that chat. Without a bot token, alerts are skipped silently.

#### RQ workers

Enrichment runs in its own container (`enrichment-worker`, same image as the scrapers) and consumes the `enrichment` queue in Redis. Scraping is bound by browser time and enrichment by API latency, so they scale separately: add workers without touching the scrape loop. Jobs are idempotent. A job first re-checks the hashes and exits if an earlier duplicate already did the work.

#### Deploy and rollback

- `scripts/deploy.sh` pulls `main`, builds the API and Remix images, runs Alembic migrations, restarts the API and waits for `/health`. If the health check fails it calls the rollback script and exits non-zero. The Remix app is restarted only after the API is healthy.
- `scripts/rollback.sh` checks out the previous commit and rebuilds and restarts the API and Remix app.
- Alembic manages only the tables this app owns. `alembic/env.py` excludes the catalog tables so autogenerate can never propose dropping them.
- `make deploy`, `make rollback`, `make migrate`, `make logs s=<service>` and `make restart s=<service>` wrap the common commands.

### API surface

| Route | Auth | Purpose |
|---|---|---|
| `GET /api/v1/products/` | `X-API-Key` | Paginated product list with an encrypted cursor |
| `GET /api/v1/products/{slug}` | `X-API-Key` | One product with variants |
| `GET /api/v1/products/brands/list` | `X-API-Key` | Brands with product counts and the shop's saved discount |
| `POST /api/v1/products/import` | `X-API-Key` | Import one chunk of a brand's products into the shop as drafts |
| `POST /api/v1/products/discount` | `X-API-Key` | Save a brand discount and re-price the shop's variants (never below MAP) |
| `GET /api/v1/storefront/product/{sku}` | none | Description and image for the theme extension |
| `GET /proxy/catalog/product/{handle}` | signed | Product payload for the Shopify App Proxy |
| `GET /proxy/catalog/product/{handle}/description` | signed | Full description; published enrichment when available |
| `GET /proxy/catalog/collection/{vendor}` | signed | Paginated brand listing |
| `GET /health`, `GET /ready` | none | Health checks |

Proxy requests are verified with HMAC-SHA256 over the query string using the Shopify app secret. The timestamp must be recent, each nonce is accepted once (Redis `SET NX`), the shop must be an active tenant, and the product's brand must be within the tenant's subscribed brands. Responses are cached in Redis for an hour.

Three middlewares wrap the keyed API: API key check, a heuristic anomaly score from request headers (weights and the blocking score are set by `ANTI_SCRAPE_*`), and a Redis rate limiter whose per-minute cap drops as the score rises (`RATE_LIMIT_*`).

### Shopify app

The embedded admin app lists brands from the API, starts and tracks per-brand import jobs, and sets per-brand discounts. Import progress is kept in a JSON file inside the app container and driven by an in-process loop that calls `POST /api/v1/products/import` one product at a time. Shopify sessions are stored in Postgres through Prisma; the API reads the shop's access token from that table.

The theme app extension has three blocks: product enrichment, a collection grid and search.

### Repository layout

```
services/scrapers/          orchestrator, diff engine, enrichment, brand scrapers
  orchestrator.py           main loop: brands in turn, enqueue enrichment
  base_scraper.py           abstract class every brand implements
  diff_engine.py            hashing and change detection
  enrichment.py             enrichment pipeline, RQ job, review CLI
  enrichment_worker.py      RQ worker entry point
  llm_client.py             Gemini / OpenAI-compatible client
  utils/ai_cleaner.py       HTML cleaning, PDF text, sanitizer, poison heuristics
  utils/logger.py           JSON logs and Telegram alerts
  prompts/                  prompt files and taxonomy
  brands/<brand>/           collect and scrape scripts per brand
  tests/                    pytest: diff engine and enrichment (mocked LLM)
services/api/               FastAPI backend, Alembic migrations
  tests/                    pytest: proxy auth, middleware, cursors (fakeredis)
services/remix-app/         Shopify embedded admin app and theme extension
infra/                      Caddy, Redis and Typesense configuration
scripts/                    deploy, rollback, migrate, Typesense reindex, proxy smoke tests
.github/workflows/         ci.yml (lint, tests, builds) and deploy.yml (release, off by default)
```

## Tech stack

| Layer | Technology |
|---|---|
| Scrapers | Python 3.12, Selenium with headless Chrome, BeautifulSoup, psycopg2 |
| Enrichment | Gemini or an OpenAI-compatible API over HTTPS, pydantic, selectolax, ftfy, pdfplumber, nh3 |
| Queue | Redis 7, RQ |
| Database | PostgreSQL on the host, Alembic migrations, SQLAlchemy |
| Search | Typesense 27 |
| API | FastAPI, Redis for rate limits, proxy cache and nonces |
| Shopify app | React Router 7 (Remix), Polaris, Prisma session storage, theme app extension |
| Infrastructure | Docker Compose, Caddy, GitHub Actions (CI and an opt-in SSH deploy) |
| Alerts | Telegram Bot API |

## Getting started

Requirements: Docker with Compose, and PostgreSQL on the host.

> The catalog tables (`product_db`, `variant_db`, `parsing_queue`, `pricesheets`) are not created by the migrations in this repository. Alembic creates only the tenant and enrichment tables. You need a database that already has the catalog tables.

```bash
# 1. Configure
cp .env.example .env            # fill in the CHANGE_ME values

# 2. Build and run migrations (tenant tables, product_enrichment, llm_enrichment_cache)
docker compose -f docker-compose.prod.yml build
bash scripts/migrate.sh

# 3. Start everything: redis, typesense, scrapers, enrichment-worker, api, remix-app
docker compose -f docker-compose.prod.yml up -d

# 4. Watch a scrape cycle
docker compose -f docker-compose.prod.yml logs -f scrapers enrichment-worker
```

To run one cycle and exit, set `RUN_ONCE=true` in `.env`. To rebuild the search index after a cycle, run this on the host:

```bash
pip install psycopg2-binary typesense beautifulsoup4
export DATABASE_URL=postgresql://USER:PASSWORD@localhost:5432/plumbing_products
export TYPESENSE_API_KEY=... TYPESENSE_HOST=127.0.0.1
python scripts/reindex_typesense.py --dry-run     # count and validate, no writes
python scripts/reindex_typesense.py               # upsert into products_v1
```

`docker-compose.yml` starts only Redis and Typesense, for working on a service outside Docker.

### Adding a brand

Brands are discovered automatically: every folder under `services/scrapers/brands/` with an `__init__.py` that defines a `BaseScraper` subclass is run on the next cycle. A folder containing a file named `DISABLED` is skipped.

The repository ships three working examples. Each takes its brand name, site root and category pages from `BRAND_<X>_NAME`, `BRAND_<X>_BASE_URL` and `BRAND_<X>_CATEGORY_URLS`; the selectors and parsing logic are in the code.

| Example | Listing pages | Variants | Also shows |
|---|---|---|---|
| `brand_a` | "Load More" button clicked until it disappears, with a JavaScript click when the button is covered | Finish swatches clicked on the product page | Spec PDFs captured from a new browser tab; crashed items retried with a fresh driver |
| `brand_b` | "Load More" button, cookie banner handled with explicit waits | Up to three option axes, every combination clicked through | Driver restart after repeated failures |
| `brand_c` | Numbered pages (`?currentPage=N`) until a page is empty, with one refresh on timeout | One page per finish, each fetched separately | The shortest of the three; a good starting point |

To add your own:

1. Copy the template and enable it.

   ```bash
   cp -r services/scrapers/brands/_template services/scrapers/brands/acme
   rm services/scrapers/brands/acme/DISABLED
   ```

2. Write `acme_collect_urls.py`. It opens the category pages, gathers product URLs and upserts them into `parsing_queue` on the unique `link` with `status = 'pending'`.

3. Write `acme_scrape_products.py`. It selects the brand's `pending` rows, parses each page, saves through the diff engine (`compute_product_hash`, `compute_variant_hash`, `should_update`) and sets the row to `processed` or `failed`. `brands/brand_c/` is a compact example.

4. Register the brand in `brands/acme/__init__.py`.

   ```python
   from base_scraper import BaseScraper

   class AcmeScraper(BaseScraper):
       BRAND_NAME = "ACME"

       def collect_urls(self):
           from brands.acme.acme_collect_urls import collect_urls
           collect_urls()

       def scrape_products(self):
           from brands.acme.acme_scrape_products import main
           main()
   ```

Keep the brand name and URLs out of the code: read them with `os.getenv`, as the examples do, and add the variables to `.env`.

To switch a brand off without deleting it, `touch services/scrapers/brands/acme/DISABLED`. Remove the file to switch it back on.

Inside a scraper, use the shared logger so errors reach Telegram:

```python
from utils.logger import get_logger

logger = get_logger("ACME", "collect")   # or "scrape"
logger.info("Found 50 URLs")             # file and console
logger.error("Site did not respond")     # file, console and Telegram
```

### Deployment

`.github/workflows/deploy.yml` (Release) runs CI on every push to `main` and on manual runs, then deploys over SSH. It never runs on pull requests.

**Deployment is off by default.** The deploy job is skipped unless the repository variable `DEPLOY_ENABLED` is `true`. With the variable set but a secret missing, the job logs which secret is absent and ends without connecting anywhere.

To turn it on:

1. Set the repository variable `DEPLOY_ENABLED` to `true`.
2. Add these secrets, on the repository or on the `production` environment:

   | Secret | Content |
   |---|---|
   | `DEPLOY_HOST` | Server hostname |
   | `DEPLOY_USER` | SSH user |
   | `DEPLOY_SSH_KEY` | Private key of that user |
   | `DEPLOY_KNOWN_HOSTS` | The server's `known_hosts` line, from `ssh-keyscan -H <host>` verified against the server's real key |
   | `DEPLOY_PATH` | Path of the checkout on the server |

3. Optional: add required reviewers to the `production` environment in the repository settings to approve each deploy by hand.

The job connects with strict host key checking against `DEPLOY_KNOWN_HOSTS`, runs `git pull --ff-only` in `DEPLOY_PATH` and then `bash scripts/deploy.sh`. Only one deploy runs at a time, and a running deploy is never cancelled by a newer push.

Use a dedicated deploy user on the server, not root and not a personal account. It needs write access to the checkout and permission to run Docker Compose, and nothing else. Give it its own key pair that is used only by this workflow.

**Rolling back.** The workflow does not roll back. If a deploy fails, the job fails and its log points here. On the server:

```bash
cd <DEPLOY_PATH>
bash scripts/rollback.sh
```

`scripts/deploy.sh` itself calls `rollback.sh` when the API fails its health check after a restart; any other failure leaves the server as it was at the failing step.

## Environment

All containers read the root `.env`. `.env.example` lists every variable with a placeholder.

| Variable | Used by | Purpose |
|---|---|---|
| `DATABASE_URL` | all | Postgres connection string |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `DB_HOST`, `DB_PORT` | scrapers | Used when `DATABASE_URL` is empty |
| `REDIS_URL` | api, scrapers | Rate limits, proxy cache and nonces, RQ queue |
| `TYPESENSE_API_KEY`, `TYPESENSE_HOST`, `TYPESENSE_PORT`, `TYPESENSE_PROTOCOL` | typesense, reindex | Search index |
| `TELEGRAM_BOT_TOKEN`, `TG_CHAT_ERRORS`, `TG_CHAT_STATUS`, `TG_CHAT_CHANGES`, `TELEGRAM_CHAT_ID` | scrapers | Alerts |
| `PAUSE_BETWEEN_BRANDS`, `PAUSE_BETWEEN_CYCLES`, `RUN_ONCE` | scrapers | Orchestrator pacing |
| `LLM_PROVIDER` | scrapers | `gemini` (default) or `openai` |
| `LLM_API_KEY` | scrapers | Empty means enrichment is skipped |
| `LLM_MODEL` | scrapers | Model id; required for enrichment |
| `LLM_BASE_URL`, `LLM_TIMEOUT` | scrapers | Endpoint override and request timeout |
| `LLM_POISON_CHECK` | scrapers | `false` turns off the LLM classifier; the regex screen stays on |
| `ENRICH_QUEUE`, `ENRICH_MAX_PER_BRAND` | scrapers | Queue name and per-brand cap per cycle |
| `BRAND_A_NAME`, `BRAND_B_NAME`, `BRAND_C_NAME` | scrapers | Brand name of each example scraper; stored as the vendor and used in logs and alerts |
| `BRAND_A_BASE_URL`, `BRAND_B_BASE_URL`, `BRAND_C_BASE_URL` | scrapers | Site root, prepended to relative product links |
| `BRAND_A_CATEGORY_URLS`, `BRAND_B_CATEGORY_URLS`, `BRAND_C_CATEGORY_URLS` | scrapers | Comma-separated category listing pages to collect from |
| `API_KEY`, `BACKEND_API_KEY` | api, remix-app | Shared key sent as `X-API-Key`; both must hold the same value |
| `API_SIGNING_SECRET`, `CURSOR_SECRET` | api | Response signature and cursor encryption. The API refuses to start without `API_SIGNING_SECRET` and `TYPESENSE_API_KEY`. |
| `API_ENV` | api | `production` turns off the OpenAPI docs |
| `CORS_ORIGINS`, `CORS_ORIGIN_REGEX` | api | Allowed origins |
| `ANTI_SCRAPE_WEIGHT_*`, `ANTI_SCRAPE_BLOCK_SCORE` | api | Score added per suspicious request signal, and the score that blocks a request |
| `RATE_LIMIT_*_PER_MIN`, `RATE_LIMIT_LOW_SCORE`, `RATE_LIMIT_MEDIUM_SCORE` | api | Requests per minute per tier, and the scores that select a tier |
| `PROXY_TIMESTAMP_TTL`, `PROXY_NONCE_TTL`, `REDIS_PROXY_CACHE_TTL` | api | Proxy signature window and cache lifetime |
| `SHOPIFY_API_KEY`, `SHOPIFY_API_SECRET`, `SCOPES`, `SHOPIFY_APP_URL` | remix-app, api | Shopify app credentials |
| `API_BASE_URL` | remix-app | Where the admin app reaches the API |
| `API_DOMAIN`, `ADMIN_DOMAIN` | caddy | Public hostnames |

## Tests

Two pytest suites run without network access, a database, Redis or an LLM key.

```bash
# API: 69 tests
cd services/api
pip install -r requirements-dev.txt
python -m pytest tests -q

# Scrapers: 106 tests
cd services/scrapers
pip install poetry && poetry install --no-root
poetry run python -m pytest tests -q
```

| Suite | Covers | How dependencies are replaced |
|---|---|---|
| `services/api/tests` | App Proxy signature (valid, changed parameter, expired timestamp, replayed nonce), tenant and brand scope, anti-scrape scoring and blocking, rate-limit tiers and 429, cursor encryption and forgery, `X-API-Key` middleware, `/health` and `/ready` | Redis by fakeredis, the database session by an in-memory fake, environment set in `tests/conftest.py` |
| `services/scrapers/tests` | Diff engine hashing and `should_update`; enrichment validation, sanitization, poison detection, routing to `needs_review`, cache, change selection | The LLM by a scripted fake |

Brand scrapers have no parsing tests: their selectors depend on live third-party pages.

`scripts/test_proxy/` is a different kind of check. It is a smoke test that sends signed requests to a running API with a real database and Redis, to confirm a deployment works end to end. Run it by hand with `SHOPIFY_API_SECRET` and the `PROXY_TEST_*` variables set:

```bash
python scripts/run_proxy_tests.py
```

Lint is `ruff check` with the error-only rule set in `ruff.toml`.

### CI

`.github/workflows/ci.yml` runs on every pull request, and on every push to `main` through the Release workflow.

| Job | What it does |
|---|---|
| `lint` | `ruff check` and a byte-compile of the API, scrapers and scripts |
| `api-tests` | pytest for `services/api` |
| `scraper-tests` | pytest for `services/scrapers` |
| `remix` | `npm ci`, Prisma client generation, `tsc --noEmit`, build |
| `docker` | Builds the API, scrapers and Remix images without pushing them |

## Roadmap

- Add a migration for the catalog tables (`product_db`, `variant_db`, `parsing_queue`, `pricesheets`) so a new environment can be created from this repository alone.
- Add search and collection endpoints to the storefront API, backed by the Typesense index. The theme extension's search and collection blocks already call them.
- Run the Typesense reindex as an RQ job after each scrape cycle; today `scripts/reindex_typesense.py` is run by hand.
- Make the theme extension's API host a block setting; today it is a constant in the JavaScript assets.
- Resolve the tenant per shop in the keyed API, so rate limits and brand subscriptions apply per store; today every request is treated as one tenant.
- Mount `app/routers/discounts.py` (automatic Shopify discounts) and `app/middleware/logging.py` (JSON request logs and API alerts) in `main.py`.
- Add a review screen for held enrichment results to the admin app; today review is a CLI.
