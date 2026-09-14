"""agent: initial schema (agents, conversations, messages)

Revision ID: 0003_agent_initial
Revises: 0001_core_initial
Create Date: 2026-05-08

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003_agent_initial"
down_revision: str | None = "0001_core_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "model",
            sa.String(length=120),
            nullable=False,
            server_default="claude-sonnet-4-6",
        ),
        sa.Column(
            "temperature", sa.Float(), nullable=False, server_default=sa.text("0.7")
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_agents_tenant_id", "agents", ["tenant_id"])

    op.execute(sa.text("""
        DO $$ BEGIN
            CREATE TYPE conversation_status AS ENUM ('open', 'closed');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
    """))

    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("contact_name", sa.String(length=255), nullable=True),
        sa.Column("contact_phone", sa.String(length=64), nullable=True),
        sa.Column("contact_email", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM("open", "closed", name="conversation_status", create_type=False),
            nullable=False,
            server_default="open",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_conversations_tenant_id", "conversations", ["tenant_id"])
    op.create_index("ix_conversations_agent_id", "conversations", ["agent_id"])
    op.create_index("ix_conversations_external_id", "conversations", ["external_id"])

    op.execute(sa.text("""
        DO $$ BEGIN
            CREATE TYPE message_role AS ENUM ('user', 'assistant', 'system');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
    """))

    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "role",
            postgresql.ENUM("user", "assistant", "system", name="message_role", create_type=False),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])


def downgrade() -> None:
    op.drop_index("ix_messages_conversation_id", table_name="messages")
    op.drop_table("messages")

    message_role = postgresql.ENUM(
        "user", "assistant", "system", name="message_role", create_type=False
    )
    message_role.drop(op.get_bind(), checkfirst=True)

    op.drop_index("ix_conversations_external_id", table_name="conversations")
    op.drop_index("ix_conversations_agent_id", table_name="conversations")
    op.drop_index("ix_conversations_tenant_id", table_name="conversations")
    op.drop_table("conversations")

    conversation_status = postgresql.ENUM(
        "open", "closed", name="conversation_status", create_type=False
    )
    conversation_status.drop(op.get_bind(), checkfirst=True)

    op.drop_index("ix_agents_tenant_id", table_name="agents")
    op.drop_table("agents")
