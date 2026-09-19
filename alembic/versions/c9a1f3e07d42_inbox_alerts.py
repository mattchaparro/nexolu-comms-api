"""Avisos de bandeja: a quien se le dice que hay gente sin responder.

`contacts.alerted_at` evita repetir el mismo aviso cada vuelta del worker;
`inbox_alert_configs` guarda a quien avisarle y cada cuanto.

Revision ID: c9a1f3e07d42
Revises: b3d7c48e2f16
Create Date: 2026-09-18 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c9a1f3e07d42'
down_revision: str | None = 'b3d7c48e2f16'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('contacts', sa.Column('alerted_at', sa.DateTime(), nullable=True))
    op.create_table(
        'inbox_alert_configs',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('app_id', sa.String(length=64), nullable=False),
        sa.Column('business_id', sa.String(length=64), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('emails', sa.JSON(), nullable=False),
        sa.Column('whatsapp_to', sa.String(length=32), nullable=False),
        sa.Column('urgent_template', sa.String(length=191), nullable=False),
        sa.Column('urgent_template_language', sa.String(length=16), nullable=False),
        sa.Column('quiet_minutes', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('app_id', 'business_id', name='uq_inbox_alert_scope'),
    )


def downgrade() -> None:
    op.drop_table('inbox_alert_configs')
    op.drop_column('contacts', 'alerted_at')
