"""whatsapp_flows: espejo local de los WhatsApp Flows de Meta ("Formularios"
en el panel) - JSON subido, estado, validation_errors y URL de vista previa

Revision ID: e1f5a8c3d726
Revises: d4e8b1a30f57
Create Date: 2026-09-22 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e1f5a8c3d726'
down_revision: str | None = 'd4e8b1a30f57'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'whatsapp_flows',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('app_id', sa.String(length=64), nullable=False),
        sa.Column('business_id', sa.String(length=64), nullable=True),
        sa.Column('business_channel_id', sa.String(length=32), nullable=True),
        sa.Column('waba_id', sa.String(length=64), nullable=False),
        sa.Column('meta_flow_id', sa.String(length=64), nullable=True),
        sa.Column('name', sa.String(length=191), nullable=False),
        sa.Column('categories', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        # Texto y no JSON: MySQL reordena las claves (ver la entidad).
        sa.Column('flow_json', sa.Text(), nullable=True),
        sa.Column('json_version', sa.String(length=16), nullable=True),
        sa.Column('validation_errors', sa.JSON(), nullable=False),
        # TEXT sin DEFAULT: MySQL no lo acepta (ver d4e8b1a30f57).
        sa.Column('preview_url', sa.Text(), nullable=True),
        sa.Column('preview_expires_at', sa.String(length=40), nullable=True),
        sa.Column('library_key', sa.String(length=64), nullable=True),
        sa.Column('last_synced_at', sa.DateTime(), nullable=True),
        sa.Column('published_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('waba_id', 'name', name='uq_whatsapp_flow_identity'),
    )
    op.create_index('ix_whatsapp_flows_app', 'whatsapp_flows', ['app_id'])


def downgrade() -> None:
    op.drop_index('ix_whatsapp_flows_app', table_name='whatsapp_flows')
    op.drop_table('whatsapp_flows')
