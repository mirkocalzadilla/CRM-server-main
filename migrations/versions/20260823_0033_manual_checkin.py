"""crm: marca de check-in manual en la puerta (fallback sin cámara)

Revision ID: 0033_manual_checkin
Revises: 0032_entry_redemption
Create Date: 2026-08-23 00:00:00.000000+00:00

`qr_entry.used_manually`: la entrada se consumió desde la lista de asistencia y no
escaneando el QR.

Por qué una columna y no un log: en la puerta el fallback manual es la vía de escape
cuando la cámara falla, y también la única por la que alguien puede entrar sin mostrar su
QR. Después de un evento las dos preguntas que se hacen son "cuántos entraron" y "a quién
dejamos pasar a mano", y la segunda necesita una respuesta durable. Los logs de esta
aplicación no sobreviven a un deploy (#260 sigue abierto), así que la traza tiene que
estar en la base.

`DEFAULT FALSE` sobre las filas existentes es correcto: todo lo consumido hasta hoy se
consumió escaneando, porque el camino manual no existía.

Reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_manual_checkin"  # <=32 chars (alembic_version es VARCHAR(32))
down_revision: str | None = "0032_entry_redemption"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "qr_entry",
        sa.Column("used_manually", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("qr_entry", "used_manually")
