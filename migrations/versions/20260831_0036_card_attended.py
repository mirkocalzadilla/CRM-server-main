"""card.attended_at / attended_by — marcar una oportunidad como atendida

Revision ID: 0036_card_attended
Revises: 0035_rename_pipelines
Create Date: 2026-08-31 00:00:00.000000+00:00

La señal "sin responder" se deducía del orden de los mensajes, así que toda
conversación que termina con un "gracias" del lead quedaba marcada como si alguien
estuviera esperando — para siempre, porque nadie va a contestar "de nada".

`attended_at` es la salida explícita: una persona declara que ahí no hay nada
pendiente, y eso cuenta como respuesta. Si el lead vuelve a escribir después, la
señal se reenciende sola (`attended_at < last_lead`), sin deshacer nada a mano.

`attended_by` es texto y no FK a `user`, como `card_move.moved_by` y
`payment_receipt.approved_by`: la traza sobrevive a la baja del usuario.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0036_card_attended"
down_revision: str | None = "0035_rename_pipelines"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("card", sa.Column("attended_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("card", sa.Column("attended_by", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("card", "attended_by")
    op.drop_column("card", "attended_at")
