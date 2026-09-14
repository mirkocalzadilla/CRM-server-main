"""agents schema, conversation y funnel_stage

Revision ID: ad9ab2d1f065
Revises: 33feacab726f
Create Date: 2026-06-06 05:44:41.268614+00:00

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "ad9ab2d1f065"
down_revision: str | None = "33feacab726f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KB_EMBEDDING_DIM = 1536


def upgrade() -> None:
    # --- extensiones que requiere el esquema `agents` (citext: requerida por el dump de origen) ---
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # --- drop del módulo plano viejo: no hay datos de producción, se reescribe sobre `agents` ---
    op.execute("DROP TABLE IF EXISTS messages CASCADE")
    op.execute("DROP TABLE IF EXISTS conversations CASCADE")
    op.execute("DROP TABLE IF EXISTS agents CASCADE")
    op.execute("DROP TYPE IF EXISTS conversation_status")
    op.execute("DROP TYPE IF EXISTS message_role")

    # --- esquema `agents` (orden respeta FKs): product -> agent_template -> agent -> agent_version -> agent_instance -> kb_chunk ---
    op.create_table(
        "product",
        sa.Column("slug", sa.Text(), primary_key=True, nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=True),
        sa.Column("domain_db_name", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "agent_template",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("slug", sa.Text(), nullable=False, unique=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column(
            "product_slug", sa.Text(), sa.ForeignKey("product.slug"), nullable=False
        ),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("default_model", sa.Text(), nullable=False),
        sa.Column(
            "default_tools",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "default_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("idx_agent_template_product", "agent_template", ["product_slug"])

    op.create_table(
        "agent",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_slug", sa.Text(), sa.ForeignKey("product.slug"), nullable=False),
        sa.Column(
            "template_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_template.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column(
            "tools",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        # Sin FK todavía: agent_version no existe aún (dependencia circular, se cierra más abajo).
        sa.Column("current_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("organization_id", "product_slug"),
    )
    op.create_index("idx_agent_organization", "agent", ["organization_id"])
    op.create_index("idx_agent_product", "agent", ["product_slug"])

    op.create_table(
        "agent_version",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("tools", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("change_summary", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("agent_id", "version_number"),
    )
    op.create_index("idx_agent_version_agent", "agent_version", ["agent_id"])

    # Cierra la FK circular agent.current_version_id -> agent_version.id (mismo orden que el dump de origen).
    op.create_foreign_key(
        "fk_agent_current_version",
        "agent",
        "agent_version",
        ["current_version_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "agent_instance",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("whatsapp_number", sa.Text(), nullable=True, unique=True),
        sa.Column("handoff_whatsapp", sa.Text(), nullable=True),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("agent_instance_agent_id_idx", "agent_instance", ["agent_id"])

    op.create_table(
        "kb_chunk",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "instance_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_instance.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(KB_EMBEDDING_DIM), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("kb_chunk_instance_id_idx", "kb_chunk", ["instance_id"])
    op.create_index(
        "kb_chunk_embedding_hnsw_idx",
        "kb_chunk",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    op.create_table(
        "ai_chat_histories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("message", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("message_order", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    # `message_order` lo asigna una secuencia dedicada (igual que el dump de origen),
    # OWNED BY ata su ciclo de vida a la columna: se borra sola al hacer DROP TABLE.
    op.execute(
        "CREATE SEQUENCE ai_chat_histories_message_order_seq "
        "START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1"
    )
    op.execute(
        "ALTER SEQUENCE ai_chat_histories_message_order_seq "
        "OWNED BY ai_chat_histories.message_order"
    )
    op.execute(
        "ALTER TABLE ai_chat_histories ALTER COLUMN message_order "
        "SET DEFAULT nextval('ai_chat_histories_message_order_seq'::regclass)"
    )
    op.create_index("idx_ai_history_agent", "ai_chat_histories", ["agent_id"])
    op.create_index("idx_ai_history_org", "ai_chat_histories", ["organization_id"])
    op.create_index("idx_ai_history_thread", "ai_chat_histories", ["thread_id"])
    op.create_index("idx_ai_history_session", "ai_chat_histories", ["session_id"])
    op.create_index(
        "idx_ai_history_thread_order", "ai_chat_histories", ["thread_id", "message_order"]
    )
    op.create_index(
        "idx_ai_history_created_at", "ai_chat_histories", [sa.text("created_at DESC")]
    )

    op.create_table(
        "app_chat_histories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("sender", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "message_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("idx_app_history_agent", "app_chat_histories", ["agent_id"])
    op.create_index("idx_app_history_session", "app_chat_histories", ["session_id"])
    op.create_index(
        "idx_app_history_session_time", "app_chat_histories", ["session_id", "message_time"]
    )

    op.create_table(
        "handoff_event",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "context",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("idx_handoff_agent", "handoff_event", ["agent_id"])
    op.create_index("idx_handoff_thread", "handoff_event", ["thread_id"])
    op.create_index("idx_handoff_reason", "handoff_event", ["reason"])
    op.create_index(
        "idx_handoff_unresolved",
        "handoff_event",
        ["agent_id", sa.text("created_at DESC")],
        postgresql_where=sa.text("resolved_at IS NULL"),
    )

    op.create_table(
        "blacklist",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("numero", sa.Text(), nullable=False),
        sa.Column("estado", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "fecha_creacion",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "fecha_actualizacion",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("organization_id", "numero"),
    )
    op.create_index(
        "blacklist_active_lookup_idx",
        "blacklist",
        ["organization_id", "numero"],
        postgresql_where=sa.text("estado = true"),
    )

    # --- conversation: tabla nueva (no viene del dump) — estado del lead: funnel + control IA ---
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE funnel_stage AS ENUM (
                'new', 'engaging', 'qualifying', 'qualified', 'handed_off', 'disqualified'
            );
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
    """)

    op.create_table(
        "conversation",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "instance_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_instance.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=False),
        sa.Column(
            "funnel_stage",
            postgresql.ENUM(
                "new",
                "engaging",
                "qualifying",
                "qualified",
                "handed_off",
                "disqualified",
                name="funnel_stage",
                create_type=False,
            ),
            nullable=False,
            server_default="new",
        ),
        sa.Column("is_ai_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("organization_id", "external_id"),
    )
    op.create_index("conv_org_external", "conversation", ["organization_id", "external_id"])
    op.create_index("conv_instance", "conversation", ["instance_id"])


def downgrade() -> None:
    op.drop_index("conv_instance", table_name="conversation")
    op.drop_index("conv_org_external", table_name="conversation")
    op.drop_table("conversation")
    postgresql.ENUM(name="funnel_stage").drop(op.get_bind(), checkfirst=True)

    op.drop_index("blacklist_active_lookup_idx", table_name="blacklist")
    op.drop_table("blacklist")

    op.drop_index("idx_handoff_unresolved", table_name="handoff_event")
    op.drop_index("idx_handoff_reason", table_name="handoff_event")
    op.drop_index("idx_handoff_thread", table_name="handoff_event")
    op.drop_index("idx_handoff_agent", table_name="handoff_event")
    op.drop_table("handoff_event")

    op.drop_index("idx_app_history_session_time", table_name="app_chat_histories")
    op.drop_index("idx_app_history_session", table_name="app_chat_histories")
    op.drop_index("idx_app_history_agent", table_name="app_chat_histories")
    op.drop_table("app_chat_histories")

    op.drop_index("idx_ai_history_created_at", table_name="ai_chat_histories")
    op.drop_index("idx_ai_history_thread_order", table_name="ai_chat_histories")
    op.drop_index("idx_ai_history_session", table_name="ai_chat_histories")
    op.drop_index("idx_ai_history_thread", table_name="ai_chat_histories")
    op.drop_index("idx_ai_history_org", table_name="ai_chat_histories")
    op.drop_index("idx_ai_history_agent", table_name="ai_chat_histories")
    # DROP TABLE arrastra la secuencia (OWNED BY ata su ciclo de vida a la columna).
    op.drop_table("ai_chat_histories")

    op.drop_index("kb_chunk_embedding_hnsw_idx", table_name="kb_chunk")
    op.drop_index("kb_chunk_instance_id_idx", table_name="kb_chunk")
    op.drop_table("kb_chunk")

    op.drop_index("agent_instance_agent_id_idx", table_name="agent_instance")
    op.drop_table("agent_instance")

    op.drop_constraint("fk_agent_current_version", "agent", type_="foreignkey")

    op.drop_index("idx_agent_version_agent", table_name="agent_version")
    op.drop_table("agent_version")

    op.drop_index("idx_agent_product", table_name="agent")
    op.drop_index("idx_agent_organization", table_name="agent")
    op.drop_table("agent")

    op.drop_index("idx_agent_template_product", table_name="agent_template")
    op.drop_table("agent_template")

    op.drop_table("product")

    # No se recrean `agents`/`conversations`/`messages`: el módulo plano que las
    # consumía fue eliminado (BITACORA, "borrado del módulo agent plano"). Bajar
    # de esta revisión dejaría el código sin su contraparte de esquema — punto
    # sin retorno intencional, acorde a "no hay datos de producción" (SPECS_MVP M0).
