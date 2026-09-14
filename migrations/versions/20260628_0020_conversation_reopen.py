"""agent: conversación multi-oportunidad por teléfono + closed_at (#163)

Revision ID: 0020_conversation_reopen
Revises: 0019_card_service
Create Date: 2026-06-28 00:00:00.000000+00:00

Un teléfono pasa a tener N conversaciones (una por oportunidad). Se elimina el
UNIQUE(organization_id, external_id) — un lead cerrado que reescribe abre una
conversación nueva, la anterior queda como histórico. `closed_at` marca la
conversación cerrada (su card en stage won/lost) para que el webhook no la reuse.
El índice `conv_org_external` (no único) se conserva para el lookup por teléfono.
Reversible (el downgrade re-crea el UNIQUE; falla si ya hay duplicados por teléfono).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_conversation_reopen"
down_revision: str | None = "0019_card_service"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UNIQUE_NAME = "conversation_organization_id_external_id_key"


def upgrade() -> None:
    op.drop_constraint(_UNIQUE_NAME, "conversation", type_="unique")
    op.add_column(
        "conversation",
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversation", "closed_at")
    op.create_unique_constraint(
        _UNIQUE_NAME, "conversation", ["organization_id", "external_id"]
    )
