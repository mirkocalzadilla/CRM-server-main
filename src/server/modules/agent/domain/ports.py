"""Ports the orchestrator depends on (§5.2, §5.3).

The orchestrator never touches SQLAlchemy or the Meta SDK directly: it talks to
these Protocols. M0 (modelos/repos) and Christian's `WhatsAppSender` satisfy them
at integration; until then a console stub does. This is the seam that lets M4 be
developed and tested in isolation against the contract (SPECS_MVP §"Contratos").
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from server.modules.agent.domain.agent_state import State
from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.modules.agent.domain.llm_port import Message


@dataclass(frozen=True, slots=True)
class ConversationSnapshot:
    """Everything `load_context` returns for one turn. `messages_window` is the
    last <=10 turns (working memory) and ends with the inbound user message.
    `tenant_id` scopes queries only — it never reaches the LLM (§5.3).
    """

    external_id: str  # wa_id of the lead; the `to` when sending
    agent_id: uuid.UUID
    funnel_stage: FunnelStage
    is_ai_active: bool
    system_prompt: str
    config: Mapping[str, object]
    messages_window: tuple[Message, ...] = field(default_factory=tuple)
    full_name: str | None = None  # lead's name if already on the conversation (#91)
    # `message_order` of the newest lead turn in the window (None if none), and how far
    # the agent has already answered. A turn runs iff `latest_user_order` outranks
    # `answered_through_order`; otherwise it's a duplicate/catch-up dispatch and is
    # skipped. The window is already trimmed to end at `latest_user_order` (#240).
    latest_user_order: int | None = None
    answered_through_order: int = 0
    # Lead turns accumulated above `conversation.summarized_through_order`: how many of the
    # lead's inbounds landed since `ai_summary` was last refreshed. Drives the within-stage
    # refresh (#254); the orchestrator refreshes once it reaches the configured threshold.
    lead_turns_since_summary: int = 0


@dataclass(frozen=True, slots=True)
class OutboundMedia:
    """A media message (image/document) the agent sent to the lead this turn,
    mirrored into the CRM thread (#175). `url` is an absolute link (catalog asset
    or payment QR), not a `/media/` disk path like inbound WhatsApp media.
    """

    media_type: str  # 'image' | 'document'
    url: str
    caption: str = ""
    filename: str = ""  # only meaningful for documents (WhatsApp send)


class ConversationStore(Protocol):
    """Persistence port (satisfied by M0's repo or an adapter over it).

    Contract aligns with SPECS_MVP: tenant-scoped access keyed by
    `conversation_id` + `tenant_id` (organization_id).
    """

    async def load(
        self, conversation_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> ConversationSnapshot | None: ...

    async def save_turn(
        self,
        conversation_id: uuid.UUID,
        tenant_id: uuid.UUID,
        *,
        reply_text: str,
        funnel_stage: FunnelStage,
        is_ai_active: bool,
        answered_order: int,
        handoff_reason: str | None = None,
        media: Sequence[OutboundMedia] = (),
    ) -> None: ...

    async def save_full_name(
        self, conversation_id: uuid.UUID, tenant_id: uuid.UUID, name: str
    ) -> None:
        """Persist the lead's name to `conversation.full_name` (#91). The 'won' hook
        copies it to the contact when the opportunity closes."""
        ...

    async def save_agent_error(
        self, conversation_id: uuid.UUID, tenant_id: uuid.UUID, *, category: str, detail: str
    ) -> None:
        """Persist a system event in the thread recording that the agent failed this
        turn (server#288): a `role: system` row the CRM renders as an error chip and
        the LLM window never loads. `category` is an `LLMError` code (or 'internal' /
        'delivery' from the dispatch handler); `detail` is a truncated diagnostic."""
        ...


class MessageSender(Protocol):
    """Outbound channel port. Matches `WhatsAppSender` (send_text/send_image/send_document)."""

    async def send_text(self, to: str, body: str) -> None: ...

    async def send_image(self, to: str, link: str, caption: str = "") -> None: ...

    async def send_document(self, to: str, link: str, filename: str, caption: str = "") -> None: ...


class HandoffPort(Protocol):
    """Side-effects al derivar a humano (§9, M5): resumen Sonnet + `handoff_event`
    + Pub/Sub al inbox. Inyectado en el orquestador; `None` = no-op (smoke/tests M4).
    """

    async def on_handoff(self, state: State, *, reason: str, reply: str) -> None: ...


class SummaryPort(Protocol):
    """Refresca el resumen del caso por IA (#96) al avanzar de etapa o por acumulación de
    turnos dentro de la misma etapa (#254). Best-effort: persiste `conversation.ai_summary`.
    `through_order` es el `message_order` del lead hasta el que se resume — avanza el
    high-water mark (`summarized_through_order`) para el throttle. `None` = no-op."""

    async def refresh(self, state: State, *, reply: str, through_order: int) -> None: ...


class ServiceCapturePort(Protocol):
    """Estampa en la card el/los servicio(s) que el bot le envió al lead en el turno
    (#133): `card_service` con `source='captured'`. Best-effort e idempotente (no
    duplica). `None` = no-op (smoke/tests M4 sin CRM)."""

    async def on_captured(self, state: State, slugs: tuple[str, ...]) -> None: ...
