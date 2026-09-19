"""La ficha del contacto en la bandeja: notas y respuestas rapidas.

`contacts.notes` = lo que quien atiende necesita saber ANTES de contestar.
`quick_replies` = lo que se escribe veinte veces al dia, guardado una vez.

Revision ID: d4e8b1a30f57
Revises: c9a1f3e07d42
Create Date: 2026-09-19 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd4e8b1a30f57'
down_revision: str | None = 'c9a1f3e07d42'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # MySQL no acepta DEFAULT en TEXT ("BLOB, TEXT... can't have a default
    # value"), y SQLite si - por eso esto paso las pruebas y fallo en
    # produccion. Se agrega nullable, se rellena y se cierra.
    op.add_column('contacts', sa.Column('notes', sa.Text(), nullable=True))
    op.execute("UPDATE contacts SET notes = '' WHERE notes IS NULL")
    op.alter_column('contacts', 'notes', existing_type=sa.Text(), nullable=False)
    op.create_table(
        'quick_replies',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('app_id', sa.String(length=64), nullable=False),
        sa.Column('business_id', sa.String(length=64), nullable=False),
        sa.Column('shortcut', sa.String(length=64), nullable=False),
        sa.Column('title', sa.String(length=128), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('app_id', 'business_id', 'shortcut', name='uq_quick_reply_shortcut'),
    )


def downgrade() -> None:
    op.drop_table('quick_replies')
    op.drop_column('contacts', 'notes')
