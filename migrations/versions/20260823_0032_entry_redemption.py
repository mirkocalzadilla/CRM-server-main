"""crm: estado de uso de la entrada (control de acceso en la puerta)

Revision ID: 0032_entry_redemption
Revises: 0031_events
Create Date: 2026-08-23 00:00:00.000000+00:00

`qr_entry.used_at` + `used_by` (server#278, cierra #202). Una entrada es de **un solo
uso**: `used_at` es lo que hace que el segundo escaneo del mismo QR se rechace, y
`used_by` deja registrado quién lo escaneó.

El consumo se hace con un `UPDATE ... WHERE used_at IS NULL` en una sola sentencia, así
que de dos escaneos simultáneos del mismo código gana exactamente uno. No hace falta un
índice nuevo: el token ya es UNIQUE y es por donde se busca.

Reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0032_entry_redemption"  # <=32 chars (alembic_version es VARCHAR(32))
down_revision: str | None = "0031_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("qr_entry", sa.Column("used_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("qr_entry", sa.Column("used_by", UUID(as_uuid=True), nullable=True))


def downgrade() -> None:
    op.drop_column("qr_entry", "used_by")
    op.drop_column("qr_entry", "used_at")
