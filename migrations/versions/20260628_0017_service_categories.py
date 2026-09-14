"""catalog: tabla service_category + service.category_id (ABM de categorías, #106)

Revision ID: 0017_service_categories
Revises: 0016_card_contact_fk
Create Date: 2026-06-28 00:00:00.000000+00:00

Reemplaza el `service.categoria` (texto libre) por una categoría administrable:
crea `service_category` (per-organización), siembra las 4 actuales por cada org
que ya tenga servicios, agrega `service.category_id` (FK `ON DELETE SET NULL` →
borrar la categoría deja los servicios sin categoría) y dropea `categoria`. La
taxonomía cambió, así que los servicios existentes quedan `category_id = NULL`
(el operador los reasigna desde la UI). Reversible.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0017_service_categories"
down_revision: str | None = "0016_card_contact_fk"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Las 4 categorías actuales (#106). Editables luego vía ABM.
_SEED_CATEGORIES = (
    "Capacitación personalizada",
    "Curso de producción audiovisual",
    "Paquetes de producción",
    "Desarrollo de marca personal",
)

_category_table = sa.table(
    "service_category",
    sa.column("id", UUID(as_uuid=True)),
    sa.column("organization_id", UUID(as_uuid=True)),
    sa.column("nombre", sa.Text()),
    sa.column("orden", sa.Integer()),
)


def upgrade() -> None:
    op.create_table(
        "service_category",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("nombre", sa.Text(), nullable=False),
        sa.Column("orden", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("organization_id", "nombre", name="uq_service_category_org_nombre"),
    )
    op.create_index("ix_service_category_organization_id", "service_category", ["organization_id"])

    # Siembra las 4 categorías por cada org que ya tenga servicios cargados.
    bind = op.get_bind()
    org_ids = bind.execute(sa.text("SELECT DISTINCT organization_id FROM service")).scalars().all()
    rows = [
        {"id": uuid.uuid4(), "organization_id": org_id, "nombre": nombre, "orden": orden}
        for org_id in org_ids
        for orden, nombre in enumerate(_SEED_CATEGORIES)
    ]
    if rows:
        op.bulk_insert(_category_table, rows)

    op.add_column("service", sa.Column("category_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "service_category_id_fkey",
        "service",
        "service_category",
        ["category_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # La taxonomía vieja (`categoria` texto) no mapea 1:1 a las nuevas; se descarta
    # y los servicios se reasignan desde la UI.
    op.drop_column("service", "categoria")


def downgrade() -> None:
    op.add_column(
        "service",
        sa.Column("categoria", sa.Text(), server_default=sa.text("''"), nullable=False),
    )
    op.drop_constraint("service_category_id_fkey", "service", type_="foreignkey")
    op.drop_column("service", "category_id")
    op.drop_index("ix_service_category_organization_id", table_name="service_category")
    op.drop_table("service_category")
