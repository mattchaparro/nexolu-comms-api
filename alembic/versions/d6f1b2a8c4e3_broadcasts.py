"""broadcasts y broadcast_recipients: difusiones de Connect (plantilla a un
publico por criterio, ahora o programadas)

Revision ID: d6f1b2a8c4e3
Revises: c3e8a5f2d917
Create Date: 2026-09-26 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd6f1b2a8c4e3'
down_revision: str | None = 'c3e8a5f2d917'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'broadcasts',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('app_id', sa.String(length=64), nullable=False),
        sa.Column('business_id', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('name', sa.String(length=191), nullable=False),
        sa.Column('template_name', sa.String(length=191), nullable=False),
        sa.Column('template_language', sa.String(length=16), nullable=False, server_default='es'),
        sa.Column('template_params', sa.JSON(), nullable=True),
        sa.Column('audience', sa.JSON(), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='draft'),
        sa.Column('scheduled_at', sa.DateTime(), nullable=True),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
        sa.Column('recipients', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_by', sa.String(length=191), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_broadcasts_due', 'broadcasts', ['status', 'scheduled_at'])
    op.create_table(
        'broadcast_recipients',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('broadcast_id', sa.String(length=32), nullable=False),
        sa.Column('contact_id', sa.String(length=32), nullable=False),
        sa.Column('phone', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='pending'),
        sa.Column('wamid', sa.String(length=191), nullable=True),
        sa.Column('error', sa.String(length=300), nullable=True),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('broadcast_id', 'contact_id', name='uq_broadcast_recipient'),
    )
    op.create_index('ix_broadcast_recipients_broadcast_id', 'broadcast_recipients', ['broadcast_id'])
    op.create_index('ix_broadcast_recipients_wamid', 'broadcast_recipients', ['wamid'])


def downgrade() -> None:
    op.drop_table('broadcast_recipients')
    op.drop_index('ix_broadcasts_due', table_name='broadcasts')
    op.drop_table('broadcasts')
