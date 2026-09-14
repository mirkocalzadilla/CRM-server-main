"""Cuándo se dispara la entrega automática.

Tres disparadores, uno por cada forma en que una card queda lista para entregar:

- **El operador valida el pago** (arrastra la card a "Pago validado"): se entrega en el
  mismo request, sin que tenga que apretar nada más. Arrastrar **es** validar, así que
  el comprobante registra esa aprobación igual que el botón del panel (server#292).
- **El lead vuelve a escribir** y había una entrega pendiente porque la ventana de 24h
  de WhatsApp estaba cerrada: el inbound reabre la ventana, así que se reintenta.
- **Alguien carga o edita el evento** de un servicio: las cards que esperaban esa fecha
  (`no_event`) o un lugar (`capacity_full`) se entregan en el mismo request (server#290).

Vive aparte del router y del worker porque los dos lo necesitan, y aparte del
`FulfillmentService` para que ese siga siendo "entregá esta card" sin saber quién lo
pidió. Best-effort en los dos casos: un fallo de la entrega se loguea y no tumba el
request del operador ni el turno del agente.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sentry_sdk
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.crm.domain import card_flags, stages
from server.modules.crm.repositories.board_repository import BoardRepository
from server.modules.crm.repositories.card_delivery_repository import CardDeliveryRepository
from server.modules.crm.repositories.card_repository import CardRepository
from server.modules.crm.repositories.payment_receipt_repository import PaymentReceiptRepository
from server.modules.crm.services.fulfillment_service import DeliveryOutcome, FulfillmentService
from server.shared.logger import get_logger
from server.shared.pubsub import publisher

logger = get_logger(__name__)


async def deliver_if_payment_validated(
    session: AsyncSession,
    stage_id: uuid.UUID,
    card_id: uuid.UUID,
    org_id: uuid.UUID,
    *,
    approved_by: uuid.UUID | None = None,
) -> DeliveryOutcome | None:
    """Entrega si el move dejó la card en "Pago validado".

    `approved_by` es el operador que arrastró la card: su aprobación queda en el
    comprobante antes de entregar. El panel del comprobante ya la registró por su
    cuenta, así que no la pasa.
    """
    stage = await BoardRepository(session).get_stage_by_id(stage_id, org_id)
    if stage is None or stage.pipeline.kind != stages.PIPELINE_HUMAN:
        return None
    if stage.name != stages.PAYMENT_VALIDATED:
        return None
    if approved_by is not None:
        await _record_manual_approval(session, card_id, org_id, approved_by)
    return await _deliver(session, card_id, org_id, trigger="payment_validated")


async def _record_manual_approval(
    session: AsyncSession, card_id: uuid.UUID, org_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    """Arrastrar la card a "Pago validado" es validar el pago: el comprobante lo registra.

    Sin esto el comprobante de una card validada por arrastre seguía "sin aprobar" y el
    panel ofrecía validarlo de nuevo sobre una entrega ya hecha. Una aprobación previa
    (del sistema o de otro operador) no se pisa: la primera es la que cuenta. Y el aviso
    "revisá este comprobante" deja de aplicar, igual que cuando se valida desde el panel.
    """
    receipt = await PaymentReceiptRepository(session).latest_for_card(card_id, org_id)
    if receipt is None or receipt.approved_at is not None:
        return
    receipt.approved_at = datetime.now(UTC)
    receipt.approved_by = str(user_id)
    card = await BoardRepository(session).get_card(card_id, org_id)
    if card is not None:
        await CardDeliveryRepository(session).set_flags(
            card, card_flags.remove(card.flags, card_flags.RECEIPT_REVIEW)
        )
    await session.commit()
    logger.info("receipt.approved_by_move", card_id=str(card_id), user_id=str(user_id))


async def retry_pending_delivery(
    session: AsyncSession, conversation_id: uuid.UUID, org_id: uuid.UUID
) -> DeliveryOutcome | None:
    """Reintenta una entrega que había quedado pendiente por la ventana de 24h.

    Se llama en cada inbound del lead. El inbound es justamente lo que reabre la
    ventana, así que este es el momento exacto en que el reintento puede funcionar. Sin
    aviso pendiente no hace nada (ni una query de más allá de la card).
    """
    card = await CardRepository(session).get_by_conversation(conversation_id)
    if card is None or not card_flags.has(card.flags, card_flags.DELIVERY_PENDING):
        return None
    logger.info("crm.delivery_retry", card_id=str(card.id))
    return await _deliver(session, card.id, org_id, trigger="lead_inbound")


async def retry_deliveries_blocked_by_event(
    session: AsyncSession, service_id: uuid.UUID, org_id: uuid.UUID
) -> int:
    """Reintenta las entregas que esperaban un evento de este servicio.

    Se llama al crear o editar un evento. Esas cards están en "Pago validado" con el
    aviso `no_event` o `capacity_full`, y hasta ahora el operador tenía que darse cuenta
    y apretar el botón. Las cards sin uno de esos avisos no se tocan.
    """
    stage = await BoardRepository(session).get_stage(
        org_id, stages.PIPELINE_HUMAN, stages.PAYMENT_VALIDATED
    )
    if stage is None:
        return 0
    cards = await CardDeliveryRepository(session).cards_in_stage_with_service(
        stage.id, service_id, org_id
    )
    retried = 0
    for card in cards:
        waiting = card_flags.has(card.flags, card_flags.NO_EVENT) or card_flags.has(
            card.flags, card_flags.CAPACITY_FULL
        )
        if not waiting:
            continue
        logger.info("crm.delivery_retry_event", card_id=str(card.id), service_id=str(service_id))
        await _deliver(session, card.id, org_id, trigger="event_scheduled")
        retried += 1
    return retried


async def _deliver(
    session: AsyncSession, card_id: uuid.UUID, org_id: uuid.UUID, *, trigger: str
) -> DeliveryOutcome | None:
    service = FulfillmentService(session=session, publisher=publisher)
    try:
        return await service.deliver(card_id, org_id)
    except Exception as exc:
        # Un fallo de entrega no puede tumbar el request del operador (que ya movió la
        # card) ni el turno del agente. Queda en Sentry y en el log; la card sigue en
        # "Pago validado", que es el estado desde el que se reintenta.
        sentry_sdk.capture_exception(exc)
        logger.error("crm.delivery_failed", card_id=str(card_id), trigger=trigger, error=str(exc))
        return None
