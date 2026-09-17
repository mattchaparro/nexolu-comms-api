"""chat_messages: la bandeja/live chat de Connect - el historial de la
conversacion con cada contacto (entrantes del webhook + salientes del
panel, los flujos y las apps)

Revision ID: e5b9d2c847f1
Revises: c2d8f4a163e7
Create Date: 2026-09-16 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e5b9d2c847f1'
down_revision: str | None = 'c2d8f4a163e7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'chat_messages',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('app_id', sa.String(length=64), nullable=False),
        sa.Column('business_id', sa.String(length=64), nullable=False),
        sa.Column('contact_id', sa.String(length=32), nullable=False),
        sa.Column('direction', sa.String(length=8), nullable=False),
        sa.Column('message_type', sa.String(length=32), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('wamid', sa.String(length=191), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('origin', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_chat_messages_contact', 'chat_messages', ['contact_id', 'created_at'])
    op.create_index(
        'ix_chat_messages_app', 'chat_messages', ['app_id', 'business_id', 'created_at']
    )


def downgrade() -> None:
    op.drop_index('ix_chat_messages_app', table_name='chat_messages')
    op.drop_index('ix_chat_messages_contact', table_name='chat_messages')
    op.drop_table('chat_messages')
