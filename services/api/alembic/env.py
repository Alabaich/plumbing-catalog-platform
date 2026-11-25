"""
Alembic environment for the Plumbing Catalog API.

Responsibilities:
  - Read DATABASE_URL from env so the same config works in dev, CI, and prod.
  - Register every model so Base.metadata is fully populated before autogenerate.
  - Exclude tables this project does NOT own (legacy catalog + Prisma sessions)
    so autogenerate never proposes to drop or alter them.

Workflow:
  - Generate a migration   :  alembic revision --autogenerate -m "describe change"
  - Apply migrations       :  alembic upgrade head   (run on deploy)
  - Mark DB as up-to-date  :  alembic stamp head     (used once during baseline)
"""

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool
from alembic import context

# Make `app` importable when alembic runs from /app inside the container.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.database import Base
from app.models import domain  # noqa: F401  — side-effect: registers models on Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Single source of truth for the connection string.
db_url = os.getenv("DATABASE_URL")
if not db_url:
    raise RuntimeError("DATABASE_URL not set; alembic cannot run")
config.set_main_option("sqlalchemy.url", db_url)

target_metadata = Base.metadata

# Tables owned by other systems — never touch them on autogenerate or upgrade.
# Add to this set if you bring in another externally-managed table.
EXCLUDED_TABLES = {
    "product_db", "variant_db", "Session",
    "parsing_queue", "pricesheets", "shopify_sessions",
}


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table" and name in EXCLUDED_TABLES:
        return False
    return True


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing — useful for review."""
    context.configure(
        url=db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_server_default=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and apply migrations against the live database."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_server_default=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()