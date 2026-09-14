"""card: FK contact_id (vínculo card↔contacto) + backfill por phone

Revision ID: 0016_card_contact_fk
Revises: 0015_rename_offer_to_service
Create Date: 2026-06-28 00:00:00.000000+00:00

Materializa el vínculo card↔contacto como FK explícita (#139, opción B): cimiento de
la cadena de Historial global del contacto (#101). `card.contact_id` es nullable
(ondelete SET NULL: borrar el contacto no borra la card) con índice. El backfill
linkea las cards existentes a su contacto por match de `phone` dentro del mismo
tenant (`conversation.external_id == contact.phone` con igual `organization_id`).
Reversible (down dropea FK, índice y columna).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_card_contact_fk"
down_revision: str | None = "0015_rename_offer_to_service"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Backfill tenant-scoped: nunca cruza organizaciones (org de la card == org del contacto).
_BACKFILL = """
UPDATE card AS c
SET contact_id = ct.id
FROM conversation AS cv, contact AS ct
WHERE c.conversation_id = cv.id
  AND ct.organization_id = c.organization_id
  AND ct.phone = cv.external_id
  AND c.contact_id IS NULL
"""


def upgrade() -> None:
    op.add_column(
        "card", sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.create_index("ix_card_contact_id", "card", ["contact_id"])
    op.create_foreign_key(
        "fk_card_contact_id",
        "card",
        "contact",
        ["contact_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(_BACKFILL)


def downgrade() -> None:
    op.drop_constraint("fk_card_contact_id", "card", type_="foreignkey")
    op.drop_index("ix_card_contact_id", table_name="card")
    op.drop_column("card", "contact_id")
