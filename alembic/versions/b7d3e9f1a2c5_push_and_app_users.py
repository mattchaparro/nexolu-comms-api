"""push_subscriptions (Web Push al celular de quien atiende el chat) y
usuarios que vienen de otra app: panel_users.origin_*, el pase de un solo
uso, y panel_memberships.business_id (membresia de UN negocio de la app)

Revision ID: b7d3e9f1a2c5
Revises: e1f5a8c3d726
Create Date: 2026-09-24 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b7d3e9f1a2c5'
down_revision: str | None = 'e1f5a8c3d726'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('panel_users', sa.Column('origin_app_id', sa.String(length=64), nullable=True))
    op.add_column('panel_users', sa.Column('origin_user_ref', sa.String(length=64), nullable=True))
    op.add_column('panel_users', sa.Column('login_ticket_hash', sa.String(length=64), nullable=True))
    op.add_column('panel_users', sa.Column('login_ticket_expires_at', sa.DateTime(), nullable=True))
    op.create_index('ix_panel_users_login_ticket_hash', 'panel_users', ['login_ticket_hash'])

    op.add_column(
        'panel_memberships',
        sa.Column('business_id', sa.String(length=64), nullable=False, server_default=''),
    )

    op.create_table(
        'push_subscriptions',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('user_email', sa.String(length=191), nullable=False),
        sa.Column('endpoint', sa.String(length=512), nullable=False),
        sa.Column('p256dh', sa.String(length=255), nullable=False),
        sa.Column('auth', sa.String(length=64), nullable=False),
        sa.Column('content_encoding', sa.String(length=16), nullable=False),
        sa.Column('user_agent', sa.String(length=255), nullable=False),
        sa.Column('last_sent_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('endpoint', name='uq_push_subscriptions_endpoint'),
    )
    op.create_index('ix_push_subscriptions_user_email', 'push_subscriptions', ['user_email'])


def downgrade() -> None:
    op.drop_index('ix_push_subscriptions_user_email', table_name='push_subscriptions')
    op.drop_table('push_subscriptions')
    op.drop_column('panel_memberships', 'business_id')
    op.drop_index('ix_panel_users_login_ticket_hash', table_name='panel_users')
    op.drop_column('panel_users', 'login_ticket_expires_at')
    op.drop_column('panel_users', 'login_ticket_hash')
    op.drop_column('panel_users', 'origin_user_ref')
    op.drop_column('panel_users', 'origin_app_id')
