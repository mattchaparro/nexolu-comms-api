"""business_channels (numero propio por negocio via Embedded Signup) +
enlace business_channel_id en webhook_events

Revision ID: c7d2f8a341e5
Revises: a1c4e7f2b9d3
Create Date: 2026-09-15 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

import nexolu_comms_api.core.security.crypto
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c7d2f8a341e5'
down_revision: str | None = 'a1c4e7f2b9d3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'business_channels',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('app_id', sa.String(length=64), nullable=False),
        sa.Column('business_id', sa.String(length=64), nullable=False),
        sa.Column('waba_id', sa.String(length=64), nullable=False),
        sa.Column('phone_number_id', sa.String(length=64), nullable=False),
        sa.Column('display_phone_number', sa.String(length=32), nullable=True),
        sa.Column('access_token', nexolu_comms_api.core.security.crypto.EncryptedString(length=1024), nullable=False),
        sa.Column('pin', nexolu_comms_api.core.security.crypto.EncryptedString(length=255), nullable=True),
        sa.Column('catalog_id', sa.String(length=64), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('connected_at', sa.DateTime(), nullable=True),
        sa.Column('disconnected_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('app_id', 'business_id', name='uq_business_channel_app_business'),
    )
    op.create_index('ix_business_channels_phone', 'business_channels', ['phone_number_id'])

    op.add_column('webhook_events', sa.Column('business_channel_id', sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column('webhook_events', 'business_channel_id')
    op.drop_index('ix_business_channels_phone', table_name='business_channels')
    op.drop_table('business_channels')
