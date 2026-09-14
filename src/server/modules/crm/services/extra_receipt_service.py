"""Detecta un comprobante que llega cuando el pago ya fue procesado.

Si el lead manda una foto con la card ya en "Pago validado", "Entregado" o cerrada, no
es el comprobante que se está esperando: puede ser un pago doble, un comprobante de
otra cosa, o simplemente una foto. El sistema **no mueve nada y no vuelve a entregar** —
deja un aviso y lo mira un humano.

Deliberadamente no interpreta la imagen: eso es trabajo de la validación por visión, y
acá justamente lo que se quiere es no actuar solo.
"""

from __future__ import annotations

import uuid

import sentry_sdk
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.crm.domain import card_flags, stages
from server.modules.crm.repositories.board_repository import BoardRepository
from server.modules.crm.repositories.card_delivery_repository import CardDeliveryRepository
from server.modules.crm.repositories.card_repository import CardRepository
from server.shared.logger import get_logger

logger = get_logger(__name__)

# Stages en los que el pago ya se procesó: un comprobante nuevo acá no es el esperado.
_ALREADY_PROCESSED: frozenset[str] = frozenset(
    {stages.PAYMENT_VALIDATED, stages.DELIVERED, stages.CLOSED}
)


async def flag_extra_receipt(
    session: AsyncSession, conversation_id: uuid.UUID, org_id: uuid.UUID
) -> bool:
    """`True` si el comprobante es **extra** (el pago de esta card ya se procesó).

    El valor de retorno responde "¿es extra?", no "¿lo marqué?": un segundo comprobante
    sigue siendo extra aunque el aviso ya estuviera puesto, y quien llama necesita eso
    para no mandarlo a validar. El aviso en sí se pone una sola vez.
    """
    card = await CardRepository(session).get_by_conversation(conversation_id)
    if card is None:
        return False
    stage = await BoardRepository(session).get_stage_by_id(card.stage_id, org_id)
    if stage is None or stage.pipeline.kind != stages.PIPELINE_HUMAN:
        return False
    if stage.name not in _ALREADY_PROCESSED:
        return False
    if card_flags.has(card.flags, card_flags.EXTRA_RECEIPT):
        return True  # ya avisado: sigue siendo extra, pero no se re-avisa por cada foto
    await CardDeliveryRepository(session).set_flags(
        card, card_flags.add(card.flags, card_flags.EXTRA_RECEIPT)
    )
    await session.commit()
    logger.info("crm.extra_receipt", card_id=str(card.id), stage=stage.name)
    return True


async def flag_extra_receipt_if_processed(
    conversation_id: uuid.UUID, org_id: uuid.UUID, session: AsyncSession
) -> bool:
    """`flag_extra_receipt` a prueba de fallos, para llamar desde el webhook.

    Devuelve `True` si el comprobante es extra (el pago ya estaba procesado), que es lo
    que el webhook usa para no mandarlo a validar.

    El aviso es informativo: si falla, lo que no puede pasar es perder el mensaje del
    lead ni devolverle un error a Meta (que reintentaría el webhook). Ante un fallo
    devuelve `False`, el camino que sí procesa: es mejor validar un comprobante de más
    que perder el que el lead está esperando que se valide.
    """
    try:
        return await flag_extra_receipt(session, conversation_id, org_id)
    except Exception as exc:
        sentry_sdk.capture_exception(exc)
        logger.error(
            "crm.extra_receipt_error", conversation_id=str(conversation_id), error=str(exc)
        )
        return False
