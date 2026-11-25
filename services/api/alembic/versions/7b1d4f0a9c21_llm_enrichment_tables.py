"""llm enrichment tables

Revision ID: 7b1d4f0a9c21
Revises: 2e34c9b926a6
Create Date: 2026-11-25 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '7b1d4f0a9c21'
down_revision: Union[str, None] = '2e34c9b926a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'product_enrichment',
        sa.Column('handle', sa.Text(), nullable=False),
        sa.Column('vendor', sa.String(length=255), nullable=True),
        sa.Column('source_hash', sa.String(length=64), nullable=False),
        sa.Column('content_hash', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('title', sa.Text(), nullable=True),
        sa.Column('html_description', sa.Text(), nullable=True),
        sa.Column('html_specs_table', sa.Text(), nullable=True),
        sa.Column('product_type', sa.String(length=255), nullable=True),
        sa.Column('category', sa.Text(), nullable=True),
        sa.Column('review_reasons', postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column('llm_provider', sa.String(length=32), nullable=True),
        sa.Column('llm_model', sa.String(length=128), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('handle'),
    )
    op.create_index('ix_product_enrichment_status', 'product_enrichment', ['status'])
    op.create_index('ix_product_enrichment_vendor', 'product_enrichment', ['vendor'])

    op.create_table(
        'llm_enrichment_cache',
        sa.Column('content_hash', sa.String(length=64), nullable=False),
        sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('llm_provider', sa.String(length=32), nullable=True),
        sa.Column('llm_model', sa.String(length=128), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('content_hash'),
    )


def downgrade() -> None:
    op.drop_table('llm_enrichment_cache')
    op.drop_index('ix_product_enrichment_vendor', table_name='product_enrichment')
    op.drop_index('ix_product_enrichment_status', table_name='product_enrichment')
    op.drop_table('product_enrichment')
