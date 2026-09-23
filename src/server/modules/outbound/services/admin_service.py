"""What the CRM needs from M-Outbound: history, settings, opt-outs and manual sends."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.models import Conversation
from server.modules.agent.services.whatsapp_service import WhatsAppSender
from server.modules.crm.domain.event_models import Event
from server.modules.crm.domain.models import Card, QrEntry
from server.modules.outbound.api.schemas import ManualSendResult, OutboundSettingsUpdate
from server.modules.outbound.domain.models import MarketingOptOut, OutboundMessage
from server.modules.outbound.domain.schedule import as_utc
from server.modules.outbound.domain.settings_models import OutboundSettings
from server.modules.outbound.repositories.opt_out_repository import OptOutRepository
from server.modules.outbound.repositories.outbound_repository import OutboundMessageRepository
from server.modules.outbound.repositories.reactivation_repository import (
    Candidate,
    ReactivationRepository,
)
from server.modules.outbound.repositories.reminder_repository import ReminderTarget
from server.modules.outbound.services.event_reminder_service import EventReminderService
from server.modules.outbound.services.reactivation_service import ReactivationService
from server.modules.outbound.services.template_sender import TemplateSenderPort
from server.shared.exceptions import NotFoundException, ValidationException


class OutboundAdminService:
    def __init__(self, session: AsyncSession, sender: TemplateSenderPort | None = None) -> None:
        self._session = session
        self._sender: TemplateSenderPort = sender if sender is not None else WhatsAppSender()
        self._messages = OutboundMessageRepository(session)
        self._opt_outs = OptOutRepository(session)
        self._reactivation = ReactivationRepository(session)

    # ---- history ---------------------------------------------------------------

    async def list_messages(
        self,
        org_id: uuid.UUID,
        *,
        purpose: str | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> list[OutboundMessage]:
        return await self._messages.list_for_org(
            org_id, purpose=purpose, status=status, limit=limit, offset=offset
        )

    async def list_for_card(self, card_id: uuid.UUID, org_id: uuid.UUID) -> list[OutboundMessage]:
        return await self._messages.list_for_card(card_id, org_id)

    # ---- settings --------------------------------------------------------------

    async def read_settings(self, org_id: uuid.UUID) -> OutboundSettings:
        current = await self._reactivation.get_settings(org_id)
        if current is None:
            current = OutboundSettings(organization_id=org_id)
            self._session.add(current)
            await self._session.commit()
            await self._session.refresh(current)
        return current

    async def update_settings(
        self, org_id: uuid.UUID, payload: OutboundSettingsUpdate
    ) -> OutboundSettings:
        current = await self.read_settings(org_id)
        data = payload.model_dump(exclude_unset=True)
        if "reactivation_rules" in data and data["reactivation_rules"] is not None:
            data["reactivation_rules"] = [
                {"stages": rule["stages"], "days": rule["days"]}
                for rule in data["reactivation_rules"]
            ]
        if "novelty_text" in data and data["novelty_text"] is not None:
            data["novelty_text"] = data["novelty_text"].strip()
        for key, value in data.items():
            if value is not None:
                setattr(current, key, value)
        if current.reactivation_enabled and not current.novelty_text:
            raise ValidationException(
                "Para activar la reactivación hace falta la novedad del mes (texto de la plantilla)."
            )
        await self._session.commit()
        await self._session.refresh(current)
        return current

    # ---- opt-outs --------------------------------------------------------------

    async def list_opt_outs(self, org_id: uuid.UUID) -> list[MarketingOptOut]:
        return await self._opt_outs.list_for_org(org_id)

    async def add_opt_out(self, org_id: uuid.UUID, wa_id: str) -> None:
        await self._opt_outs.add(org_id, wa_id, "manual")
        await self._session.commit()

    # ---- manual sends ----------------------------------------------------------

    async def remind_card(self, card_id: uuid.UUID, org_id: uuid.UUID) -> ManualSendResult:
        card, conversation = await self._card_with_conversation(card_id, org_id)
        entry = (
            await self._session.execute(select(QrEntry).where(QrEntry.card_id == card.id))
        ).scalar_one_or_none()
        if entry is None or entry.event_id is None or entry.revoked_at is not None:
            return ManualSendResult(sent=False, reason="sin_entrada_vigente")
        event = await self._session.get(Event, entry.event_id)
        if event is None or as_utc(event.starts_at) <= datetime.now(UTC):
            return ManualSendResult(sent=False, reason="evento_pasado")
        target = ReminderTarget(entry_id=entry.id, card_id=card.id, conversation=conversation)
        sent = await EventReminderService(self._session, self._sender).send_reminder(
            event, target, f"manual:{datetime.now(UTC).timestamp():.0f}"
        )
        await self._session.commit()
        return ManualSendResult(sent=sent, reason=None if sent else "meta_rechazo_o_baja")

    async def reactivate_card(self, card_id: uuid.UUID, org_id: uuid.UUID) -> ManualSendResult:
        card, conversation = await self._card_with_conversation(card_id, org_id)
        cfg = await self.read_settings(org_id)
        if not cfg.novelty_text.strip():
            return ManualSendResult(sent=False, reason="sin_novedad")
        sent = await ReactivationService(self._session, self._sender).send_to(
            Candidate(conversation=conversation, card_id=card.id),
            cfg,
            datetime.now(UTC),
            manual=True,
        )
        await self._session.commit()
        return ManualSendResult(sent=sent, reason=None if sent else "meta_rechazo_o_baja")

    async def _card_with_conversation(
        self, card_id: uuid.UUID, org_id: uuid.UUID
    ) -> tuple[Card, Conversation]:
        card = (
            await self._session.execute(
                select(Card).where(Card.id == card_id, Card.organization_id == org_id)
            )
        ).scalar_one_or_none()
        if card is None:
            raise NotFoundException("card no encontrada")
        conversation = await self._session.get(Conversation, card.conversation_id)
        if conversation is None:
            raise NotFoundException("conversación no encontrada")
        return card, conversation
