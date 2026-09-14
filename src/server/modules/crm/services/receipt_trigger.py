"""Second trigger of the receipt validation: the agent turn that hands the card off.

The webhook enqueues the vision job the moment a photo arrives, but the job only runs
when the card is already in "Por validar pago" — and the card gets there **after** the
agent's turn (a handoff that takes seconds of LLM), while the job's gate is a
millisecond read. In the common case (the lead sends the photo directly) the job ran
first, saw the card still in Gestión Venta and dropped the receipt silently: the lead got
"lo validamos en un rato" and then nothing (server#290).

So, like delivery, validation has two triggers: the webhook (card already waiting — the
lead said "ya pagué" first, or sent a second photo) and this one, after the agent turn
moved the card. Idempotent by wamid on both ends: `already_processed` here, the lock and
the `payment_receipt` unique on the worker.

Known limit: only the media of the turn that handed off is picked up (rows above the
`answered_through_order` the turn started from). A photo answered in an earlier turn
without a handoff is not re-enqueued — the operator validates it from the panel.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.repositories.ai_chat_history_repository import AiChatHistoryRepository
from server.modules.crm.services.receipt_context import ReceiptContext
from server.shared.logger import get_logger

logger = get_logger(__name__)


class VisionQueue(Protocol):
    """The slice of the dispatcher this trigger needs."""

    async def enqueue_vision(
        self, conversation_id: uuid.UUID, tenant_id: uuid.UUID, wamid: str
    ) -> None: ...


async def enqueue_receipts_awaiting_validation(
    session: AsyncSession,
    queue: VisionQueue,
    conversation_id: uuid.UUID,
    org_id: uuid.UUID,
    *,
    after_order: int,
) -> list[str]:
    """Enqueues the validation of the photos this turn answered, if the card now waits
    for it. Returns the wamids enqueued (empty when there was nothing to do)."""
    ctx = ReceiptContext(session)
    if await ctx.card_awaiting_payment(conversation_id, org_id) is None:
        return []
    wamids = await AiChatHistoryRepository(session).media_wamids_after(
        str(conversation_id), after_order
    )
    enqueued: list[str] = []
    for wamid in wamids:
        if await ctx.already_processed(wamid, org_id):
            continue
        await queue.enqueue_vision(conversation_id=conversation_id, tenant_id=org_id, wamid=wamid)
        enqueued.append(wamid)
    if enqueued:
        logger.info(
            "receipt.enqueued_after_handoff",
            conversation_id=str(conversation_id),
            wamids=enqueued,
        )
    return enqueued
