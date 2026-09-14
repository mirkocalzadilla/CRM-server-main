"""crm: tabla event + entrada ligada al evento (con snapshot)

Revision ID: 0031_events
Revises: 0030_entry_revocation
Create Date: 2026-08-23 00:00:00.000000+00:00

Los eventos (server#276, cierra #201 y #203). Tabla dedicada y no campos dentro de
`service` porque son dos ciclos de vida distintos: un servicio es lo que se vende y dura
mientras esté en el catálogo; un evento es una fecha que ocurre y pasa, y un mismo curso
presencial tiene varias ediciones.

`qr_entry.event_id` (FK `SET NULL`) + `event_snapshot`:

- El `SET NULL` es deliberado: borrar un evento **no** puede borrar las entradas ya
  emitidas. El lead las tiene en su teléfono y son legítimas; quedan sin evento y el
  escáner las trata como legacy.
- El snapshot es una copia de la fecha y el lugar al momento de emitir. Redundante a
  propósito: en la puerta tienen que poder leerse aunque alguien haya editado el evento.

**Las entradas que ya existen quedan con `event_id = NULL`.** No se les asigna ninguno
automáticamente: no hay forma de saber a qué evento pertenecen (los eventos no existían
cuando se emitieron), y adivinar sería peor — mandaría a alguien a una fecha equivocada.
El backfill, si hace falta, es `scripts/backfill_entry_events.py`, que solo asigna
cuando el servicio tiene exactamente un evento y por lo tanto no hay ambigüedad.

Reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0031_events"  # <=32 chars (alembic_version es VARCHAR(32))
down_revision: str | None = "0030_entry_revocation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("service_id", UUID(as_uuid=True), nullable=False),
        sa.Column("nombre", sa.Text(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("maps_url", sa.Text(), nullable=True),
        sa.Column("capacity", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="scheduled"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["service_id"], ["service.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "status IN ('scheduled','active','closed')", name="event_status_check"
        ),
    )
    op.create_index("ix_event_organization_id", "event", ["organization_id"])
    op.create_index("ix_event_service_id", "event", ["service_id"])

    op.add_column("qr_entry", sa.Column("event_id", UUID(as_uuid=True), nullable=True))
    op.add_column(
        "qr_entry",
        sa.Column("event_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.create_index("ix_qr_entry_event_id", "qr_entry", ["event_id"])
    op.create_foreign_key(
        "qr_entry_event_id_fkey", "qr_entry", "event", ["event_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint("qr_entry_event_id_fkey", "qr_entry", type_="foreignkey")
    op.drop_index("ix_qr_entry_event_id", table_name="qr_entry")
    op.drop_column("qr_entry", "event_snapshot")
    op.drop_column("qr_entry", "event_id")
    op.drop_index("ix_event_service_id", table_name="event")
    op.drop_index("ix_event_organization_id", table_name="event")
    op.drop_table("event")
