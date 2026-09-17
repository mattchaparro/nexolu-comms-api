"""contacts: estado de bandeja (leido y asignacion).

`last_read_at` = hasta cuando alguien leyo ese hilo (no leido = entro algo
despues). `assigned_to` = panel_users.id de quien lo esta atendiendo.

Revision ID: b3d7c48e2f16
Revises: a7f3e1c95b28
Create Date: 2026-09-17 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b3d7c48e2f16'
down_revision: str | None = 'a7f3e1c95b28'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('contacts', sa.Column('last_read_at', sa.DateTime(), nullable=True))
    op.add_column('contacts', sa.Column('assigned_to', sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column('contacts', 'assigned_to')
    op.drop_column('contacts', 'last_read_at')
