"""crm board tables: pipeline, stage_status, stage, card, card_move

Revision ID: c4f1a2b3d5e6
Revises: ad9ab2d1f065
Create Date: 2026-06-06 18:40:00.000000+00:00

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c4f1a2b3d5e6"
down_revision: str | None = "ad9ab2d1f065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- tablero CRM (orden respeta FKs): stage_status -> pipeline -> stage -> card -> card_move ---
    op.create_table(
        "stage_status",
        sa.Column("code", sa.Text(), primary_key=True, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("color", sa.Text(), nullable=True),
    )

    op.create_table(
        "pipeline",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.CheckConstraint("kind IN ('ia','human')", name="pipeline_kind_check"),
        sa.UniqueConstraint("organization_id", "kind", name="uq_pipeline_org_kind"),
    )
    op.create_index("idx_pipeline_org", "pipeline", ["organization_id"])

    op.create_table(
        "stage",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "pipeline_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pipeline.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "status_code",
            sa.Text(),
            sa.ForeignKey("stage_status.code"),
            nullable=False,
            server_default=sa.text("'open'"),
        ),
        sa.UniqueConstraint("pipeline_id", "position", name="uq_stage_pipeline_pos"),
    )
    op.create_index("idx_stage_pipeline", "stage", ["pipeline_id"])

    op.create_table(
        "card",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversation.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "stage_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("stage.id"),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("idx_card_org", "card", ["organization_id"])
    op.create_index("idx_card_stage", "card", ["stage_id"])

    op.create_table(
        "card_move",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "card_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("card.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage_from_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("stage.id"), nullable=True),
        sa.Column(
            "stage_to_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("stage.id"),
            nullable=False,
        ),
        sa.Column("moved_by", sa.Text(), nullable=False),
        sa.Column(
            "moved_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("idx_card_move_card", "card_move", ["card_id"])


def downgrade() -> None:
    op.drop_table("card_move")
    op.drop_table("card")
    op.drop_table("stage")
    op.drop_table("pipeline")
    op.drop_table("stage_status")
