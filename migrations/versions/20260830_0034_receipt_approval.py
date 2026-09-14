"""crm: el comprobante registra que se aprobó y quién lo aprobó

Revision ID: 0034_receipt_approval
Revises: 0033_manual_checkin
Create Date: 2026-08-30 00:00:00.000000+00:00

`payment_receipt.approved_at` + `approved_by` (`'system'` o el uuid del operador).

Hasta acá el comprobante guardaba el veredicto de los checks y la conciliación
posterior (`human_confirmed_at` / `human_rejected_at`), pero **no que el pago se aprobó
ni quién lo hizo**: la aprobación vivía solo como un move de la card. El panel del CRM no
tenía forma de saber que un pago ya estaba validado y entregado, y seguía ofreciendo
"Validar pago y entregar" sobre una card cerrada (server#292 / web#194).

Backfill de lo ya aprobado, en el mismo orden de precedencia que usa el CRM:

- Auto-aprobados: `verdict = 'pass'` sin nota humana, con la card fuera de "Por validar
  pago" (la auto-aprobación mueve la card en el mismo acto; si sigue ahí, nadie aprobó).
- Overrides: `human_note` sin rechazo. `approved_by` queda en NULL cuando no se registró
  el operador (el override no escribía `human_by`): el CRM lo lee como "a mano".

Un pago después **rechazado** puede quedar con `approved_at` (fue aprobado antes de que
el banco lo desmintiera): es cierto, y `human_rejected_at` manda sobre todo lo demás.

Reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_receipt_approval"  # <=32 chars (alembic_version es VARCHAR(32))
down_revision: str | None = "0033_manual_checkin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AWAITING_STAGE = "Por validar pago"


def upgrade() -> None:
    op.add_column(
        "payment_receipt", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("payment_receipt", sa.Column("approved_by", sa.Text(), nullable=True))

    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE payment_receipt r SET approved_at = r.created_at, approved_by = 'system' "
            "FROM card c JOIN stage s ON s.id = c.stage_id "
            "WHERE c.id = r.card_id AND r.verdict = 'pass' AND r.human_note IS NULL "
            "AND s.name <> :awaiting"
        ),
        {"awaiting": _AWAITING_STAGE},
    )
    bind.execute(
        sa.text(
            "UPDATE payment_receipt SET approved_at = updated_at, approved_by = human_by::text "
            "WHERE human_note IS NOT NULL AND human_rejected_at IS NULL AND approved_at IS NULL"
        )
    )


def downgrade() -> None:
    op.drop_column("payment_receipt", "approved_by")
    op.drop_column("payment_receipt", "approved_at")
