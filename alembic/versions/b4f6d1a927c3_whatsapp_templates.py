"""whatsapp_templates: espejo local de plantillas de Meta (estado por
webhook message_template_status_update + sync manual)

Revision ID: b4f6d1a927c3
Revises: e9a3b5c718f2
Create Date: 2026-09-15 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b4f6d1a927c3'
down_revision: str | None = 'e9a3b5c718f2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'whatsapp_templates',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('app_id', sa.String(length=64), nullable=False),
        sa.Column('business_channel_id', sa.String(length=32), nullable=True),
        sa.Column('waba_id', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=191), nullable=False),
        sa.Column('language', sa.String(length=16), nullable=False),
        sa.Column('category', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('meta_template_id', sa.String(length=64), nullable=True),
        sa.Column('components', sa.JSON(), nullable=False),
        sa.Column('quality_score', sa.String(length=32), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('last_synced_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('waba_id', 'name', 'language', name='uq_whatsapp_template_identity'),
    )
    op.create_index('ix_whatsapp_templates_app', 'whatsapp_templates', ['app_id'])


def downgrade() -> None:
    op.drop_index('ix_whatsapp_templates_app', table_name='whatsapp_templates')
    op.drop_table('whatsapp_templates')
