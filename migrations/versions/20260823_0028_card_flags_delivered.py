"""crm: avisos operativos en la card + rename del stage "Entrada enviada" -> "Entregado"

Revision ID: 0028_card_flags_delivered
Revises: 0027_catalog_delivery
Create Date: 2026-08-23 00:00:00.000000+00:00

Dos cambios que habilitan la entrega automática (server#270):

- `card.flags` (JSON, default `[]`): avisos operativos de la card — por qué algo
  automático no se completó (falta el nombre del lead, se cerró la ventana de 24h de
  WhatsApp, llegó un comprobante extra, el servicio no tiene modalidad, falta un link).
  Son códigos; el CRM los traduce al español. JSON y no columnas booleanas porque el
  conjunto crece con cada fase y no vale una migración por aviso.

- Rename del stage `"Entrada enviada"` → `"Entregado"` en los pipelines humanos
  existentes. La entrega dejó de ser siempre una entrada: un curso virtual recibe
  links, no un QR, así que el stage pasa a nombrarse por el resultado y no por el
  medio. `stage.name` es **dato por organización**, así que el seed solo alcanza a las
  organizaciones nuevas — de ahí esta migración de datos (mismo criterio que #235).
  El rename es idempotente y no toca stages de otro nombre.

Reversible: el downgrade vuelve el nombre anterior y quita la columna.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_card_flags_delivered"  # <=32 chars (alembic_version es VARCHAR(32))
down_revision: str | None = "0027_catalog_delivery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_NAME = "Entrada enviada"
_NEW_NAME = "Entregado"

_RENAME = sa.text(
    "UPDATE stage SET name = :new_name "
    "WHERE name = :old_name "
    "AND pipeline_id IN (SELECT id FROM pipeline WHERE kind = 'human')"
)


def upgrade() -> None:
    op.add_column(
        "card",
        sa.Column("flags", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.get_bind().execute(_RENAME, {"new_name": _NEW_NAME, "old_name": _LEGACY_NAME})


def downgrade() -> None:
    op.get_bind().execute(_RENAME, {"new_name": _LEGACY_NAME, "old_name": _NEW_NAME})
    op.drop_column("card", "flags")
