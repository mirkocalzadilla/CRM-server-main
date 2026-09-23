"""outbound_message + marketing_opt_out — envíos iniciados por el negocio (plantillas)

Revision ID: 0038_outbound_messages
Revises: 0037_payment_qr_upload
Create Date: 2026-09-23 00:00:00.000000+00:00

Hasta ahora el sistema solo respondía dentro de la ventana de 24 h y no guardaba
ningún envío saliente: un mensaje que Meta aceptaba (2xx) y descartaba después era
invisible (#298), y no existía forma de que un lead pidiera no recibir más mensajes.

`outbound_message`: una fila por plantilla enviada (o descartada a propósito), con el
`wamid` que devuelve Meta para cruzar los `statuses` del webhook, y una `dedupe_key`
única que hace idempotentes los envíos automáticos (recordatorio X del evento Y a la
card Z se manda una sola vez aunque el job corra dos veces).

`marketing_opt_out`: números que pidieron la baja. Se consulta antes de cualquier
envío iniciado por el negocio.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0038_outbound_messages"
down_revision: str | None = "0037_payment_qr_upload"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outbound_message",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("card_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("wa_id", sa.Text(), nullable=False),
        sa.Column("template_name", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False, server_default="es"),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("variables", postgresql.JSON(), nullable=False, server_default="{}"),
        sa.Column("rendered_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("dedupe_key", sa.Text(), nullable=True),
        sa.Column("wamid", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("error_code", sa.Integer(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("dedupe_key", name="uq_outbound_dedupe_key"),
        sa.UniqueConstraint("wamid", name="uq_outbound_wamid"),
    )
    op.create_index("ix_outbound_message_organization_id", "outbound_message", ["organization_id"])
    op.create_index("ix_outbound_message_conversation_id", "outbound_message", ["conversation_id"])
    op.create_index("ix_outbound_message_card_id", "outbound_message", ["card_id"])
    op.create_index("ix_outbound_message_wa_id", "outbound_message", ["wa_id"])
    op.create_index("ix_outbound_message_purpose", "outbound_message", ["purpose"])
    op.create_index("ix_outbound_message_status", "outbound_message", ["status"])

    op.create_table(
        "marketing_opt_out",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("wa_id", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False, server_default="keyword"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("organization_id", "wa_id", name="uq_opt_out_org_wa"),
    )


def downgrade() -> None:
    op.drop_table("marketing_opt_out")
    for name in (
        "ix_outbound_message_status",
        "ix_outbound_message_purpose",
        "ix_outbound_message_wa_id",
        "ix_outbound_message_card_id",
        "ix_outbound_message_conversation_id",
        "ix_outbound_message_organization_id",
    ):
        op.drop_index(name, table_name="outbound_message")
    op.drop_table("outbound_message")
