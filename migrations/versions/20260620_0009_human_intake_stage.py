"""human pipeline: add 'Por atender' generic intake stage

Revision ID: 0009_human_intake_stage
Revises: 0008_qr_entry
Create Date: 2026-06-20 00:00:00.000000+00:00

Backfill de datos: la pipeline "Gestión Humana" de cada organización gana una stage
de intake genérica ("Por atender") para los handoffs por `explicit_request` /
`agent_error` (pedir humano ≠ validar pago). Se inserta en `position = 0` para
quedar primera sin re-numerar las stages existentes (la UNIQUE (pipeline_id,
position) + el gotcha de Postgres con `position = position + 1` hacen el shift
frágil). Idempotente: no duplica si ya existe. El seed (`crm/seed.py`) ya la crea
para orgs nuevas; esto cubre las ya seedeadas (p. ej. prod).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009_human_intake_stage"
down_revision: str | None = "0008_qr_entry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO stage (id, pipeline_id, name, position, status_code)
        SELECT gen_random_uuid(), p.id, 'Por atender', 0, 'open'
        FROM pipeline p
        WHERE p.kind = 'human'
          AND NOT EXISTS (
              SELECT 1 FROM stage s
              WHERE s.pipeline_id = p.id AND s.name = 'Por atender'
          )
        """
    )


def downgrade() -> None:
    # Best-effort y destructivo: reasigna cards parqueadas en 'Por atender' a
    # 'Por validar pago' (mismo pipeline) y borra los movimientos que la referencian
    # (FK sin cascade) antes de eliminar la stage.
    op.execute(
        """
        UPDATE card SET stage_id = pv.id
        FROM stage pa
        JOIN stage pv ON pv.pipeline_id = pa.pipeline_id AND pv.name = 'Por validar pago'
        WHERE card.stage_id = pa.id AND pa.name = 'Por atender'
        """
    )
    op.execute(
        """
        DELETE FROM card_move
        WHERE stage_from_id IN (SELECT id FROM stage WHERE name = 'Por atender')
           OR stage_to_id   IN (SELECT id FROM stage WHERE name = 'Por atender')
        """
    )
    op.execute(
        """
        DELETE FROM stage
        WHERE name = 'Por atender'
          AND pipeline_id IN (SELECT id FROM pipeline WHERE kind = 'human')
        """
    )
