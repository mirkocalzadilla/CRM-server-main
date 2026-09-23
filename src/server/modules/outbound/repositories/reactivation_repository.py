from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import NamedTuple

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.catalog_models import Service
from server.modules.agent.domain.models import Conversation
from server.modules.crm.domain.models import Card, CardService, Stage
from server.modules.outbound.domain.settings_models import OutboundSettings


class Candidate(NamedTuple):
    conversation: Conversation
    card_id: uuid.UUID


class ReactivationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enabled_settings(self) -> list[OutboundSettings]:
        result = await self._session.execute(
            select(OutboundSettings).where(OutboundSettings.reactivation_enabled.is_(True))
        )
        return list(result.scalars().all())

    async def get_settings(self, organization_id: uuid.UUID) -> OutboundSettings | None:
        result = await self._session.execute(
            select(OutboundSettings).where(OutboundSettings.organization_id == organization_id)
        )
        return result.scalar_one_or_none()

    async def candidates(
        self, organization_id: uuid.UUID, stages: Sequence[str]
    ) -> list[Candidate]:
        """Open conversations in `stages` still handled by the agent, with an open card.

        Human takeovers (`is_ai_active = False`) and closed conversations are out: a
        person is already talking to that lead, or the opportunity ended.
        """
        open_card = (
            select(Card.id)
            .join(Stage, Stage.id == Card.stage_id)
            .where(Card.conversation_id == Conversation.id, Stage.status_code == "open")
            .order_by(Card.created_at.desc())
            .limit(1)
            .correlate(Conversation)
            .scalar_subquery()
        )
        result = await self._session.execute(
            select(Conversation, open_card)
            .where(
                Conversation.organization_id == organization_id,
                Conversation.funnel_stage.in_(list(stages)),
                Conversation.closed_at.is_(None),
                Conversation.is_ai_active.is_(True),
                exists(
                    select(Card.id)
                    .join(Stage, Stage.id == Card.stage_id)
                    .where(Card.conversation_id == Conversation.id, Stage.status_code == "open")
                ),
            )
            .order_by(Conversation.updated_at)
        )
        return [
            Candidate(conversation=conversation, card_id=card_id)
            for conversation, card_id in result.unique().all()
        ]

    async def first_service_name(self, card_id: uuid.UUID) -> str | None:
        result = await self._session.execute(
            select(Service.nombre)
            .join(CardService, CardService.service_id == Service.id)
            .where(CardService.card_id == card_id)
            .order_by(CardService.created_at)
            .limit(1)
        )
        return result.scalar_one_or_none()
