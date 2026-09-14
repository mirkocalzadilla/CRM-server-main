"""Catch-up de inbounds sin atender (#187).

Cuando el sistema estuvo caído (worker abajo, o la cola `agent:dispatch` de Redis se
perdió/vació), quedan leads cuyo último mensaje es un `user` sin respuesta del agente:
el inbound se persistió en `ai_chat_histories` pero su item de dispatch nunca llegó a
procesarse. `run_catch_up` re-encola esas conversaciones para que el worker las conteste
al recuperarse, en vez de perderlas.

Idempotente: `AgentService.process_message` corta si el inbound ya está en o por debajo
del high-water mark `answered_through_order` (dispatch duplicado), y el lock por
conversación serializa cualquier duplicado. Lo corre el worker una vez al arrancar
(`server.worker.main`).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.repositories.ai_chat_history_repository import AiChatHistoryRepository
from server.modules.agent.repositories.conversation_repository import ConversationRepository
from server.shared.dispatcher import Dispatcher
from server.shared.logger import get_logger

logger = get_logger(__name__)


async def run_catch_up(session: AsyncSession, dispatcher: Dispatcher, *, limit: int = 500) -> int:
    """Re-encola las conversaciones AI-elegibles cuyo último turno quedó sin responder.

    Devuelve cuántas re-encoló. `limit` acota el barrido (las conversaciones abiertas
    están acotadas en el MVP; si se llega al tope se loguea para no ocultar un backlog).
    """
    conv_repo = ConversationRepository(session)
    history_repo = AiChatHistoryRepository(session)

    eligible = await conv_repo.list_ai_eligible(limit=limit)
    requeued = 0
    for conversation in eligible:
        latest_user_order = await history_repo.latest_user_order(str(conversation.id))
        # Sin atender = hay un inbound del lead por encima del high-water mark. Mirar el
        # orden del último turno del lead (no "la última fila es user") lo hace robusto a
        # una respuesta guardada tarde que lo supera por `message_order` (#240).
        if latest_user_order is None or latest_user_order <= conversation.answered_through_order:
            continue
        await dispatcher.enqueue(
            conversation_id=conversation.id, tenant_id=conversation.organization_id
        )
        requeued += 1

    if len(eligible) >= limit:
        logger.warning("catch_up.limit_reached", limit=limit, requeued=requeued)
    logger.info("catch_up.completed", eligible=len(eligible), requeued=requeued)
    return requeued
