"""What the lead is told around a delivery, and its mirror in the CRM thread.

Two messages live here: the delivery itself (entry or links), mirrored so the operator
sees exactly what the lead received, and the confirmation sent when the payment is
validated but nothing can ship yet (server#290). Both go to `ai_chat_histories` as
assistant turns — the system sent them, not an operator — with the `role/content/media_*`
shape the thread mirror reads; the `kind` tag is only for this module's idempotency.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.models import AiChatHistory, Conversation
from server.modules.agent.domain.ports import MessageSender
from server.modules.agent.repositories.ai_chat_history_repository import AiChatHistoryRepository
from server.modules.agent.repositories.conversation_repository import ConversationRepository
from server.modules.crm.domain.delivery import PAYMENT_CONFIRMED_PENDING, DeliveryPlan
from server.modules.crm.domain.models import Card
from server.shared.logger import get_logger

logger = get_logger(__name__)

PAYMENT_CONFIRMED_KIND = "payment_confirmed_notice"


class DeliveryNotices:
    def __init__(self, *, session: AsyncSession, sender: MessageSender) -> None:
        self._session = session
        self._sender = sender
        self._conv = ConversationRepository(session)
        self._history = AiChatHistoryRepository(session)

    async def record_delivery(
        self, card: Card, org_id: uuid.UUID, plan: DeliveryPlan, entry_url: str | None
    ) -> None:
        """Mirrors in the thread what the lead received."""
        conversation = await self._conv.get_by_id(card.conversation_id, org_id)
        if conversation is None:  # pragma: no cover - FK NOT NULL
            return
        content = plan.entry_caption if plan.needs_entry else plan.text
        await self._record(conversation, org_id, content, media_url=entry_url)

    async def confirm_payment_pending(self, card: Card, org_id: uuid.UUID) -> bool:
        """Tells the lead the payment is validated while a human completes the delivery.

        Once per conversation: the tagged row in the thread is what keeps a retry (the
        operator loads the event, the card is re-delivered) from saying it again. A
        failed send is logged and not retried — the card's notice is what brings the
        human in, and that one is already set.
        """
        thread_id = str(card.conversation_id)
        if await self._history.has_kind(thread_id, PAYMENT_CONFIRMED_KIND):
            return False
        conversation = await self._conv.get_by_id(card.conversation_id, org_id)
        if conversation is None:  # pragma: no cover - FK NOT NULL
            return False
        try:
            await self._sender.send_text(conversation.external_id, PAYMENT_CONFIRMED_PENDING)
        except Exception as exc:
            logger.warning(
                "crm.payment_confirmed_notice_failed", card_id=str(card.id), error=str(exc)
            )
            return False
        await self._record(
            conversation, org_id, PAYMENT_CONFIRMED_PENDING, kind=PAYMENT_CONFIRMED_KIND
        )
        logger.info("crm.payment_confirmed_notice", card_id=str(card.id))
        return True

    async def _record(
        self,
        conversation: Conversation,
        org_id: uuid.UUID,
        content: str,
        *,
        media_url: str | None = None,
        kind: str | None = None,
    ) -> None:
        message: dict[str, object] = {
            "role": "assistant",
            "content": content,
            "channel": "whatsapp",
        }
        if media_url is not None:
            message["media_type"] = "image"
            message["media_url"] = media_url
        if kind is not None:
            message["kind"] = kind
        await self._history.add(
            AiChatHistory(
                agent_id=conversation.instance.agent_id,
                organization_id=org_id,
                thread_id=str(conversation.id),
                session_id=conversation.external_id,
                message=message,
            )
        )
        await self._session.commit()
