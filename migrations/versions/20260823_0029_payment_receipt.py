"""crm: payment_receipt (validación del comprobante) + dedup por wamid en la ingesta

Revision ID: 0029_payment_receipt
Revises: 0028_card_flags_delivered
Create Date: 2026-08-23 00:00:00.000000+00:00

Dos piezas de la validación automática de comprobantes (server#272):

- **`payment_receipt`**: qué decía el comprobante, qué check pasó y cuál no, y quién
  terminó aprobándolo. Sus dos UNIQUE son la defensa anti-reuso, y son constraints y no
  lógica a propósito: `(org, reference)` impide que la misma transferencia pague dos
  compras, y `(org, image_sha256)` impide reenviar la misma captura. Un `reference` NULL
  no colisiona en Postgres — por eso un comprobante sin identificador legible nunca se
  aprueba solo: sin él esa defensa no existe.

- **Índice único parcial `uq_ai_history_org_wamid`** sobre `(organization_id,
  message->>'wamid')`: idempotencia de la ingesta. Meta reintenta la entrega del
  webhook, y sin esto un reintento duplica el turno del lead — el agente responde dos
  veces y un comprobante se valida dos veces. Es de expresión porque el wamid vive
  dentro del JSON, y parcial porque los turnos del asistente no llevan wamid y no deben
  colisionar entre sí.

**Antes de aplicar en producción**, verificar que no haya duplicados preexistentes (si
los hubiera, el índice no se crea):

    SELECT message->>'wamid', count(*) FROM ai_chat_histories
    WHERE message->>'wamid' IS NOT NULL GROUP BY 1 HAVING count(*) > 1;

Reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0029_payment_receipt"  # <=32 chars (alembic_version es VARCHAR(32))
down_revision: str | None = "0028_card_flags_delivered"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WAMID_INDEX = "uq_ai_history_org_wamid"


def upgrade() -> None:
    op.create_table(
        "payment_receipt",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("card_id", UUID(as_uuid=True), nullable=False),
        sa.Column("wamid", sa.Text(), nullable=False),
        sa.Column("media_path", sa.Text(), nullable=True),
        sa.Column("image_sha256", sa.Text(), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("currency", sa.Text(), nullable=True),
        sa.Column("paid_at", sa.Date(), nullable=True),
        sa.Column("beneficiary", sa.Text(), nullable=True),
        sa.Column("reference", sa.Text(), nullable=True),
        sa.Column("bank", sa.Text(), nullable=True),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("checks", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("extracted", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("human_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("human_rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("human_by", UUID(as_uuid=True), nullable=True),
        sa.Column("human_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["card_id"], ["card.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("organization_id", "reference", name="uq_payment_receipt_org_reference"),
        sa.UniqueConstraint("organization_id", "image_sha256", name="uq_payment_receipt_org_sha"),
    )
    op.create_index("ix_payment_receipt_organization_id", "payment_receipt", ["organization_id"])
    op.create_index("ix_payment_receipt_card_id", "payment_receipt", ["card_id"])
    op.create_index("ix_payment_receipt_wamid", "payment_receipt", ["wamid"])

    op.create_index(
        _WAMID_INDEX,
        "ai_chat_histories",
        ["organization_id", sa.text("(message ->> 'wamid')")],
        unique=True,
        postgresql_where=sa.text("message ->> 'wamid' IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_WAMID_INDEX, table_name="ai_chat_histories")
    op.drop_index("ix_payment_receipt_wamid", table_name="payment_receipt")
    op.drop_index("ix_payment_receipt_card_id", table_name="payment_receipt")
    op.drop_index("ix_payment_receipt_organization_id", table_name="payment_receipt")
    op.drop_table("payment_receipt")
