"""`ConversationStore` adapter sobre los repos/modelos de M0 (§5.2, §5.3).

Traduce entre el mundo del orquestador (`ConversationSnapshot`, `llm_port.Message`)
y el esquema `agents` (`conversation`, `ai_chat_histories`). `tenant_id` =
`organization_id`: scopea las queries, nunca entra al contexto del LLM.

Frontera transaccional: `save_turn` commitea la persistencia (funnel + turno)
antes de que el orquestador haga el envío saliente, para que el turno quede
durable aunque el `send` a Meta falle.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.modules.agent.domain.llm_port import Message, Role
from server.modules.agent.domain.models import AiChatHistory
from server.modules.agent.domain.ports import ConversationSnapshot, OutboundMedia
from server.modules.agent.repositories.ai_chat_history_repository import AiChatHistoryRepository
from server.modules.agent.repositories.conversation_repository import ConversationRepository
from server.shared.logger import get_logger

logger = get_logger(__name__)

_HISTORY_WINDOW = 10

# UI copy of the thread's system event when the agent fails a turn (server#288). The
# category code travels alongside so the CRM can qualify it (card_flags philosophy).
AGENT_ERROR_NOTE = "El agente IA no pudo responder este mensaje."
_ERROR_DETAIL_MAX = 300


def window_up_to_latest_user(
    ordered: list[tuple[int, Message]],
) -> tuple[tuple[Message, ...], int | None]:
    """From `(message_order, Message)` pairs in ascending order, build the working window
    and report the newest lead turn's order (`None` if there's no user turn).

    The window is cut to end at that lead turn: rows outranking it are replies from a turn
    still in flight that persisted late (their `message_order` leapfrogged this inbound).
    Dropping them makes the window end on the lead's message — what the turn guard and the
    Anthropic API (must end with `user`) both need (#240). Then trimmed to the first user.
    """
    latest_user_order = max(
        (order for order, msg in ordered if msg.role is Role.USER), default=None
    )
    if latest_user_order is not None:
        ordered = [pair for pair in ordered if pair[0] <= latest_user_order]
    return _trim_to_first_user(tuple(msg for _, msg in ordered)), latest_user_order


def _trim_to_first_user(window: tuple[Message, ...]) -> tuple[Message, ...]:
    """Return the window starting at its first user message.

    Anthropic's Messages API requires the array to start with role `user`; the
    recency window can begin on an assistant turn once history scrolls past
    `_HISTORY_WINDOW`, which would 400 every turn for established leads. Dropping
    leading non-user turns is provider-neutral (harmless for OpenAI, which prepends
    a system message). The latest turn is always the inbound user message, so the
    trimmed window is never empty in practice.
    """
    for index, message in enumerate(window):
        if message.role is Role.USER:
            return window[index:]
    return ()


def stored_message_to_llm(stored: Mapping[str, object]) -> Message | None:
    """Convierte una fila `ai_chat_histories.message` (`{role, content}`) a `Message`.

    Devuelve `None` si el rol no es user/assistant (turnos no proyectables al LLM).
    """
    raw_role = str(stored.get("role", ""))
    try:
        role = Role(raw_role)
    except ValueError:
        return None
    return Message(role=role, text=str(stored.get("content", "")))


class ConversationStoreAdapter:
    """Implementa el port `ConversationStore` contra la DB (tenant-scoped)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._conv_repo = ConversationRepository(session)
        self._history_repo = AiChatHistoryRepository(session)

    async def load(
        self, conversation_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> ConversationSnapshot | None:
        conversation = await self._conv_repo.get_by_id(conversation_id, tenant_id)
        if conversation is None:
            return None

        agent = conversation.instance.agent  # instance/agent eager (lazy="joined")
        rows = await self._history_repo.list_recent(str(conversation_id), _HISTORY_WINDOW)
        ordered = [
            (row.message_order, msg)
            for row in rows
            if (msg := stored_message_to_llm(row.message)) is not None
        ]
        window, latest_user_order = window_up_to_latest_user(ordered)
        # Lead turns piled up above the summary high-water mark: counted from the loaded
        # rows (no extra query). Turn-by-turn evaluation keeps this exact for small N —
        # at most a handful of rows land between two inbounds, well inside the window (#254).
        lead_turns_since_summary = sum(
            1
            for order, msg in ordered
            if msg.role is Role.USER and order > conversation.summarized_through_order
        )
        return ConversationSnapshot(
            external_id=conversation.external_id,
            agent_id=conversation.instance.agent_id,
            funnel_stage=conversation.funnel_stage,
            is_ai_active=conversation.is_ai_active,
            system_prompt=agent.system_prompt,
            config=agent.config,
            messages_window=window,
            full_name=conversation.full_name,
            latest_user_order=latest_user_order,
            answered_through_order=conversation.answered_through_order,
            lead_turns_since_summary=lead_turns_since_summary,
        )

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
    ) -> None:
        conversation = await self._conv_repo.get_by_id(conversation_id, tenant_id)
        if conversation is None:  # carrera improbable: la conversación desapareció
            logger.warning(
                "agent.save_turn_missing_conversation", conversation_id=str(conversation_id)
            )
            return

        conversation.funnel_stage = funnel_stage
        conversation.is_ai_active = is_ai_active
        # Advance the high-water mark: this turn consumed every lead inbound up to
        # `answered_order`, so later dispatches for them are skipped as duplicates (#240).
        conversation.answered_through_order = answered_order

        agent_id = conversation.instance.agent_id
        if reply_text:
            await self._history_repo.add(
                AiChatHistory(
                    agent_id=agent_id,
                    organization_id=tenant_id,
                    thread_id=str(conversation_id),
                    session_id=conversation.external_id,
                    message={"role": "assistant", "content": reply_text, "channel": "whatsapp"},
                )
            )
        # Media saliente (#175): materiales del catálogo / QR que el agente le manda al
        # lead. `media_url` es absoluta (el mirror la usa tal cual, sin base_url).
        for item in media:
            await self._history_repo.add(
                AiChatHistory(
                    agent_id=agent_id,
                    organization_id=tenant_id,
                    thread_id=str(conversation_id),
                    session_id=conversation.external_id,
                    message={
                        "role": "assistant",
                        "content": item.caption,
                        "channel": "whatsapp",
                        "media_type": item.media_type,
                        "media_url": item.url,
                    },
                )
            )
        # handoff_event + resumen Sonnet + Pub/Sub → M5. Acá solo persistimos el
        # estado (is_ai_active=false silencia al agente; el funnel ya quedó en handed_off).
        await self._session.commit()

    async def save_full_name(
        self, conversation_id: uuid.UUID, tenant_id: uuid.UUID, name: str
    ) -> None:
        conversation = await self._conv_repo.get_by_id(conversation_id, tenant_id)
        if conversation is None:  # carrera improbable: la conversación desapareció
            logger.warning(
                "agent.save_full_name_missing_conversation", conversation_id=str(conversation_id)
            )
            return
        conversation.full_name = name
        await self._session.commit()

    async def save_agent_error(
        self, conversation_id: uuid.UUID, tenant_id: uuid.UUID, *, category: str, detail: str
    ) -> None:
        conversation = await self._conv_repo.get_by_id(conversation_id, tenant_id)
        if conversation is None:  # carrera improbable: la conversación desapareció
            logger.warning(
                "agent.save_agent_error_missing_conversation",
                conversation_id=str(conversation_id),
            )
            return
        # `role: system` queda fuera de la ventana del LLM (`stored_message_to_llm` lo
        # descarta) y el mirror lo proyecta como evento de error del hilo (server#288).
        await self._history_repo.add(
            AiChatHistory(
                agent_id=conversation.instance.agent_id,
                organization_id=tenant_id,
                thread_id=str(conversation_id),
                session_id=conversation.external_id,
                message={
                    "role": "system",
                    "kind": "agent_error",
                    "content": AGENT_ERROR_NOTE,
                    "category": category,
                    "detail": detail[:_ERROR_DETAIL_MAX],
                },
            )
        )
        # Commit propio: el evento debe quedar durable aunque el resto del turno de
        # fallback (save_turn / envío) no llegue a completarse.
        await self._session.commit()
