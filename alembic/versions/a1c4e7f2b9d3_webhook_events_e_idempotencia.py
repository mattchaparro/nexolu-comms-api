"""webhook_events (persistencia + reintentos) e idempotency_records

Revision ID: a1c4e7f2b9d3
Revises: 3f7b9e2a5c14
Create Date: 2026-09-15 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a1c4e7f2b9d3'
down_revision: str | None = '3f7b9e2a5c14'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'webhook_events',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('app_id', sa.String(length=64), nullable=False),
        sa.Column('event_type', sa.String(length=64), nullable=False),
        sa.Column('phone_number_id', sa.String(length=64), nullable=True),
        sa.Column('payload', sa.Text(), nullable=False),
        sa.Column('signature_valid', sa.Boolean(), nullable=True),
        sa.Column('forward_status', sa.String(length=16), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('next_retry_at', sa.DateTime(), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('received_at', sa.DateTime(), nullable=False),
        sa.Column('delivered_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_webhook_events_retry', 'webhook_events', ['forward_status', 'next_retry_at'])
    op.create_index('ix_webhook_events_app', 'webhook_events', ['app_id', 'received_at'])

    op.create_table(
        'idempotency_records',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('app_id', sa.String(length=64), nullable=False),
        sa.Column('idempotency_key', sa.String(length=191), nullable=False),
        sa.Column('response_body', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('app_id', 'idempotency_key', name='uq_idempotency_app_key'),
    )


def downgrade() -> None:
    op.drop_table('idempotency_records')
    op.drop_index('ix_webhook_events_app', table_name='webhook_events')
    op.drop_index('ix_webhook_events_retry', table_name='webhook_events')
    op.drop_table('webhook_events')
