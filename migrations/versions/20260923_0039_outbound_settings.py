"""outbound_settings — reglas de reactivación de leads por organización (M-Outbound D)

Revision ID: 0039_outbound_settings
Revises: 0038_outbound_messages
Create Date: 2026-09-23 12:00:00.000000+00:00

La reactivación manda plantillas de MARKETING (Meta las cobra y penaliza los bloqueos),
así que arranca **apagada** por organización: nada sale hasta que el negocio la activa
y escribe la novedad del mes. Reglas en JSON (`[{"stages": [...], "days": N}]`) para
que el CRM las edite sin migración.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0039_outbound_settings"
down_revision: str | None = "0038_outbound_messages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outbound_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reactivation_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reactivation_daily_cap", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("reactivation_recontact_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column(
            "reactivation_rules",
            postgresql.JSON(),
            nullable=False,
            server_default='[{"stages": ["engaging", "qualified"], "days": 7}, '
            '{"stages": ["new"], "days": 14}]',
        ),
        sa.Column("novelty_text", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("organization_id", name="uq_outbound_settings_org"),
    )


def downgrade() -> None:
    op.drop_table("outbound_settings")
