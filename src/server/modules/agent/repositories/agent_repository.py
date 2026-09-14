"""Acceso a `agent` + `agent_version` para M-Config (lectura org-scoped + versionado)."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.models import Agent, AgentVersion


class AgentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, agent_id: uuid.UUID, organization_id: uuid.UUID) -> Agent | None:
        result = await self.session.execute(
            select(Agent).where(
                Agent.id == agent_id,
                Agent.organization_id == organization_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_for_organization(self, organization_id: uuid.UUID) -> list[Agent]:
        result = await self.session.execute(
            select(Agent).where(Agent.organization_id == organization_id).order_by(Agent.created_at)
        )
        return list(result.scalars().all())

    async def get_version(self, version_id: uuid.UUID) -> AgentVersion | None:
        result = await self.session.execute(
            select(AgentVersion).where(AgentVersion.id == version_id)
        )
        return result.scalar_one_or_none()

    async def next_version_number(self, agent_id: uuid.UUID) -> int:
        result = await self.session.execute(
            select(func.max(AgentVersion.version_number)).where(AgentVersion.agent_id == agent_id)
        )
        current_max = result.scalar_one_or_none()
        return (current_max or 0) + 1

    async def add_version(self, version: AgentVersion) -> AgentVersion:
        self.session.add(version)
        await self.session.flush()
        await self.session.refresh(version)
        return version
