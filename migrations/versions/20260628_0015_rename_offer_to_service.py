"""rename offer→service: tabla, índices, constraints, soft delete + snapshot JSON

Revision ID: 0015_rename_offer_to_service
Revises: 0014_conversation_ai_summary
Create Date: 2026-06-28 00:00:00.000000+00:00

Unifica la nomenclatura del catálogo a "servicios" (server #131). Renombra la
tabla `offer→service` con sus índices/constraints, agrega `service.deleted_at`
(baja lógica: la referencia nunca se pierde) y reescribe la clave del snapshot
publicado `config.ofertas → config.services` en `agent`, `agent_version` y
`agent_template` para que el bot siga leyendo el catálogo sin re-publicar.
Reversible (down deshace cada paso).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_rename_offer_to_service"
down_revision: str | None = "0014_conversation_ai_summary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (tabla, columna JSON) que pueden contener el snapshot del catálogo.
_CONFIG_COLUMNS = (
    ("agent", "config"),
    ("agent_version", "config"),
    ("agent_template", "default_config"),
)


def _rename_json_key(table: str, column: str, src: str, dst: str) -> None:
    """Renombra la clave de primer nivel `src→dst` en una columna JSON (vía jsonb)."""
    op.execute(
        f"UPDATE {table} SET {column} = "
        f"jsonb_set({column}::jsonb - '{src}', '{{{dst}}}', {column}::jsonb -> '{src}')::json "
        f"WHERE jsonb_exists({column}::jsonb, '{src}')"
    )


def upgrade() -> None:
    op.rename_table("offer", "service")
    op.execute("ALTER INDEX offer_pkey RENAME TO service_pkey")
    op.execute("ALTER INDEX ix_offer_organization_id RENAME TO ix_service_organization_id")
    op.execute("ALTER INDEX ix_offer_agent_id RENAME TO ix_service_agent_id")
    op.execute("ALTER TABLE service RENAME CONSTRAINT uq_offer_agent_slug TO uq_service_agent_slug")
    op.execute("ALTER TABLE service RENAME CONSTRAINT offer_agent_id_fkey TO service_agent_id_fkey")
    op.execute("ALTER TABLE service RENAME CONSTRAINT offer_asset_id_fkey TO service_asset_id_fkey")

    op.add_column("service", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))

    for table, column in _CONFIG_COLUMNS:
        _rename_json_key(table, column, "ofertas", "services")


def downgrade() -> None:
    for table, column in _CONFIG_COLUMNS:
        _rename_json_key(table, column, "services", "ofertas")

    op.drop_column("service", "deleted_at")

    op.execute("ALTER TABLE service RENAME CONSTRAINT service_asset_id_fkey TO offer_asset_id_fkey")
    op.execute("ALTER TABLE service RENAME CONSTRAINT service_agent_id_fkey TO offer_agent_id_fkey")
    op.execute("ALTER TABLE service RENAME CONSTRAINT uq_service_agent_slug TO uq_offer_agent_slug")
    op.execute("ALTER INDEX ix_service_agent_id RENAME TO ix_offer_agent_id")
    op.execute("ALTER INDEX ix_service_organization_id RENAME TO ix_offer_organization_id")
    op.execute("ALTER INDEX service_pkey RENAME TO offer_pkey")
    op.rename_table("service", "offer")
