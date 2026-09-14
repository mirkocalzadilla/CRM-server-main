"""card: re-backfill de contact_id para cards huérfanas

Cards creadas después de que su contacto existiera quedaron sin `contact_id`
(el link solo se materializaba en el hook 'won' y en el alta manual de contacto).
El código ahora linkea al crear la card; esta migración repara los datos ya
existentes re-ejecutando el mismo backfill tenant-scoped de `0016_card_contact_fk`.

Revision ID: 0021_card_contact_relink
Revises: 0020_conversation_reopen
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021_card_contact_relink"
down_revision: str | None = "0020_conversation_reopen"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tenant-scoped: nunca cruza organizaciones (org de la card == org del contacto).
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
    op.execute(_BACKFILL)


def downgrade() -> None:
    # Data-only repair: no hay estado previo que restaurar.
    pass
