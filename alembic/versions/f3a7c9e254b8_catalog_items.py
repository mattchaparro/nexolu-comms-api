"""catalog_items: estado de sincronizacion del catalogo de Meta por
retailer_id (content_hash contra el rate limit, handle del batch, errores)

Revision ID: f3a7c9e254b8
Revises: d8e2c5f019a4
Create Date: 2026-09-15 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f3a7c9e254b8'
down_revision: str | None = 'd8e2c5f019a4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'catalog_items',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('app_id', sa.String(length=64), nullable=False),
        sa.Column('business_channel_id', sa.String(length=32), nullable=True),
        sa.Column('catalog_id', sa.String(length=64), nullable=False),
        sa.Column('retailer_id', sa.String(length=191), nullable=False),
        sa.Column('title', sa.String(length=191), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('price', sa.String(length=32), nullable=False),
        sa.Column('availability', sa.String(length=16), nullable=False),
        sa.Column('image_link', sa.String(length=512), nullable=True),
        sa.Column('link', sa.String(length=512), nullable=True),
        sa.Column('brand', sa.String(length=128), nullable=True),
        sa.Column('content_hash', sa.String(length=64), nullable=False),
        sa.Column('sync_status', sa.String(length=16), nullable=False),
        sa.Column('batch_handle', sa.String(length=255), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('last_synced_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('catalog_id', 'retailer_id', name='uq_catalog_item_identity'),
    )
    op.create_index('ix_catalog_items_app', 'catalog_items', ['app_id'])


def downgrade() -> None:
    op.drop_index('ix_catalog_items_app', table_name='catalog_items')
    op.drop_table('catalog_items')
