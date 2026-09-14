import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.models import Agent, AgentInstance

_NON_DIGITS = re.compile(r"\D")


def _digits(value: str) -> str:
    """Strip everything but digits (drops '+', spaces, dashes)."""
    return _NON_DIGITS.sub("", value)


class AgentInstanceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_whatsapp_number(self, whatsapp_number: str) -> AgentInstance | None:
        """Resolve the instance for an inbound number, tolerant of formatting.

        Exact match first (hits the unique index). If it misses, fall back to a
        digit-normalized comparison: Meta's ``display_phone_number`` may carry
        spaces or omit the ``+`` that the stored number has, and a byte mismatch
        would otherwise drop every inbound message as ``unknown_instance``.
        """
        result = await self.session.execute(
            select(AgentInstance).where(AgentInstance.whatsapp_number == whatsapp_number)
        )
        instance = result.scalar_one_or_none()
        if instance is not None:
            return instance

        target = _digits(whatsapp_number)
        if not target:
            return None
        candidates = await self.session.execute(
            select(AgentInstance).where(AgentInstance.whatsapp_number.is_not(None))
        )
        for candidate in candidates.scalars():
            if candidate.whatsapp_number and _digits(candidate.whatsapp_number) == target:
                return candidate
        return None

    async def get_for_org(self, organization_id: uuid.UUID) -> AgentInstance | None:
        """Primera instancia activa de la org (vía su agente). Usada para anclar la
        conversación de un alta manual de oportunidad (#97)."""
        result = await self.session.execute(
            select(AgentInstance)
            .join(Agent, AgentInstance.agent_id == Agent.id)
            .where(Agent.organization_id == organization_id, AgentInstance.is_active.is_(True))
            .order_by(AgentInstance.created_at)
            .limit(1)
        )
        return result.scalar_one_or_none()
