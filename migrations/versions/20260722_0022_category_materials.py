"""catalog: material a nivel de categoría (asset.category_id + service_category.slug)

Revision ID: 0022_category_materials
Revises: 0021_card_contact_relink
Create Date: 2026-07-22 00:00:00.000000+00:00

El material (PDF/imagen) pasa a colgar de la categoría, no del servicio: se agrega
`asset.category_id` (FK `ON DELETE CASCADE` → borrar la categoría borra sus docs) y
`service_category.slug` (clave estable por org para que el agente referencie la
categoría en `enviar_material`). El slug de las categorías existentes se siembra a
partir del nombre. Los `asset.service_id` existentes NO se migran (decisión de
negocio: se descartan); la columna `service_id` se conserva para no romper el
histórico. Reversible.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0022_category_materials"
down_revision: str | None = "0021_card_contact_relink"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    return cleaned or "categoria"


def upgrade() -> None:
    # slug nullable primero para poder sembrar las filas existentes.
    op.add_column("service_category", sa.Column("slug", sa.Text(), nullable=True))

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, organization_id, nombre FROM service_category "
            "ORDER BY organization_id, orden, nombre"
        )
    ).all()
    seen_by_org: dict[str, set[str]] = {}
    for row in rows:
        org_key = str(row.organization_id)
        seen = seen_by_org.setdefault(org_key, set())
        base = _slugify(row.nombre)
        slug = base
        suffix = 2
        while slug in seen:
            slug = f"{base}-{suffix}"
            suffix += 1
        seen.add(slug)
        bind.execute(
            sa.text("UPDATE service_category SET slug = :slug WHERE id = :id"),
            {"slug": slug, "id": row.id},
        )

    op.alter_column("service_category", "slug", nullable=False)
    op.create_unique_constraint(
        "uq_service_category_org_slug", "service_category", ["organization_id", "slug"]
    )

    op.add_column("asset", sa.Column("category_id", UUID(as_uuid=True), nullable=True))
    op.create_index("ix_asset_category_id", "asset", ["category_id"])
    op.create_foreign_key(
        "asset_category_id_fkey",
        "asset",
        "service_category",
        ["category_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("asset_category_id_fkey", "asset", type_="foreignkey")
    op.drop_index("ix_asset_category_id", table_name="asset")
    op.drop_column("asset", "category_id")
    op.drop_constraint("uq_service_category_org_slug", "service_category", type_="unique")
    op.drop_column("service_category", "slug")
