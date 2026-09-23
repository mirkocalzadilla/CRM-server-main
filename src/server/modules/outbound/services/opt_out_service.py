"""Registers an opt-out request and confirms it, bypassing the agent for that turn."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.models import AiChatHistory
from server.modules.agent.domain.ports import MessageSender
from server.modules.agent.repositories.ai_chat_history_repository import AiChatHistoryRepository
from server.modules.outbound.domain.opt_out import OPT_OUT_REPLY
from server.modules.outbound.repositories.opt_out_repository import OptOutRepository
from server.shared.logger import get_logger

logger = get_logger(__name__)


class OptOutService:
    def __init__(self, session: AsyncSession, sender: MessageSender) -> None:
        self._repo = OptOutRepository(session)
        self._history = AiChatHistoryRepository(session)
        self._sender = sender

    async def is_opted_out(self, organization_id: uuid.UUID, wa_id: str) -> bool:
        return await self._repo.is_opted_out(organization_id, wa_id)

    async def register(
        self,
        *,
        organization_id: uuid.UUID,
        agent_id: uuid.UUID,
        conversation_id: uuid.UUID,
        wa_id: str,
        source: str = "keyword",
    ) -> None:
        created = await self._repo.add(organization_id, wa_id, source)
        logger.info("outbound.opt_out", wa_id=wa_id, created=created, source=source)
        # The lead just wrote, so the 24h window is open and a plain text reply is
        # allowed. Best-effort: a send failure must never lose the opt-out itself.
        try:
            await self._sender.send_text(wa_id, OPT_OUT_REPLY)
        except Exception as exc:
            logger.warning("outbound.opt_out_reply_failed", wa_id=wa_id, error=str(exc))
            return
        await self._history.add(
            AiChatHistory(
                agent_id=agent_id,
                organization_id=organization_id,
                thread_id=str(conversation_id),
                session_id=wa_id,
                message={
                    "role": "assistant",
                    "content": OPT_OUT_REPLY,
                    "channel": "whatsapp",
                    "kind": "opt_out",
                },
            )
        )
