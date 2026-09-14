"""conversation: high-water mark `answered_through_order` (carrera inbound en vuelo)

Revision ID: 0023_answered_watermark
Revises: 0022_category_materials
Create Date: 2026-07-23 00:00:00.000000+00:00

El agente decidía "¿tengo algo que contestar?" mirando si la última fila de la
ventana (por `message_order`) era del lead. Esa inferencia posicional falla cuando
una respuesta se guarda tarde y su `message_order` salta por encima de un inbound
que llegó antes (lead que escribe mientras un turno sigue en vuelo) → el mensaje
queda sin responder. Se agrega el dato que faltaba: hasta qué `message_order` del
lead ya procesó la conversación. `DEFAULT 0` = retrocompatible (toda conversación
existente arranca como "nada atendido"; el próximo inbound la pone al día).
Reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_answered_watermark"  # <=32 chars: alembic_version.version_num es VARCHAR(32)
down_revision: str | None = "0022_category_materials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversation",
        sa.Column(
            "answered_through_order",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )
    # Backfill preservando el comportamiento anterior (guard = "última fila es del
    # agente"): marcar como atendido el último turno del lead que quedó por debajo de la
    # última respuesta del agente. Así una conversación ya respondida NO se re-despacha al
    # reiniciar el worker (evita un re-envío masivo), y una con inbound colgado (sin
    # assistant por encima) queda en 0 y el catch-up la toma. `->>` sirve para json/jsonb.
    op.execute(
        """
        UPDATE conversation c SET answered_through_order = COALESCE((
            SELECT max(u.message_order)
            FROM ai_chat_histories u
            WHERE u.thread_id = c.id::text
              AND u.message->>'role' = 'user'
              AND u.message_order < (
                  SELECT max(a.message_order)
                  FROM ai_chat_histories a
                  WHERE a.thread_id = c.id::text
                    AND a.message->>'role' = 'assistant'
              )
        ), 0)
        """
    )


def downgrade() -> None:
    op.drop_column("conversation", "answered_through_order")
