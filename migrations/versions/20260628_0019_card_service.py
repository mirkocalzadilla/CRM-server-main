"""crm: tabla card_service (servicios asignados a la oportunidad, #132)

Revision ID: 0019_card_service
Revises: 0018_service_materials
Create Date: 2026-06-28 00:00:00.000000+00:00

Tabla puente N-a-N entre `card` (oportunidad) y `service` (catálogo): un chat puede
contratar un combo de servicios. `source` distingue asignación manual del operador
(`assigned`) de la captura automática del bot (`captured`, #133). `service_id` apunta
al catálogo vivo (los servicios se borran en lógico, así que la fila persiste para el
historial). Org-scoped. Reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0019_card_service"
down_revision: str | None = "0018_service_materials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "card_service",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "card_id",
            UUID(as_uuid=True),
            sa.ForeignKey("card.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "service_id",
            UUID(as_uuid=True),
            sa.ForeignKey("service.id"),
            nullable=False,
        ),
        sa.Column("source", sa.Text(), server_default=sa.text("'assigned'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("card_id", "service_id", name="uq_card_service"),
        sa.CheckConstraint(
            "source IN ('assigned','captured')", name="card_service_source_check"
        ),
    )
    op.create_index("ix_card_service_organization_id", "card_service", ["organization_id"])
    op.create_index("ix_card_service_card_id", "card_service", ["card_id"])
    op.create_index("ix_card_service_service_id", "card_service", ["service_id"])


def downgrade() -> None:
    op.drop_index("ix_card_service_service_id", table_name="card_service")
    op.drop_index("ix_card_service_card_id", table_name="card_service")
    op.drop_index("ix_card_service_organization_id", table_name="card_service")
    op.drop_table("card_service")
