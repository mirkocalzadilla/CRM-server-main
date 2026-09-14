"""qr_entry: tabla de entradas QR de acceso al evento

Revision ID: 0008_qr_entry
Revises: 0007_rbac_3_niveles
Create Date: 2026-06-06 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0008_qr_entry"
down_revision: str | None = "0007_rbac_3_niveles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "qr_entry",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "card_id",
            UUID(as_uuid=True),
            sa.ForeignKey("card.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("token", sa.Text(), nullable=False, unique=True),
        sa.Column("qr_ref", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_qr_entry_card_id", "qr_entry", ["card_id"])


def downgrade() -> None:
    op.drop_index("ix_qr_entry_card_id", table_name="qr_entry")
    op.drop_table("qr_entry")
