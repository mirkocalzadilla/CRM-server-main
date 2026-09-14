"""card: columna notes (notas libres de la oportunidad)

Revision ID: 0013_card_notes
Revises: 0012_contact_full_name
Create Date: 2026-06-28 00:00:00.000000+00:00

Notas libres del operador sobre la oportunidad (ej. "cliente enojado", "cambió la
fecha de la boda"). Editable vía el ABM de oportunidad (issue #97 / #54 FE).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_card_notes"
down_revision: str | None = "0012_contact_full_name"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("card", sa.Column("notes", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("card", "notes")
