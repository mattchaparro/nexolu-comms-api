"""comms_apps y provider_credentials: apps y credenciales persistidas en BD

Revision ID: 3f7b9e2a5c14
Revises: 8d1f8ddc4f41
Create Date: 2026-08-27 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

import nexolu_comms_api.core.security.crypto
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '3f7b9e2a5c14'
down_revision: str | None = '8d1f8ddc4f41'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'comms_apps',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('app_id', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('api_key', nexolu_comms_api.core.security.crypto.EncryptedString(255), nullable=False),
        sa.Column('api_key_hash', sa.String(length=64), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('app_id'),
        sa.UniqueConstraint('api_key_hash'),
    )
    op.create_table(
        'provider_credentials',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('app_id', sa.String(length=32), nullable=False),
        sa.Column('provider_slug', sa.String(length=32), nullable=False),
        sa.Column('config', sa.JSON(), nullable=False),
        sa.Column('secrets', nexolu_comms_api.core.security.crypto.EncryptedJSON(4000), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['app_id'], ['comms_apps.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('app_id', 'provider_slug', name='uq_provider_credential_app_provider'),
    )
    op.create_index('ix_provider_credentials_app_id', 'provider_credentials', ['app_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_provider_credentials_app_id', table_name='provider_credentials')
    op.drop_table('provider_credentials')
    op.drop_table('comms_apps')
