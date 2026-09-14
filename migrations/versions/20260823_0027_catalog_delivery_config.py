"""catalogo: modalidad + precio estructurado + links tipados + config de pagos por org

Revision ID: 0027_catalog_delivery
Revises: 0026_card_move_reason
Create Date: 2026-08-23 00:00:00.000000+00:00

Prepara el terreno para la entrega automática post-pago (server#268):

- `service.modality` (`presencial`/`virtual`/NULL) decide QUÉ se entrega al validarse
  el pago. NULL = no se entrega nada: default seguro, y el estado en que quedan todos
  los servicios existentes (la modalidad la marca el operador desde el CRM; adivinarla
  acá arriesgaría mandarle una entrada a quien compró otra cosa).
- `service.price_amount` es el precio como número, para comparar contra el monto de un
  comprobante. `service.precio` sigue siendo el texto que ve el lead (admite rangos y
  USD, y por eso no sirve para validar). NULL = no comparable ⇒ validación humana.
- `service_link` = links de entrega tipados (grupo de WhatsApp, reunión, ubicación).
- `payment_settings` = config de pagos por organización: beneficiario esperado del
  comprobante + QR de pago propio. Sin fila, el QR cae al global de la env var, así que
  el comportamiento actual de producción no cambia al aplicar esta migración.

El backfill de `price_amount` a partir del texto NO va acá: es `scripts/backfill_catalog_prices.py`
(parseo con reglas de negocio, idempotente y reversible a mano). Reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0027_catalog_delivery"  # <=32 chars: alembic_version.version_num es VARCHAR(32)
down_revision: str | None = "0026_card_move_reason"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("service", sa.Column("modality", sa.Text(), nullable=True))
    op.add_column("service", sa.Column("price_amount", sa.Numeric(12, 2), nullable=True))

    op.create_table(
        "service_link",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("service_id", UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("orden", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["service_id"], ["service.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "kind IN ('whatsapp_group','meeting','maps','other')",
            name="service_link_kind_check",
        ),
    )
    op.create_index("ix_service_link_organization_id", "service_link", ["organization_id"])
    op.create_index("ix_service_link_service_id", "service_link", ["service_id"])

    op.create_table(
        "payment_settings",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("expected_beneficiary", sa.Text(), nullable=True),
        sa.Column("payment_qr_url", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("organization_id", name="uq_payment_settings_org"),
    )
    op.create_index(
        "ix_payment_settings_organization_id", "payment_settings", ["organization_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_payment_settings_organization_id", table_name="payment_settings")
    op.drop_table("payment_settings")
    op.drop_index("ix_service_link_service_id", table_name="service_link")
    op.drop_index("ix_service_link_organization_id", table_name="service_link")
    op.drop_table("service_link")
    op.drop_column("service", "price_amount")
    op.drop_column("service", "modality")
