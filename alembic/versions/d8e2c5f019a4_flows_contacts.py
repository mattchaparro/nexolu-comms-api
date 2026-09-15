"""contacts (tags/fields por telefono), flows y flow_sessions: el motor de
automatizacion de conversaciones de Connect (modelo ManyChat)

Revision ID: d8e2c5f019a4
Revises: b4f6d1a927c3
Create Date: 2026-09-15 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd8e2c5f019a4'
down_revision: str | None = 'b4f6d1a927c3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'contacts',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('app_id', sa.String(length=64), nullable=False),
        sa.Column('business_id', sa.String(length=64), nullable=False),
        sa.Column('phone', sa.String(length=32), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('tags', sa.JSON(), nullable=False),
        sa.Column('fields', sa.JSON(), nullable=False),
        sa.Column('last_inbound_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('app_id', 'business_id', 'phone', name='uq_contact_identity'),
    )
    op.create_index('ix_contacts_app', 'contacts', ['app_id', 'business_id'])

    op.create_table(
        'flows',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('app_id', sa.String(length=64), nullable=False),
        sa.Column('business_id', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('trigger_type', sa.String(length=16), nullable=False),
        sa.Column('trigger_keywords', sa.JSON(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('definition', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('app_id', 'business_id', 'name', name='uq_flow_identity'),
    )
    op.create_index('ix_flows_app', 'flows', ['app_id', 'business_id'])

    op.create_table(
        'flow_sessions',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('flow_id', sa.String(length=32), nullable=False),
        sa.Column('contact_id', sa.String(length=32), nullable=False),
        sa.Column('app_id', sa.String(length=64), nullable=False),
        sa.Column('business_id', sa.String(length=64), nullable=False),
        sa.Column('current_node', sa.String(length=64), nullable=True),
        sa.Column('context', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_flow_sessions_contact', 'flow_sessions', ['contact_id', 'status'])


def downgrade() -> None:
    op.drop_index('ix_flow_sessions_contact', table_name='flow_sessions')
    op.drop_table('flow_sessions')
    op.drop_index('ix_flows_app', table_name='flows')
    op.drop_table('flows')
    op.drop_index('ix_contacts_app', table_name='contacts')
    op.drop_table('contacts')
