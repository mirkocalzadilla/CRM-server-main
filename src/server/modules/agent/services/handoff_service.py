"""Side-effects de la derivación a humano (§9, M5).

Al derivar: genera un resumen con Sonnet (1 vez), escribe `handoff_event` y publica
al inbox `/crm` por Redis Pub/Sub. El resumen es **best-effort**: si el LLM falla
(sin saldo / error), se loguea y se sigue con summary vacío — nunca perdemos el
`handoff_event` ni la notificación. `is_ai_active=false` + funnel `handed_off` ya los
persistió el `save_turn` del turno.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.agent_state import State
from server.modules.agent.domain.llm_port import LLMPort
from server.modules.agent.domain.models import HandoffEvent
from server.modules.agent.domain.summary_transcript import summary_request
from server.modules.agent.repositories.conversation_repository import ConversationRepository
from server.modules.agent.repositories.handoff_event_repository import HandoffEventRepository
from server.shared.logger import get_logger
from server.shared.pubsub import Publisher, crm_channel

logger = get_logger(__name__)

SUMMARY_SYSTEM = (
    "Sos un asistente que resume conversaciones para el equipo humano de ventas. "
    "En 2-3 líneas y en español, resumí: qué busca el lead, en qué quedó la charla y "
    "por qué se derivó. Directo, sin saludos ni encabezados."
)


class HandoffService:
    """Implementa `HandoffPort`. Summarizer (Sonnet) + repo + publisher inyectados."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        summarizer: LLMPort,
        publisher: Publisher,
    ) -> None:
        self._session = session
        self._summarizer = summarizer
        self._repo = HandoffEventRepository(session)
        self._conv = ConversationRepository(session)
        self._publisher = publisher

    async def on_handoff(self, state: State, *, reason: str, reply: str) -> None:
        summary = await self._summarize(state, reply)

        await self._repo.add(
            HandoffEvent(
                agent_id=state.agent_id,
                organization_id=state.tenant_id,
                thread_id=str(state.conversation_id),
                reason=reason,
                context={"summary": summary, "from_stage": state.funnel_stage.value},
            )
        )
        # Espejamos el resumen en la conversación para el read-model unificado del CRM
        # (#96): el detalle de la oportunidad lee `conversation.ai_summary`.
        if summary:
            conversation = await self._conv.get_by_id(state.conversation_id, state.tenant_id)
            if conversation is not None:
                conversation.ai_summary = summary
        await self._session.commit()

        await self._publisher.publish(
            crm_channel(state.tenant_id),
            {
                "type": "handoff",
                "conversation_id": str(state.conversation_id),
                "external_id": state.external_id,
                "reason": reason,
                "summary": summary,
            },
        )
        logger.info("handoff.published", conversation_id=str(state.conversation_id), reason=reason)

    async def _summarize(self, state: State, reply: str) -> str:
        # One user message with the transcript, never a trailing assistant turn: that
        # would be a prefill, and the model would continue the chat instead (server#290).
        request = summary_request(state.messages_window, reply)
        if request is None:
            return ""
        try:
            turn = await self._summarizer.complete(
                system=SUMMARY_SYSTEM, messages=[request], tools=[]
            )
            return turn.text.strip()
        except Exception as exc:  # best-effort: el handoff no depende del resumen
            logger.warning(
                "handoff.summary_failed",
                conversation_id=str(state.conversation_id),
                error=str(exc),
            )
            return ""
