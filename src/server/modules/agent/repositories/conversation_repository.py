import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.models import Conversation


class ConversationRepository:
    """Acceso a `conversation`, siempre filtrado por `organization_id` (tenant-scoped)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_latest_by_external_id(
        self, external_id: str, organization_id: uuid.UUID
    ) -> Conversation | None:
        """Most recent conversation for the phone, or None. A phone holds many
        conversations over time (one per opportunity, #163); the latest is the
        current one — callers decide whether to reuse it or open a new opportunity."""
        result = await self.session.execute(
            select(Conversation)
            .where(
                Conversation.external_id == external_id,
                Conversation.organization_id == organization_id,
            )
            .order_by(Conversation.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_by_id(
        self, conversation_id: uuid.UUID, organization_id: uuid.UUID
    ) -> Conversation | None:
        result = await self.session.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.organization_id == organization_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_ai_eligible(self, limit: int = 500) -> list[Conversation]:
        """Conversaciones abiertas que sigue manejando la IA (`is_ai_active` y sin
        `closed_at`), de cualquier tenant. Cross-tenant a propósito: lo consume el
        catch-up del worker (#187), un proceso de sistema (no un request), para hallar
        leads que quedaron sin atender tras una caída. Ordenadas por antigüedad."""
        result = await self.session.execute(
            select(Conversation)
            .where(
                Conversation.is_ai_active.is_(True),
                Conversation.closed_at.is_(None),
            )
            .order_by(Conversation.created_at)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def add(self, conversation: Conversation) -> Conversation:
        self.session.add(conversation)
        await self.session.flush()
        await self.session.refresh(conversation)
        return conversation
