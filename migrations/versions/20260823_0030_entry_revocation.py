"""crm: revocación de la entrada + stage terminal "Perdido" en el pipeline humano

Revision ID: 0030_entry_revocation
Revises: 0029_payment_receipt
Create Date: 2026-08-23 00:00:00.000000+00:00

Dos cosas que necesita la conciliación de pagos (server#274):

- **`qr_entry.revoked_at` + `revoked_reason`**: la entrada de un pago que después no se
  pudo confirmar contra el banco. La fila **no se borra** a propósito — el lead ya tiene
  el QR en su teléfono, así que la única defensa real es que el escáner lo rechace en la
  puerta, y para eso tiene que poder encontrar la entrada y ver que está anulada.

- **Stage `"Perdido"` (`status_code = 'lost'`) en el pipeline humano.** No existía: una
  oportunidad que se caía ya en manos de un humano no tenía ningún destino terminal
  negativo (el `Descalificado` del pipeline del bot no aplica, es de otro tablero), así
  que un pago rechazado se quedaba sin dónde ir. Se agrega a los pipelines humanos
  existentes con `max(position) + 1`, que funciona con las dos numeraciones que hay en
  circulación (las organizaciones viejas arrancan en 0, las nuevas en 1) y respeta el
  UNIQUE `(pipeline_id, position)`. Idempotente: no lo duplica si ya está.

Reversible. El downgrade solo borra el stage si ninguna card lo está usando: mover cards
por su cuenta sería peor que dejar el stage.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_entry_revocation"  # <=32 chars (alembic_version es VARCHAR(32))
down_revision: str | None = "0029_payment_receipt"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOST_NAME = "Perdido"


def upgrade() -> None:
    op.add_column("qr_entry", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("qr_entry", sa.Column("revoked_reason", sa.Text(), nullable=True))

    bind = op.get_bind()
    # `stage_status` es lookup global; en una base recién migrada puede no tener 'lost'.
    bind.execute(
        sa.text(
            "INSERT INTO stage_status (code, name, color) VALUES ('lost', 'Perdido', '#dc2626') "
            "ON CONFLICT (code) DO NOTHING"
        )
    )
    pipelines = bind.execute(
        sa.text("SELECT id FROM pipeline WHERE kind = 'human'")
    ).all()
    for row in pipelines:
        exists = bind.execute(
            sa.text("SELECT 1 FROM stage WHERE pipeline_id = :pid AND name = :name"),
            {"pid": row.id, "name": _LOST_NAME},
        ).first()
        if exists is not None:
            continue
        next_position = bind.execute(
            sa.text("SELECT COALESCE(MAX(position), 0) + 1 FROM stage WHERE pipeline_id = :pid"),
            {"pid": row.id},
        ).scalar_one()
        bind.execute(
            sa.text(
                "INSERT INTO stage (id, pipeline_id, name, position, status_code) "
                "VALUES (gen_random_uuid(), :pid, :name, :pos, 'lost')"
            ),
            {"pid": row.id, "name": _LOST_NAME, "pos": next_position},
        )


def downgrade() -> None:
    bind = op.get_bind()
    # Solo se borra el stage vacío: reubicar cards por cuenta propia sería peor que
    # dejarlo, así que si alguna oportunidad quedó ahí, el stage se conserva.
    bind.execute(
        sa.text(
            "DELETE FROM stage WHERE name = :name AND status_code = 'lost' "
            "AND pipeline_id IN (SELECT id FROM pipeline WHERE kind = 'human') "
            "AND id NOT IN (SELECT DISTINCT stage_id FROM card) "
            "AND id NOT IN (SELECT DISTINCT stage_to_id FROM card_move) "
            "AND id NOT IN (SELECT DISTINCT stage_from_id FROM card_move "
            "               WHERE stage_from_id IS NOT NULL)"
        ),
        {"name": _LOST_NAME},
    )
    op.drop_column("qr_entry", "revoked_reason")
    op.drop_column("qr_entry", "revoked_at")
