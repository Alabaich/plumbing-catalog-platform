"""
Declarative Base for SQLAlchemy models.

Engine + session construction lives in app/dependencies.py (sync engine used
by FastAPI routes) and in alembic/env.py (engine used during migrations).
Keeping this module side-effect-free means importing a model never opens a
connection or requires a particular driver to be installed.
"""

from sqlalchemy.orm import declarative_base

Base = declarative_base()
