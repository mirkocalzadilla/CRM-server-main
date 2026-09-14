"""Wiring del handler que consume `agent:vision` → validación del comprobante.

Igual que el handler de turnos: arma el servicio con las implementaciones reales y
atrapa todo. Un comprobante que no se pudo validar no puede tumbar el worker ni
bloquear la cola — la card se queda en "Por validar pago", que es exactamente el estado
en el que un humano lo revisa.

Cuando la validación aprueba el pago, la card queda en "Pago validado" y de ahí sale la
entrega, con el mismo disparador que usa el operador cuando valida a mano: un solo
camino para las dos formas de llegar.
"""

from __future__ import annotations

import uuid

import sentry_sdk
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import get_settings
from server.modules.agent.services.llm_factory import build_vision
from server.modules.agent.services.whatsapp_service import WhatsAppSender
from server.modules.crm.services.delivery_trigger import deliver_if_payment_validated
from server.modules.crm.services.receipt_validation_service import ReceiptValidationService
from server.shared.logger import get_logger
from server.shared.pubsub import publisher

logger = get_logger(__name__)


def build_receipt_validation(session: AsyncSession) -> ReceiptValidationService:
    return ReceiptValidationService(
        session=session,
        vision=build_vision(get_settings()),
        sender=WhatsAppSender(),
        publisher=publisher,
    )


async def receipt_vision_handler(
    conversation_id: uuid.UUID, tenant_id: uuid.UUID, wamid: str, session: AsyncSession
) -> None:
    service = build_receipt_validation(session)
    try:
        outcome = await service.validate(conversation_id, tenant_id, wamid)
    except Exception as exc:
        sentry_sdk.capture_exception(exc)
        logger.error(
            "receipt.validation_error",
            conversation_id=str(conversation_id),
            wamid=wamid,
            error=str(exc),
        )
        return

    if not outcome.approved:
        logger.info("receipt.not_approved", wamid=wamid, reason=outcome.reason)
        return

    # Pago aprobado ⇒ la card ya está en "Pago validado": se entrega por el mismo camino
    # que cuando el operador la mueve a mano.
    from server.modules.crm.repositories.card_repository import CardRepository

    card = await CardRepository(session).get_by_conversation(conversation_id)
    if card is None:  # pragma: no cover - la validación ya la resolvió
        return
    await deliver_if_payment_validated(session, card.stage_id, card.id, tenant_id)
