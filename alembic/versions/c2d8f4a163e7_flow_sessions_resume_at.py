"""flow_sessions.resume_at: soporte del nodo delay del motor de flujos
(status=waiting + cuando retomar; el worker de reanudacion lo consulta)

Revision ID: c2d8f4a163e7
Revises: f3a7c9e254b8
Create Date: 2026-09-15 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c2d8f4a163e7'
down_revision: str | None = 'f3a7c9e254b8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('flow_sessions', sa.Column('resume_at', sa.DateTime(), nullable=True))
    op.create_index('ix_flow_sessions_resume', 'flow_sessions', ['status', 'resume_at'])


def downgrade() -> None:
    op.drop_index('ix_flow_sessions_resume', table_name='flow_sessions')
    op.drop_column('flow_sessions', 'resume_at')
