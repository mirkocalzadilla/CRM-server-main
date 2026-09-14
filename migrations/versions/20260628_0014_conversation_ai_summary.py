"""conversation: columna ai_summary (resumen del caso por IA)

Revision ID: 0014_conversation_ai_summary
Revises: 0013_card_notes
Create Date: 2026-06-28 00:00:00.000000+00:00

Resumen breve del caso generado por IA (se refresca al avanzar de etapa del funnel
y al derivar a humano). Se expone en el detalle de la oportunidad (#96 / #53 FE).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_conversation_ai_summary"
down_revision: str | None = "0013_card_notes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversation", sa.Column("ai_summary", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("conversation", "ai_summary")
