"""Modelos del tablero CRM (§"M-CRM"), basados en el CRM de Firefly.

Dos pipelines por organización (IA = espejo del `funnel_stage`; Gestión Postventa =
operación post-handoff). **1 card por `conversation`**. `card_move` = traceability.
Multi-tenant: `organization_id` en `pipeline`/`card`. Ref: docs/SPECS_MVP.md §"M-CRM".
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import DateTime as SADateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.shared.base_model import Base, TimestampMixin, UUIDPrimaryKeyMixin


class StageStatus(Base):
    """Lookup global del tipo de columna terminal (`open`/`won`/`lost`)."""

    __tablename__ = "stage_status"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    color: Mapped[str | None] = mapped_column(Text)


class Pipeline(Base, UUIDPrimaryKeyMixin):
    """Tablero por organización y tipo (`ia` | `human`)."""

    __tablename__ = "pipeline"
    __table_args__ = (
        CheckConstraint("kind IN ('ia','human')", name="pipeline_kind_check"),
        UniqueConstraint("organization_id", "kind", name="uq_pipeline_org_kind"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    stages: Mapped[list["Stage"]] = relationship(
        back_populates="pipeline",
        cascade="all, delete-orphan",
        order_by="Stage.position",
        lazy="selectin",
    )


class Stage(Base, UUIDPrimaryKeyMixin):
    """Columna ordenada de un pipeline; su `status_code` marca si es terminal."""

    __tablename__ = "stage"
    __table_args__ = (UniqueConstraint("pipeline_id", "position", name="uq_stage_pipeline_pos"),)

    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pipeline.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    status_code: Mapped[str] = mapped_column(
        Text, ForeignKey("stage_status.code"), nullable=False, default="open"
    )

    pipeline: Mapped[Pipeline] = relationship(back_populates="stages")


class Card(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Proyección CRM de una conversación. 1 card por `conversation` (UNIQUE)."""

    __tablename__ = "card"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversation.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    stage_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stage.id"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    # Notas libres del operador (ABM de oportunidad, #97/#54): "cliente enojado", etc.
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Explicit link to the lead's contact (#139). Nullable: a card may have no contact
    # yet. Set on manual contact creation from the detail and on the 'won' hook.
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contact.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Operational notices: why something automatic could not finish (missing lead name,
    # closed 24h window, extra receipt…). Codes, not copy — the CRM renders the Spanish
    # text. See `crm/domain/card_flags.py`.
    flags: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    # A person declared there is nothing pending here (0036). Counts as an answer for the
    # "sin responder" signal, so a conversation that ended with the lead's "gracias" stops
    # flagging; a later inbound re-arms it on its own. `attended_by` is text, not an FK:
    # the trace outlives the user, same as `card_move.moved_by`.
    attended_at: Mapped[datetime | None] = mapped_column(SADateTime(timezone=True))
    attended_by: Mapped[str | None] = mapped_column(Text, nullable=True)


class QrEntry(Base, UUIDPrimaryKeyMixin):
    """QR de acceso generado al validar el pago. 1 entrada por card (UNIQUE)."""

    __tablename__ = "qr_entry"

    card_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("card.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    qr_ref: Mapped[str] = mapped_column(Text, nullable=False)
    # A qué evento da acceso. `NULL` = entrada legacy, emitida antes de que los eventos
    # existieran: son legítimas, así que el escáner las deja pasar con una advertencia
    # en vez de rechazarlas.
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("event.id", ondelete="SET NULL"), index=True
    )
    # Fecha y lugar tal como estaban al emitirse. Copia deliberada: la entrada tiene que
    # seguir siendo legible en la puerta aunque después alguien edite o borre el evento.
    event_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    # Entrada anulada: el pago que la generó no se pudo confirmar contra el banco. El
    # lead ya la tiene en su teléfono, así que la única defensa es que el escáner la
    # rechace en la puerta — de ahí que se marque acá y no se borre la fila.
    revoked_at: Mapped[datetime | None] = mapped_column(SADateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(Text)
    # Consumo en la puerta. Una entrada es de **un solo uso**: `used_at` es lo que hace
    # que el segundo escaneo del mismo QR se rechace, y `used_by` deja quién lo escaneó.
    used_at: Mapped[datetime | None] = mapped_column(SADateTime(timezone=True))
    used_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Se consumió desde la lista de asistencia, sin escanear. Es la vía de escape cuando
    # la cámara no funciona, y la única forma de entrar sin mostrar el QR: queda marcada
    # para poder revisarla después del evento.
    used_manually: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        SADateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CardService(Base, UUIDPrimaryKeyMixin):
    """Servicio del catálogo asignado a una oportunidad (card). N por card (#132).

    `source`: `assigned` (el operador lo asignó a mano desde el detalle) | `captured`
    (lo eligió el bot en el pipeline, #133). `service_id` apunta al catálogo vivo; los
    servicios se dan de baja en lógico (`deleted_at`), así que la fila sigue resolviendo
    nombre/precio para el historial (#99) aunque el operador borre el servicio.
    """

    __tablename__ = "card_service"
    __table_args__ = (
        UniqueConstraint("card_id", "service_id", name="uq_card_service"),
        CheckConstraint("source IN ('assigned','captured')", name="card_service_source_check"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    card_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("card.id", ondelete="CASCADE"), nullable=False, index=True
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service.id"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(Text, nullable=False, default="assigned")
    created_at: Mapped[datetime] = mapped_column(
        SADateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CardMove(Base, UUIDPrimaryKeyMixin):
    """Historial de movimientos de una card (traceability de Firefly)."""

    __tablename__ = "card_move"

    card_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("card.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stage_from_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stage.id")
    )
    stage_to_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stage.id"), nullable=False
    )
    moved_by: Mapped[str] = mapped_column(Text, nullable=False)  # 'agent' | user_id::text
    # Motivo del move manual (audit, #253). Opcional: NULL en filas viejas y en los
    # syncs del bot; la obligatoriedad para Descalificado se impone en la UI.
    reason: Mapped[str | None] = mapped_column(Text)
    moved_at: Mapped[datetime] = mapped_column(
        SADateTime(timezone=True), server_default=func.now(), nullable=False
    )
