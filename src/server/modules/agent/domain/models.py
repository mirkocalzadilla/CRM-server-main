"""Modelos del esquema `agents` — multi-dominio, versionado, RAG.

Ref: docs/reference/agents.schema.sql (DDL de origen) · docs/SPECS_MVP.md
("M0 en detalle", tabla `conversation` + `funnel_stage`).
"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    FetchedValue,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.shared.base_model import Base, TimestampMixin, UUIDPrimaryKeyMixin

KB_EMBEDDING_DIM = 1536


class Product(Base):
    """Rubro/dominio de producto (ej. "cursos-mirko"). PK = slug."""

    __tablename__ = "product"

    slug: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str | None] = mapped_column(Text)
    domain_db_name: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AgentTemplate(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Plantilla reusable de agente para un producto (system_prompt + tools por defecto)."""

    __tablename__ = "agent_template"

    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    product_slug: Mapped[str] = mapped_column(
        ForeignKey("product.slug"), nullable=False, index=True
    )
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    default_model: Mapped[str] = mapped_column(Text, nullable=False)
    default_tools: Mapped[list[object]] = mapped_column(JSON, nullable=False, default=list)
    default_config: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)


class Agent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Agente IA configurado para una organización dentro de un producto."""

    __tablename__ = "agent"
    __table_args__ = (UniqueConstraint("organization_id", "product_slug"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    product_slug: Mapped[str] = mapped_column(
        ForeignKey("product.slug"), nullable=False, index=True
    )
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_template.id", ondelete="SET NULL")
    )
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    tools: Mapped[list[object]] = mapped_column(JSON, nullable=False, default=list)
    config: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # FK circular con agent_version (current_version_id -> agent_version.id, agent_version.agent_id -> agent.id):
    # use_alter para que se cree con ALTER TABLE una vez que ambas tablas existen.
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "agent_version.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_agent_current_version",
        ),
    )

    instances: Mapped[list["AgentInstance"]] = relationship(
        back_populates="agent",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class AgentVersion(Base, UUIDPrimaryKeyMixin):
    """Snapshot versionado de la configuración de un agente (auditoría / rollback)."""

    __tablename__ = "agent_version"
    __table_args__ = (UniqueConstraint("agent_id", "version_number"),)

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    tools: Mapped[list[object]] = mapped_column(JSON, nullable=False)
    config: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    change_summary: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AgentInstance(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Instancia desplegada de un agente (ej. un número de WhatsApp)."""

    __tablename__ = "agent_instance"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    whatsapp_number: Mapped[str | None] = mapped_column(Text, unique=True)
    handoff_whatsapp: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    agent: Mapped[Agent] = relationship(back_populates="instances", lazy="joined")
    conversations: Mapped[list["Conversation"]] = relationship(
        back_populates="instance",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class KbChunk(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Fragmento de conocimiento (RAG) embebido para una instancia de agente."""

    __tablename__ = "kb_chunk"

    instance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_instance.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(KB_EMBEDDING_DIM), nullable=False)
    # Columna en BD se llama `metadata`; renombrada a `meta` porque `metadata`
    # está reservado por DeclarativeBase (mismo criterio que Message.meta antes).
    meta: Mapped[dict[str, object]] = mapped_column("metadata", JSON, nullable=False, default=dict)


class AiChatHistory(Base, UUIDPrimaryKeyMixin):
    """Historial de turnos LLM, agrupado por `thread_id` (= conversation.id::text)."""

    __tablename__ = "ai_chat_histories"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    thread_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    message: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    message_order: Mapped[int] = mapped_column(
        BigInteger,
        autoincrement=True,
        nullable=False,
        server_default=FetchedValue(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    __table_args__ = (Index("idx_ai_history_thread_order", "thread_id", "message_order"),)


# Idempotencia de la ingesta: Meta reintenta el webhook, y sin esto un reintento duplica
# el turno del lead — el agente respondería dos veces y un comprobante se validaría dos
# veces. El wamid identifica el mensaje unívocamente y vive dentro del JSON, así que el
# índice es de expresión y parcial (los turnos del asistente no llevan wamid y no deben
# colisionar entre sí). `.as_string()` es la vía portable PG/SQLite que ya usa el repo.
Index(
    "uq_ai_history_org_wamid",
    AiChatHistory.organization_id,
    AiChatHistory.message["wamid"].as_string(),
    unique=True,
    postgresql_where=text("message ->> 'wamid' IS NOT NULL"),
    sqlite_where=text("json_extract(message, '$.wamid') IS NOT NULL"),
)


class AppChatHistory(Base, UUIDPrimaryKeyMixin):
    """Historial de mensajes humanos del CRM (`/crm`, takeover)."""

    __tablename__ = "app_chat_histories"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    session_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    sender: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    # Media saliente del takeover (#251): 'image' | 'document' + URL absoluta enviada.
    # NULL = mensaje de texto.
    media_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    __table_args__ = (Index("idx_app_history_session_time", "session_id", "message_time"),)


class HandoffEvent(Base, UUIDPrimaryKeyMixin):
    """Evento de traspaso a humano (dispara `is_ai_active=false` + resumen)."""

    __tablename__ = "handoff_event"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    thread_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    context: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    # Nota: `idx_handoff_unresolved` (índice parcial WHERE resolved_at IS NULL)
    # se crea en la migración — no es expresable de forma simple en __table_args__.


class Blacklist(Base, UUIDPrimaryKeyMixin):
    """Números bloqueados por organización (no reciben mensajes del agente)."""

    __tablename__ = "blacklist"
    __table_args__ = (UniqueConstraint("organization_id", "numero"),)
    # Nota: `blacklist_active_lookup_idx` (índice parcial WHERE estado = true)
    # se crea en la migración — no es expresable de forma simple en __table_args__.

    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    numero: Mapped[str] = mapped_column(Text, nullable=False)
    estado: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    fecha_creacion: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    fecha_actualizacion: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Conversation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Estado del lead: etapa del funnel + quién responde (IA vs. humano).

    Tabla nueva (no viene del dump de noxis) — ver SPECS_MVP "M0 en detalle".
    `ai_chat_histories.thread_id = conversation.id::text` agrupa el historial LLM.
    """

    __tablename__ = "conversation"
    # No UNIQUE on (organization_id, external_id): a phone holds MANY conversations
    # over time — one per opportunity. A closed lead that writes again starts a new
    # conversation (= new opportunity), the previous one stays as history (#163).
    __table_args__ = (
        Index("conv_org_external", "organization_id", "external_id"),
        Index("conv_instance", "instance_id"),
    )

    instance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_instance.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    funnel_stage: Mapped[FunnelStage] = mapped_column(
        Enum(
            FunnelStage,
            name="funnel_stage",
            values_callable=lambda obj: [e.value for e in obj],
        ),
        nullable=False,
        default=FunnelStage.NEW,
    )
    is_ai_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Lead's name; filled later by the agent (#91) or an operator. Copied to
    # contact.full_name when the opportunity is won.
    full_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    # AI case summary; refreshed on funnel advance and on handoff (#96). Shown in the
    # opportunity detail.
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set when this opportunity is closed (its card enters a won/lost stage); cleared
    # when reopened to an open stage. A closed conversation is never reused: the next
    # inbound from the lead opens a fresh conversation = a new opportunity (#163).
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # High-water mark: `message_order` of the latest lead turn the agent already
    # processed. A turn answers only inbounds above it, then advances it. Guards against
    # a follow-up that arrives while the previous turn is in flight being mistaken for an
    # already-answered/duplicate dispatch (its reply can outrank it by `message_order`).
    answered_through_order: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0", default=0
    )
    # High-water mark: lead `message_order` through which `ai_summary` was last refreshed.
    # A within-stage refresh fires once N new lead turns pile up above it, then it advances
    # (#254). Complements the on-stage-change / on-handoff refreshes so the summary can't
    # freeze while a lead heats up inside the same funnel stage.
    summarized_through_order: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0", default=0
    )

    instance: Mapped[AgentInstance] = relationship(back_populates="conversations", lazy="joined")


class Contact(Base, UUIDPrimaryKeyMixin):
    """Persistent person behind a phone number; created when an opportunity is won.

    `phone` mirrors `conversation.external_id` (value copy, not an FK). Base for the
    Contacts section and the lead's global history.
    """

    __tablename__ = "contact"
    __table_args__ = (UniqueConstraint("organization_id", "phone"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    phone: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
