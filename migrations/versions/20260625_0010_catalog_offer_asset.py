"""catalog: tablas offer + asset (carga del catálogo + materiales desde UI)

Revision ID: 0010_catalog_offer_asset
Revises: 0009_human_intake_stage
Create Date: 2026-06-25 00:00:00.000000+00:00

Fase 1 del catálogo multi-oferta (SPEC_admin_catalogo_kb §4): `asset` (PDF/imagen
hosteado en media_root) y `offer` (catálogo curado del agente, slug único por
agente). `offer.asset_id` referencia el material; se crea `asset` primero.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0010_catalog_offer_asset"
down_revision: str | None = "0009_human_intake_stage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "asset",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("storage_ref", sa.Text(), nullable=False),
        sa.Column("public_url", sa.Text(), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_asset_organization_id", "asset", ["organization_id"])

    op.create_table(
        "offer",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "agent_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agent.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("nombre", sa.Text(), nullable=False),
        sa.Column("categoria", sa.Text(), nullable=False),
        sa.Column("resumen", sa.Text(), nullable=False),
        sa.Column("detalle", sa.Text(), nullable=True),
        sa.Column("precio", sa.Text(), nullable=False),
        sa.Column("moneda", sa.Text(), nullable=False),
        sa.Column(
            "flujo_cierre", sa.Text(), server_default=sa.text("'pago_qr'"), nullable=False
        ),
        sa.Column(
            "asset_id",
            UUID(as_uuid=True),
            sa.ForeignKey("asset.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("orden", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
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
        sa.UniqueConstraint("agent_id", "slug", name="uq_offer_agent_slug"),
    )
    op.create_index("ix_offer_organization_id", "offer", ["organization_id"])
    op.create_index("ix_offer_agent_id", "offer", ["agent_id"])


def downgrade() -> None:
    op.drop_index("ix_offer_agent_id", table_name="offer")
    op.drop_index("ix_offer_organization_id", table_name="offer")
    op.drop_table("offer")
    op.drop_index("ix_asset_organization_id", table_name="asset")
    op.drop_table("asset")
