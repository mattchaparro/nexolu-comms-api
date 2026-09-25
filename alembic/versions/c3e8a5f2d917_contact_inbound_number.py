"""contacts.last_inbound_phone_number_id: a que numero del negocio escribio
la persona por ultima vez (la ventana de 24 h es con ESE numero)

Revision ID: c3e8a5f2d917
Revises: b7d3e9f1a2c5
Create Date: 2026-09-25 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c3e8a5f2d917'
down_revision: str | None = 'b7d3e9f1a2c5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('contacts', sa.Column('last_inbound_phone_number_id', sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column('contacts', 'last_inbound_phone_number_id')
