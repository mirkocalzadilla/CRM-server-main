"""contact: tabla contact + columna conversation.full_name (base de Contactos)

Revision ID: 0012_contact_full_name
Revises: 0011_rename_ia_pipeline
Create Date: 2026-06-27 00:00:00.000000+00:00

Cimientos de datos de la sección de Contactos (issue #122). Agrega
`conversation.full_name` (nombre del lead, nullable) y la tabla `contact`
(persona persistente por teléfono, unique por org+phone). No incluye servicio,
hook de cierre ni endpoints (issues #101 / #99).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0012_contact_full_name"
down_revision: str | None = "0011_rename_ia_pipeline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversation", sa.Column("full_name", sa.Text(), nullable=True))

    op.create_table(
        "contact",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("full_name", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("organization_id", "phone", name="uq_contact_org_phone"),
    )
    op.create_index("ix_contact_organization_id", "contact", ["organization_id"])


def downgrade() -> None:
    op.drop_index("ix_contact_organization_id", table_name="contact")
    op.drop_table("contact")
    op.drop_column("conversation", "full_name")
