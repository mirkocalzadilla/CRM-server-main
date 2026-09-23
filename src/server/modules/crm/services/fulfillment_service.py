"""Entrega automática de lo que el lead compró, al quedar validado su pago.

Orquesta lo que `crm/domain/delivery.py` decide y `DeliveryPlanner` resuelve: envía
(entrada QR para un servicio presencial o híbrido, links para uno virtual), espeja lo
enviado en el hilo del CRM, mueve la card a "Entregado" y cierra la oportunidad.

Reglas que gobiernan el orden de las cosas:

- **Enviar primero, mover después** (#90): el stage solo avanza si el lead recibió algo.
- **Nada a medias**: si falta un dato para armar la entrega, el lead no recibe un
  mensaje roto — la card queda con un aviso y la retoma un humano. Pero el pago sí quedó
  validado y eso se le dice, una sola vez: un lead que pagó y no recibe nada asume lo
  peor (server#290).
- **Una entrega pagada no se retiene por un dato administrativo**: sin nombre del lead
  se entrega igual y la oportunidad queda sin cerrar, con su aviso (#241).
- **Ventana de 24h**: si Meta rechaza el envío libre, una entrada sale igual por la
  plantilla `entry_qr_ready` (M-Outbound B); si no, queda pendiente hasta que el lead
  vuelva a escribir.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.ports import MessageSender
from server.modules.agent.repositories.conversation_repository import ConversationRepository
from server.modules.agent.services.whatsapp_service import WhatsAppSender
from server.modules.crm.domain import card_flags, stages
from server.modules.crm.domain.models import Card
from server.modules.crm.repositories.board_repository import BoardRepository
from server.modules.crm.repositories.card_delivery_repository import CardDeliveryRepository
from server.modules.crm.repositories.qr_entry_repository import QrEntryRepository
from server.modules.crm.services.board_service import SYSTEM_ACTOR, BoardService
from server.modules.crm.services.delivery_notice import DeliveryNotices
from server.modules.crm.services.delivery_planner import DeliveryPlanner, PlannedDelivery
from server.modules.crm.services.entry_service import EntryService
from server.modules.crm.services.qr_image import public_url
from server.modules.outbound.services.entry_delivery import EntryTemplateDelivery
from server.modules.outbound.services.template_sender import TemplateSenderPort
from server.shared.exceptions import (
    NotFoundException,
    OutsideWindowError,
    WonRequiresNameError,
)
from server.shared.logger import get_logger
from server.shared.pubsub import Publisher

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    """Qué pasó con el intento de entrega."""

    delivered: bool = False
    closed: bool = False
    flags: tuple[str, ...] = field(default_factory=tuple)
    reason: str | None = None  # por qué no se entregó, para el log


class FulfillmentService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        publisher: Publisher,
        sender: MessageSender | None = None,
    ) -> None:
        self._session = session
        self._board = BoardRepository(session)
        self._delivery = CardDeliveryRepository(session)
        self._conv = ConversationRepository(session)
        self._board_svc = BoardService(session=session, publisher=publisher)
        self._sender = sender or WhatsAppSender()
        self._entries = EntryService(session=session, publisher=publisher, sender=self._sender)
        self._planner = DeliveryPlanner(session)
        self._notices = DeliveryNotices(session=session, sender=self._sender)
        self._qr_entries = QrEntryRepository(session)
        template_port: TemplateSenderPort = (
            self._sender if isinstance(self._sender, TemplateSenderPort) else WhatsAppSender()
        )
        self._entry_template = EntryTemplateDelivery(session, template_port)

    async def deliver(self, card_id: uuid.UUID, org_id: uuid.UUID) -> DeliveryOutcome:
        """Entrega lo que corresponda a una card cuyo pago quedó validado.

        Idempotente en la práctica: una card que ya no está en "Pago validado" no se
        vuelve a entregar (y la entrada, si la hubo, se reusa por token).
        """
        card = await self._board.get_card(card_id, org_id)
        if card is None:
            return DeliveryOutcome(reason="card no encontrada")

        validated = await self._board.get_stage(
            org_id, stages.PIPELINE_HUMAN, stages.PAYMENT_VALIDATED
        )
        delivered_stage = await self._board.get_stage(
            org_id, stages.PIPELINE_HUMAN, stages.DELIVERED
        )
        if validated is None or delivered_stage is None:
            return DeliveryOutcome(reason="pipeline humano sin los stages de entrega")
        if card.stage_id != validated.id:
            return DeliveryOutcome(reason="la card no está en 'Pago validado'")

        planned = await self._planner.plan_for(card, org_id)
        if planned.blocked_by is not None:
            # El pago está validado aunque no haya qué mandar todavía: se le confirma al
            # lead (una vez) y un humano completa la entrega.
            await self._notices.confirm_payment_pending(card, org_id)
            return await self._block(card, planned.blocked_by)

        try:
            entry_url = await self._send(card, org_id, planned)
        except OutsideWindowError:
            # La conversación se enfrió: Meta no acepta un envío libre. Una entrada sale
            # igual por plantilla (etapa B); si no, se reintenta cuando el lead escriba.
            logger.info("crm.delivery_outside_window", card_id=str(card.id))
            entry_url = await self._send_entry_template(card, org_id, planned)
            if entry_url is None:
                return await self._block(card, card_flags.DELIVERY_PENDING)
        else:  # the template path mirrors itself
            await self._notices.record_delivery(card, org_id, planned.plan, entry_url)
        # Recién con el lead servido avanza el pipeline.
        await self._board_svc.move_card(card.id, delivered_stage.id, SYSTEM_ACTOR, org_id)
        flags = await self._clear_flags(card, keep=planned.plan.warnings)
        closed, close_flags = await self._close(card, org_id)
        logger.info(
            "crm.delivered",
            card_id=str(card.id),
            entry=planned.plan.needs_entry,
            closed=closed,
            flags=list(flags + close_flags),
        )
        return DeliveryOutcome(delivered=True, closed=closed, flags=flags + close_flags)

    async def _send(self, card: Card, org_id: uuid.UUID, planned: PlannedDelivery) -> str | None:
        """Manda la entrega al lead. Devuelve la URL del QR si hubo entrada."""
        if planned.plan.needs_entry:
            entry = await self._entries.issue_and_send(
                card,
                org_id,
                caption=planned.plan.entry_caption,
                existing=planned.existing_entry,
                event=planned.event,
            )
            return public_url(entry.qr_ref)
        conversation = await self._conv.get_by_id(card.conversation_id, org_id)
        if conversation is None:  # pragma: no cover - FK NOT NULL
            raise NotFoundException("conversación no encontrada para entregar")
        await self._sender.send_text(conversation.external_id, planned.plan.text)
        return None

    async def _send_entry_template(
        self, card: Card, org_id: uuid.UUID, planned: PlannedDelivery
    ) -> str | None:
        """Outside the window an issued entry still goes out as a template."""
        if not planned.plan.needs_entry or planned.event is None:
            return None
        entry = await self._qr_entries.get_by_card(card.id)
        conversation = await self._conv.get_by_id(card.conversation_id, org_id)
        if entry is None or conversation is None:
            return None
        sent = await self._entry_template.send(
            card=card,
            org_id=org_id,
            conversation=conversation,
            entry=entry,
            event_name=planned.event.nombre,
            starts_at=planned.event.starts_at,
        )
        return public_url(entry.qr_ref) if sent else None

    async def _close(self, card: Card, org_id: uuid.UUID) -> tuple[bool, tuple[str, ...]]:
        """Cierra la oportunidad en `won`. Sin nombre del lead no se cierra, pero la
        entrega ya ocurrió: queda el aviso y la card en "Entregado" (#241)."""
        won = await self._board.get_stage_by_status(org_id, stages.PIPELINE_HUMAN, "won")
        if won is None:
            return False, ()
        try:
            await self._board_svc.move_card(card.id, won.id, SYSTEM_ACTOR, org_id)
        except WonRequiresNameError:
            logger.info("crm.close_pending_name", card_id=str(card.id))
            await self._delivery.set_flags(card, card_flags.add(card.flags, card_flags.NEEDS_NAME))
            await self._session.commit()
            return False, (card_flags.NEEDS_NAME,)
        return True, ()

    async def _block(self, card: Card, flag: str) -> DeliveryOutcome:
        """Deja el aviso en la card sin mover nada: lo retoma un humano."""
        flags = card_flags.replace_delivery_flags(card.flags, flag)
        await self._delivery.set_flags(card, flags)
        await self._session.commit()
        logger.info("crm.delivery_blocked", card_id=str(card.id), flag=flag)
        return DeliveryOutcome(flags=(flag,), reason=flag)

    async def _clear_flags(self, card: Card, *, keep: tuple[str, ...]) -> tuple[str, ...]:
        """Entrega exitosa: se van los avisos del intento anterior, quedan los de esta."""
        flags = card_flags.replace_delivery_flags(card.flags, *keep)
        await self._delivery.set_flags(card, flags)
        await self._session.commit()
        return keep
