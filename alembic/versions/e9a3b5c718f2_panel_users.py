"""panel_users y panel_memberships: multi-usuario del panel Connect
(admin de plataforma vs cliente externo con scope por apps)

Revision ID: e9a3b5c718f2
Revises: c7d2f8a341e5
Create Date: 2026-09-15 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e9a3b5c718f2'
down_revision: str | None = 'c7d2f8a341e5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'panel_users',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('email', sa.String(length=191), nullable=False, unique=True),
        sa.Column('full_name', sa.String(length=128), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('password_hash', sa.String(length=128), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('last_login_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )

    op.create_table(
        'panel_memberships',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('user_id', sa.String(length=32), sa.ForeignKey('panel_users.id'), nullable=False, index=True),
        sa.Column('app_id', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('user_id', 'app_id', name='uq_panel_membership_user_app'),
    )


def downgrade() -> None:
    op.drop_table('panel_memberships')
    op.drop_table('panel_users')
