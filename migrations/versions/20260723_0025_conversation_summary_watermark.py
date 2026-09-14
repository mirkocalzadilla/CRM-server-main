"""conversation: high-water mark `summarized_through_order` (resumen IA #254)

Revision ID: 0025_summary_watermark
Revises: 0024_app_history_media
Create Date: 2026-07-23 00:00:00.000000+00:00

El `ai_summary` sólo se refrescaba al cambiar de `funnel_stage`: un lead que se
calienta dentro de la misma etapa quedaba con un resumen congelado (#254). Se agrega
el mark que faltaba: hasta qué `message_order` del lead ya se resumió. El refresh
dentro de la etapa dispara cada N turnos nuevos por encima de este valor y luego lo
avanza (throttle). `DEFAULT 0` = retrocompatible. El backfill marca todo lo existente
como "ya resumido" para NO regenerar en masa al desplegar (evita un storm de Haiku);
la acumulación arranca desde el próximo inbound. Reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_summary_watermark"  # <=32 chars: alembic_version.version_num es VARCHAR(32)
down_revision: str | None = "0024_app_history_media"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversation",
        sa.Column(
            "summarized_through_order",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )
    # Backfill: marcar como "ya resumido" hasta el último turno del lead de cada
    # conversación, para que el deploy NO regenere resúmenes en masa. Lo que engancha
    # se refresca a partir de los turnos nuevos. `->>` sirve para json/jsonb.
    op.execute(
        """
        UPDATE conversation c SET summarized_through_order = COALESCE((
            SELECT max(u.message_order)
            FROM ai_chat_histories u
            WHERE u.thread_id = c.id::text
              AND u.message->>'role' = 'user'
        ), 0)
        """
    )


def downgrade() -> None:
    op.drop_column("conversation", "summarized_through_order")
