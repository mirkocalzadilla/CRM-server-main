"""Genera y envía la entrada (QR) de acceso de un servicio presencial.

`issue_and_send` es la pieza reusable: crea la entrada si falta (idempotente por card)
y la manda al lead con su caption, **sin** tocar el stage. `generate_entry` es el
camino manual del CRM: valida el stage de origen, emite y recién ahí mueve la card
(enviar primero, mover después — #90). El fulfillment automático reusa `issue_and_send`
y hace su propio move, para que el avance del pipeline viva en un solo lugar.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.ports import MessageSender
from server.modules.agent.repositories.conversation_repository import ConversationRepository
from server.modules.agent.services.whatsapp_service import WhatsAppSender
from server.modules.crm.domain import stages
from server.modules.crm.domain.delivery import MODALITY_HYBRID, MODALITY_PRESENCIAL
from server.modules.crm.domain.event_models import Event
from server.modules.crm.domain.models import Card, QrEntry
from server.modules.crm.repositories.board_repository import BoardRepository
from server.modules.crm.repositories.card_delivery_repository import CardDeliveryRepository
from server.modules.crm.repositories.qr_entry_repository import QrEntryRepository
from server.modules.crm.services.board_service import BoardService
from server.modules.crm.services.delivery_planner import DeliveryPlanner
from server.modules.crm.services.event_service import blocked_message, snapshot_of
from server.modules.crm.services.qr_image import public_url, save_qr
from server.shared.exceptions import (
    ExternalServiceError,
    NotFoundException,
    OutsideWindowError,
    ValidationException,
)
from server.shared.logger import get_logger
from server.shared.pubsub import Publisher

logger = get_logger(__name__)

_STAGE_ORIGIN = stages.PAYMENT_VALIDATED
_STAGE_TARGET = stages.DELIVERED
_PIPELINE_KIND = stages.PIPELINE_HUMAN


class EntryService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        publisher: Publisher,
        sender: MessageSender | None = None,
    ) -> None:
        self._session = session
        self._board_repo = BoardRepository(session)
        self._qr_repo = QrEntryRepository(session)
        self._conv_repo = ConversationRepository(session)
        self._delivery = CardDeliveryRepository(session)
        self._planner = DeliveryPlanner(session)
        self._board_svc = BoardService(session=session, publisher=publisher)
        self._sender = sender or WhatsAppSender()

    async def generate_entry(
        self,
        card_id: uuid.UUID,
        user_id: uuid.UUID,
        org_id: uuid.UUID,
    ) -> QrEntry:
        card = await self._board_repo.get_card(card_id, org_id)
        if card is None:
            raise NotFoundException("card no encontrada")

        origin_stage = await self._board_repo.get_stage(org_id, _PIPELINE_KIND, _STAGE_ORIGIN)
        target_stage = await self._board_repo.get_stage(org_id, _PIPELINE_KIND, _STAGE_TARGET)
        existing = await self._qr_repo.get_by_card(card_id)

        # Ya generada, enviada y movida → idempotente.
        if existing is not None and target_stage is not None and card.stage_id == target_stage.id:
            return existing

        if origin_stage is None or card.stage_id != origin_stage.id:
            raise ValidationException(
                f"la card debe estar en '{_STAGE_ORIGIN}' para generar la entrada"
            )
        await self._require_presencial(card, org_id)
        if target_stage is None:
            # Sin stage destino el flujo no puede completarse, y enviar la entrada acá
            # dejaría al lead con su QR y a la fila sin commitear (nadie commitea si no
            # se mueve la card). Mejor fallar antes de mandar nada.
            raise ValidationException(
                f"la organización no tiene el stage '{_STAGE_TARGET}' configurado"
            )

        # El mismo plan que la entrega automática (server#290): el botón manda el mismo
        # mensaje — confirmación, fecha, lugar, y los links del híbrido — y no puede
        # saltear el gate del evento: una entrada sin fecha ni lugar no valida nada en la
        # puerta, la haya pedido el sistema o una persona. Una entrada ya emitida se
        # reenvía tal cual, con su evento original.
        planned = await self._planner.plan_for(card, org_id)
        if planned.blocked_by is not None:
            raise ValidationException(blocked_message(planned.blocked_by))

        entry = await self.issue_and_send(
            card,
            org_id,
            caption=planned.plan.entry_caption,
            existing=planned.existing_entry,
            event=planned.event,
        )
        await self._board_svc.move_card(card_id, target_stage.id, user_id, org_id)
        return entry

    async def _require_presencial(self, card: Card, org_id: uuid.UUID) -> None:
        """La entrada solo existe para un servicio presencial (#263).

        Un curso virtual recibe links, no un QR; y un servicio sin modalidad cargada no
        se sabe qué entrega, así que tampoco genera entrada (default seguro). Antes de
        esto se le mandaba un QR inútil a cualquiera que llegara a "Pago validado".
        """
        services = await self._delivery.services_for_card(card.id, org_id)
        if not services:
            raise ValidationException(
                "la oportunidad no tiene un servicio aceptado: cargalo antes de generar la entrada"
            )
        if len(services) > 1:
            raise ValidationException(
                "la oportunidad tiene más de un servicio: no se puede saber qué entrada generar"
            )
        if services[0].modality not in (MODALITY_PRESENCIAL, MODALITY_HYBRID):
            raise ValidationException(
                "este servicio no genera entrada: solo los presenciales e híbridos tienen "
                "entrada de acceso"
            )

    async def issue_and_send(
        self,
        card: Card,
        org_id: uuid.UUID,
        *,
        caption: str,
        existing: QrEntry | None = None,
        event: Event | None = None,
    ) -> QrEntry:
        """Crea la entrada si falta y la manda al lead. No toca el stage.

        Idempotente por card: una entrada ya emitida se reenvía con el mismo token, así
        que un reintento no invalida el QR que el lead ya tiene. Propaga el fallo de
        envío para que el caller no avance el stage (#90).

        `event` liga la entrada a una fecha y un lugar concretos, y guarda una copia de
        esos datos: en la puerta tienen que poder leerse aunque después alguien edite o
        borre el evento.
        """
        entry = existing if existing is not None else await self._qr_repo.get_by_card(card.id)
        if entry is None:
            token = str(uuid.uuid4())
            qr_ref = save_qr(str(org_id), token)
            entry = await self._qr_repo.add(
                QrEntry(
                    card_id=card.id,
                    token=token,
                    qr_ref=qr_ref,
                    event_id=event.id if event is not None else None,
                    event_snapshot=snapshot_of(event) if event is not None else {},
                )
            )
        await self._send_qr_whatsapp(card.conversation_id, org_id, entry.qr_ref, caption)
        return entry

    async def _send_qr_whatsapp(
        self, conversation_id: uuid.UUID, org_id: uuid.UUID, qr_ref: str, caption: str
    ) -> None:
        """Envía la entrada/QR al lead. Propaga el fallo: el caller NO debe avanzar el
        stage si el lead no recibió la entrada (#90). `OutsideWindowError` se propaga
        **sin envolver**: la ventana de 24h cerrada no es un fallo de Meta, es un estado
        que el fulfillment sabe manejar (deja la entrega pendiente y reintenta)."""
        conv = await self._conv_repo.get_by_id(conversation_id, org_id)
        if conv is None:
            raise NotFoundException("conversación no encontrada para enviar la entrada")
        try:
            await self._sender.send_image(
                to=conv.external_id, link=public_url(qr_ref), caption=caption
            )
        except OutsideWindowError:
            logger.warning("whatsapp.qr_outside_window", conversation_id=str(conversation_id))
            raise
        except Exception as exc:
            logger.warning("whatsapp.qr_send_failed", conversation_id=str(conversation_id))
            raise ExternalServiceError(f"no se pudo enviar la entrada por WhatsApp: {exc}") from exc
        logger.info("whatsapp.qr_sent", conversation_id=str(conversation_id))
