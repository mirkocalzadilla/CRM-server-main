"""card_move: columna opcional `reason` (motivo del move manual, audit)

Revision ID: 0026_card_move_reason
Revises: 0025_summary_watermark
Create Date: 2026-07-23 00:00:00.000000+00:00

Un move manual solo persistía quién/cuándo; al descalificar un lead no quedaba
rastro de *por qué* (server#253). Se agrega `reason TEXT NULL`: opcional a nivel
datos (la obligatoriedad para Descalificado se impone en la UI). `NULL` =
retrocompatible: las filas viejas no tienen motivo. Reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_card_move_reason"  # <=32 chars: alembic_version.version_num es VARCHAR(32)
down_revision: str | None = "0025_summary_watermark"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("card_move", sa.Column("reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("card_move", "reason")
