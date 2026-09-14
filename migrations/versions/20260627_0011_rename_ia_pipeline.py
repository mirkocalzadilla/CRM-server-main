"""rename pipeline 'Pipeline IA' -> 'Gestión IA'

Revision ID: 0011_rename_ia_pipeline
Revises: 0010_catalog_offer_asset
Create Date: 2026-06-27 00:00:00.000000+00:00

Renombra el pipeline de la IA. La etiqueta visible del CRM es data-driven (web
muestra `pipeline.name`), así que el rename del dato es lo que cambia la UI; no
hay strings hardcodeados. El seed (`crm/seed.py`) ya crea el nombre nuevo para
orgs nuevas; esto cubre las ya seedeadas (p. ej. prod). Match por `kind = 'ia'`
(estable) acotado al nombre viejo para ser idempotente y no pisar renombres
manuales.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011_rename_ia_pipeline"
down_revision: str | None = "0010_catalog_offer_asset"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE pipeline SET name = 'Gestión IA'
        WHERE kind = 'ia' AND name = 'Pipeline IA'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE pipeline SET name = 'Pipeline IA'
        WHERE kind = 'ia' AND name = 'Gestión IA'
        """
    )
