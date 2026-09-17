"""webhook_events.flow_handled: si el motor de flujos atendio el mensaje.

Coordina el hibrido flujos+bot: el reenvio a la app duena lleva
X-Nexolu-Flow-Handled para que su agente IA calle cuando un flujo de
Connect ya respondio (y conteste cuando nada matcheo).

Revision ID: a7f3e1c95b28
Revises: e5b9d2c847f1
Create Date: 2026-09-16 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a7f3e1c95b28'
down_revision: str | None = 'e5b9d2c847f1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('webhook_events', sa.Column('flow_handled', sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column('webhook_events', 'flow_handled')
