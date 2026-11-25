import os
from pathlib import Path
from dotenv import load_dotenv
from pydantic_settings import BaseSettings
from typing import Optional

# 1. Manually find and load the .env file
# We look for it in the same directory as this file's parent (services/api/.env)
current_file = Path(__file__).resolve()
env_path = current_file.parent.parent / ".env"

print(f"--- DEBUG: Loading .env from: {env_path} ---")
load_dotenv(dotenv_path=env_path)

class Settings(BaseSettings):
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")
    SESSION_DB_URL: str = os.getenv("SESSION_DB_URL", "")

    API_KEY: str = os.getenv("API_KEY", "")
    BACKEND_API_KEY: str = os.getenv("BACKEND_API_KEY", "")
    API_ENV: str = os.getenv("API_ENV", "development")

    # Shopify
    SHOPIFY_API_VERSION: str = "2026-07"
    SHOPIFY_API_SECRET: str = os.getenv("SHOPIFY_API_SECRET", "")
    API_SIGNING_SECRET: str = os.getenv("API_SIGNING_SECRET", "")
    CURSOR_SECRET: str = os.getenv("CURSOR_SECRET", "")  # encrypts pagination cursors

    # Redis
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # Proxy security & caching (Phase 0.2)
    PROXY_TIMESTAMP_TTL: int = int(os.getenv("PROXY_TIMESTAMP_TTL", "60"))   # max HMAC age, seconds
    PROXY_NONCE_TTL: int = int(os.getenv("PROXY_NONCE_TTL", "120"))           # nonce TTL ≥ TTL + clock skew
    REDIS_PROXY_CACHE_TTL: int = int(os.getenv("REDIS_PROXY_CACHE_TTL", "3600"))  # product cache, 1 hour

    # Anti-scrape scoring and rate limits. The defaults are neutral examples;
    # production values are set in the private .env.
    ANTI_SCRAPE_WEIGHT_SCRIPT_UA: float = float(os.getenv("ANTI_SCRAPE_WEIGHT_SCRIPT_UA", "0.5"))
    ANTI_SCRAPE_WEIGHT_HEADLESS_UA: float = float(os.getenv("ANTI_SCRAPE_WEIGHT_HEADLESS_UA", "0.5"))
    ANTI_SCRAPE_WEIGHT_NO_REFERER: float = float(os.getenv("ANTI_SCRAPE_WEIGHT_NO_REFERER", "0.5"))
    ANTI_SCRAPE_WEIGHT_FOREIGN_REFERER: float = float(os.getenv("ANTI_SCRAPE_WEIGHT_FOREIGN_REFERER", "0.5"))
    ANTI_SCRAPE_WEIGHT_MISSING_HEADERS: float = float(os.getenv("ANTI_SCRAPE_WEIGHT_MISSING_HEADERS", "0.5"))
    ANTI_SCRAPE_BLOCK_SCORE: float = float(os.getenv("ANTI_SCRAPE_BLOCK_SCORE", "1.0"))
    RATE_LIMIT_NORMAL_PER_MIN: int = int(os.getenv("RATE_LIMIT_NORMAL_PER_MIN", "60"))
    RATE_LIMIT_LOW_PER_MIN: int = int(os.getenv("RATE_LIMIT_LOW_PER_MIN", "30"))
    RATE_LIMIT_MEDIUM_PER_MIN: int = int(os.getenv("RATE_LIMIT_MEDIUM_PER_MIN", "10"))
    RATE_LIMIT_LOW_SCORE: float = float(os.getenv("RATE_LIMIT_LOW_SCORE", "0.25"))
    RATE_LIMIT_MEDIUM_SCORE: float = float(os.getenv("RATE_LIMIT_MEDIUM_SCORE", "0.5"))

    # Typesense
    TYPESENSE_HOST: str = os.getenv("TYPESENSE_HOST", "typesense")
    TYPESENSE_PORT: str = os.getenv("TYPESENSE_PORT", "8108")
    TYPESENSE_API_KEY: str = os.getenv("TYPESENSE_API_KEY", "")

    model_config = {"extra": "ignore"}

settings = Settings()

# Secrets with no safe default: refuse to start without them.
_REQUIRED_SECRETS = ("API_SIGNING_SECRET", "TYPESENSE_API_KEY")
_missing = [name for name in _REQUIRED_SECRETS if not getattr(settings, name)]
if _missing:
    raise RuntimeError(
        "Missing required environment variable(s): " + ", ".join(_missing)
        + ". Set them in .env (see .env.example)."
    )

# 2. Final check to stop the app if critical variables are empty
if not settings.DATABASE_URL or not settings.API_KEY:
    print("--- CRITICAL ERROR: Environment variables NOT loaded! ---")
    print(f"DATABASE_URL found: {bool(settings.DATABASE_URL)}")
    print(f"API_KEY found: {bool(settings.API_KEY)}")

from functools import lru_cache

@lru_cache()
def get_settings() -> Settings:
    return settings