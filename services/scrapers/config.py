"""
Scraper configuration — all env vars, DB connection, brand registry.
"""

import os

# ---------------------------------------------------------------------------
# Database (Postgres on HOST, not in Docker)
# ---------------------------------------------------------------------------
DB_HOST = os.getenv("DB_HOST", "host.docker.internal")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("POSTGRES_DB", "plumbing_products")
DB_USER = os.getenv("POSTGRES_USER", "plumbing_app")
DB_PASS = os.getenv("POSTGRES_PASSWORD", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")

# ---------------------------------------------------------------------------
# Redis (RQ job queue for LLM enrichment)
# ---------------------------------------------------------------------------
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

# ---------------------------------------------------------------------------
# Telegram alerts — 3 channels
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# Channel 1: Scraper errors (crashes, failed brands)
TG_CHAT_ERRORS = os.getenv("TG_CHAT_ERRORS", "")

# Channel 2: Scraper status (cycle started/finished, stats)
TG_CHAT_STATUS = os.getenv("TG_CHAT_STATUS", "")

# Channel 3: New products / changes found
TG_CHAT_CHANGES = os.getenv("TG_CHAT_CHANGES", "")

# Fallback: if only one CHAT_ID set, use it for everything
TG_CHAT_FALLBACK = os.getenv("TELEGRAM_CHAT_ID", "")

# ---------------------------------------------------------------------------
# Orchestrator settings
# ---------------------------------------------------------------------------
# Pause between brands (seconds) — be polite to manufacturer sites
PAUSE_BETWEEN_BRANDS = int(os.getenv("PAUSE_BETWEEN_BRANDS", "30"))

# Pause between full cycles (seconds) — wait before starting over
PAUSE_BETWEEN_CYCLES = int(os.getenv("PAUSE_BETWEEN_CYCLES", "3600"))  # 1 hour

# Run continuously or single cycle
RUN_ONCE = os.getenv("RUN_ONCE", "false").lower() == "true"

# ---------------------------------------------------------------------------
# Chrome / Selenium
# ---------------------------------------------------------------------------
CHROME_HEADLESS = os.getenv("CHROME_HEADLESS", "true").lower() == "true"
PAGE_LOAD_TIMEOUT = int(os.getenv("PAGE_LOAD_TIMEOUT", "20"))

# ---------------------------------------------------------------------------
# LLM enrichment (optional — without LLM_API_KEY the step is skipped)
# ---------------------------------------------------------------------------
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").strip().lower()   # "gemini" | "openai"
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")                         # OpenAI-compatible gateways
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "120"))

# Second, cheaper LLM call that classifies scraped text before it is rewritten
LLM_POISON_CHECK = os.getenv("LLM_POISON_CHECK", "true").lower() == "true"

# RQ queue the orchestrator pushes changed products to
ENRICH_QUEUE = os.getenv("ENRICH_QUEUE", "enrichment")

# Cap on products enqueued per brand per cycle — bounds LLM spend on a first run
ENRICH_MAX_PER_BRAND = int(os.getenv("ENRICH_MAX_PER_BRAND", "200"))
