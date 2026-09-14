"""catalog: asset.service_id (≤5 materiales por servicio, #108)

Revision ID: 0018_service_materials
Revises: 0017_service_categories
Create Date: 2026-06-28 00:00:00.000000+00:00

Pasa de un único material por servicio (`service.asset_id`) a varios: el enlace se
invierte a `asset.service_id` (un servicio → N materiales). Migra el material
existente (cada `service.asset_id` → `asset.service_id`) y dropea `service.asset_id`.
El snapshot de `publish` conserva `material_url` apuntando al material principal, así
que el runtime del agente no cambia. Reversible (el downgrade reconstruye `asset_id`
con el material más antiguo de cada servicio).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0018_service_materials"
down_revision: str | None = "0017_service_categories"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("asset", sa.Column("service_id", UUID(as_uuid=True), nullable=True))
    op.create_index("ix_asset_service_id", "asset", ["service_id"])
    op.create_foreign_key(
        "asset_service_id_fkey", "asset", "service", ["service_id"], ["id"], ondelete="SET NULL"
    )
    # Migra el material actual: service.asset_id → asset.service_id.
    op.execute(
        "UPDATE asset SET service_id = service.id "
        "FROM service WHERE service.asset_id = asset.id"
    )
    op.drop_column("service", "asset_id")


def downgrade() -> None:
    op.add_column("service", sa.Column("asset_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "service_asset_id_fkey", "service", "asset", ["asset_id"], ["id"], ondelete="SET NULL"
    )
    # Reconstruye el único asset_id con el material más antiguo de cada servicio.
    op.execute(
        "UPDATE service SET asset_id = sub.id FROM ("
        "SELECT DISTINCT ON (service_id) id, service_id FROM asset "
        "WHERE service_id IS NOT NULL ORDER BY service_id, created_at"
        ") sub WHERE sub.service_id = service.id"
    )
    op.drop_constraint("asset_service_id_fkey", "asset", type_="foreignkey")
    op.drop_index("ix_asset_service_id", table_name="asset")
    op.drop_column("asset", "service_id")
