import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.models import AiChatHistory


class AiChatHistoryRepository:
    """Persiste turnos del historial LLM (`message_order` lo asigna la secuencia de BD)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, history: AiChatHistory) -> AiChatHistory:
        self.session.add(history)
        await self.session.flush()
        await self.session.refresh(history)
        return history

    async def exists_by_wamid(self, wamid: str, organization_id: uuid.UUID) -> bool:
        """Si ese mensaje de WhatsApp ya se ingirió (idempotencia del webhook).

        Meta reintenta la entrega del webhook, y el wamid identifica el mensaje: sin
        este chequeo un reintento duplica el turno, el agente responde dos veces y un
        comprobante se valida dos veces."""
        wamid_expr = AiChatHistory.message["wamid"].as_string()  # portable PG / SQLite
        result = await self.session.execute(
            select(AiChatHistory.id)
            .where(AiChatHistory.organization_id == organization_id)
            .where(wamid_expr == wamid)
            .limit(1)
        )
        return result.first() is not None

    async def get_by_wamid(self, wamid: str, organization_id: uuid.UUID) -> AiChatHistory | None:
        """El turno de ese mensaje, para leer su media al validar el comprobante."""
        wamid_expr = AiChatHistory.message["wamid"].as_string()
        result = await self.session.execute(
            select(AiChatHistory)
            .where(AiChatHistory.organization_id == organization_id)
            .where(wamid_expr == wamid)
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def media_wamids_after(self, thread_id: str, after_order: int) -> list[str]:
        """Wamids of the lead's media turns (image/document) above `after_order`, oldest
        first. The receipt trigger uses it to find the photos an agent turn just answered."""
        role = AiChatHistory.message["role"].as_string()
        media_type = AiChatHistory.message["media_type"].as_string()
        wamid = AiChatHistory.message["wamid"].as_string()
        result = await self.session.execute(
            select(wamid)
            .where(AiChatHistory.thread_id == thread_id)
            .where(role == "user")
            .where(media_type.in_(("image", "document")))
            .where(AiChatHistory.message_order > after_order)
            .order_by(AiChatHistory.message_order)
        )
        return [str(value) for value in result.scalars().all() if value]

    async def has_kind(self, thread_id: str, kind: str) -> bool:
        """Whether the thread already holds a system-authored turn tagged `kind`.

        The tag lives inside the JSON so a one-off notice (the payment confirmation while
        a delivery waits on a human) is sent once and stays idempotent across retries."""
        kind_expr = AiChatHistory.message["kind"].as_string()
        result = await self.session.execute(
            select(AiChatHistory.id)
            .where(AiChatHistory.thread_id == thread_id)
            .where(kind_expr == kind)
            .limit(1)
        )
        return result.first() is not None

    async def get_last_user_message_at(self, thread_id: str) -> datetime | None:
        """Timestamp of the most recent user turn — used for 24h window detection."""
        result = await self.session.execute(
            select(func.max(AiChatHistory.created_at))
            .where(AiChatHistory.thread_id == thread_id)
            .where(AiChatHistory.message["role"].astext == "user")
        )
        return result.scalar_one_or_none()

    async def latest_user_order(self, thread_id: str) -> int | None:
        """Mayor `message_order` entre los turnos del lead (rol `user`) del thread, o None.

        El catch-up de #187 lo compara contra `conversation.answered_through_order` para
        hallar inbounds sin responder. Es robusto donde el viejo "último turno = user"
        fallaba: una respuesta guardada tarde supera por `message_order` a un inbound que
        llegó antes, pero acá sólo miramos el orden de los turnos del lead (#240)."""
        role = AiChatHistory.message["role"].as_string()  # portable PG (->>) / SQLite
        result = await self.session.execute(
            select(func.max(AiChatHistory.message_order))
            .where(AiChatHistory.thread_id == thread_id)
            .where(role == "user")
        )
        return result.scalar_one_or_none()

    async def last_activity_by_thread(
        self, thread_ids: Sequence[str]
    ) -> dict[str, tuple[datetime | None, datetime | None]]:
        """Por cada `thread_id`, el timestamp del último inbound del lead (rol `user`) y el
        de la última respuesta del agente (rol `assistant`). Batch de una sola query para el
        badge de "sin responder" del board. Devuelve `{thread_id: (last_user, last_assistant)}`."""
        if not thread_ids:
            return {}
        role = AiChatHistory.message[
            "role"
        ].as_string()  # portable PG (->>) / SQLite (json_extract)
        result = await self.session.execute(
            select(
                AiChatHistory.thread_id,
                func.max(case((role == "user", AiChatHistory.created_at))),
                func.max(case((role == "assistant", AiChatHistory.created_at))),
            )
            .where(AiChatHistory.thread_id.in_(thread_ids))
            .group_by(AiChatHistory.thread_id)
        )
        return {
            thread_id: (last_user, last_assistant)
            for thread_id, last_user, last_assistant in result
        }

    async def list_recent(self, thread_id: str, limit: int = 10) -> list[AiChatHistory]:
        """Últimos `limit` turnos del thread, en orden cronológico (asc por `message_order`)."""
        result = await self.session.execute(
            select(AiChatHistory)
            .where(AiChatHistory.thread_id == thread_id)
            .order_by(AiChatHistory.message_order.desc())
            .limit(limit)
        )
        return list(reversed(result.scalars().all()))
