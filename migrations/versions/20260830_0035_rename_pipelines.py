"""rename pipelines: 'Gestión IA' -> 'Gestión Venta', 'Gestión Humana' -> 'Gestión Postventa'

Revision ID: 0035_rename_pipelines
Revises: 0034_receipt_approval
Create Date: 2026-08-30 00:00:00.000000+00:00

Los dos pipelines se llamaban por **quién trabajaba** en ellos. Eso dejó de ser
cierto cuando la validación del comprobante y la entrega pasaron a ser automáticas
(CR1-CR6): hoy el sistema mueve la card de "Por validar pago" a "Cerrado" sin que
nadie la toque, y la persona interviene solo en la excepción. El nombre "Gestión
Humana" describía al ejecutor, no al proceso, y ya no coincide con lo que pasa
adentro. Pasan a nombrarse por la **fase del negocio**: venta y postventa.

Es rename de dato, como el 0011 y como "Entrada enviada" -> "Entregado": la etiqueta
del CRM es data-driven (el front muestra `pipeline.name`) y nada rutea por el nombre
— el `kind` ('ia'/'human') es el identificador estable. El seed (`crm/seed.py`) ya
crea los nombres nuevos para orgs nuevas; esto cubre las ya seedeadas.

Match por `kind` (estable) acotado al nombre viejo: idempotente y no pisa el rename
manual de una organización que ya se puso el suyo.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0035_rename_pipelines"
down_revision: str | None = "0034_receipt_approval"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE pipeline SET name = 'Gestión Venta'
        WHERE kind = 'ia' AND name = 'Gestión IA'
        """
    )
    op.execute(
        """
        UPDATE pipeline SET name = 'Gestión Postventa'
        WHERE kind = 'human' AND name = 'Gestión Humana'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE pipeline SET name = 'Gestión IA'
        WHERE kind = 'ia' AND name = 'Gestión Venta'
        """
    )
    op.execute(
        """
        UPDATE pipeline SET name = 'Gestión Humana'
        WHERE kind = 'human' AND name = 'Gestión Postventa'
        """
    )
