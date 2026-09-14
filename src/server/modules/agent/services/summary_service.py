"""Resumen del caso por IA (#96, #254).

Regeneramos un resumen breve (Haiku) y lo persistimos en `conversation.ai_summary`
cuando el lead avanza de `funnel_stage` sin derivar, o por acumulación de turnos dentro
de la misma etapa (el orquestador aplica el throttle de N turnos). Best-effort: si el LLM
falla, se loguea y el turno sigue (el resumen es complementario; nunca pisa un resumen
previo con vacío). Cada refresh avanza `summarized_through_order` (mark del throttle). El
handoff tiene su propio resumen (Sonnet) en `HandoffService`.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.agent_state import State
from server.modules.agent.domain.llm_port import LLMPort
from server.modules.agent.domain.summary_transcript import summary_request
from server.modules.agent.repositories.conversation_repository import ConversationRepository
from server.shared.logger import get_logger

logger = get_logger(__name__)

SUMMARY_SYSTEM = (
    "Sos un asistente que resume conversaciones para el equipo de ventas. En 1-2 líneas "
    "y en español, resumí qué busca el lead y en qué quedó la charla. Directo, sin saludos "
    "ni encabezados."
)


class SummaryService:
    """Implementa `SummaryPort`. Summarizer (Haiku) + repo inyectados."""

    def __init__(self, *, session: AsyncSession, summarizer: LLMPort) -> None:
        self._session = session
        self._summarizer = summarizer
        self._conv = ConversationRepository(session)

    async def refresh(self, state: State, *, reply: str, through_order: int) -> None:
        summary = await self._summarize(state, reply)
        conversation = await self._conv.get_by_id(state.conversation_id, state.tenant_id)
        if conversation is None:
            return
        if summary:  # best-effort: no pisamos un resumen previo con vacío
            conversation.ai_summary = summary
        # Advance the mark even when the LLM failed: the throttle counts attempts, not
        # successes, so a persistent failure can't re-fire a refresh every turn (#254).
        conversation.summarized_through_order = through_order
        await self._session.commit()

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
        except Exception as exc:  # best-effort: el turno no depende del resumen
            logger.warning(
                "summary.refresh_failed",
                conversation_id=str(state.conversation_id),
                error=str(exc),
            )
            return ""
