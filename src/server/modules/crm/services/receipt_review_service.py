"""Lo que un operador hace con un comprobante desde el CRM.

Dos acciones, y una regla que las distingue:

- **Validar** un comprobante cuyos checks pasaron: un click, sin explicar nada.
- **Validar con override** uno cuyos checks fallaron: exige una nota. Si una persona
  aprueba un pago que el sistema no aprobaría (un sobrepago legítimo, un pago desde la
  cuenta de un familiar), lo único que después explica esa decisión es lo que escribió.

Las dos hacen lo mismo a continuación: mover la card a "Pago validado" **y entregar**,
en una sola operación. Separarlas en dos endpoints dejaría a la card en un estado
intermedio si el segundo falla, y la generación de la entrada exige que ya esté ahí.

Y las dos **registran la aprobación en el comprobante** (`approved_at`, `approved_by`) y
se niegan sobre una card que ya pasó "Pago validado": antes, el botón sobre una card
entregada la retrocedía y volvía a entregar al lead (server#292).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from server.config import get_settings
from server.modules.crm.api.receipt_schemas import ReceiptCheckOut, ReceiptOut
from server.modules.crm.domain import card_flags, stages
from server.modules.crm.domain.models import Stage
from server.modules.crm.domain.payment_models import PaymentReceipt
from server.modules.crm.repositories.board_repository import BoardRepository
from server.modules.crm.repositories.card_delivery_repository import CardDeliveryRepository
from server.modules.crm.repositories.payment_receipt_repository import PaymentReceiptRepository
from server.modules.crm.services.board_service import BoardService
from server.modules.crm.services.delivery_trigger import deliver_if_payment_validated
from server.shared.exceptions import NotFoundException, ValidationException
from server.shared.logger import get_logger
from server.shared.pubsub import Publisher

logger = get_logger(__name__)


class ReceiptReviewService:
    def __init__(self, *, session: AsyncSession, publisher: Publisher) -> None:
        self._session = session
        self._receipts = PaymentReceiptRepository(session)
        self._board = BoardRepository(session)
        self._delivery = CardDeliveryRepository(session)
        self._board_svc = BoardService(session=session, publisher=publisher)

    async def latest_for_card(self, card_id: uuid.UUID, org_id: uuid.UUID) -> ReceiptOut | None:
        receipt = await self._receipts.latest_for_card(card_id, org_id)
        return _to_out(receipt) if receipt is not None else None

    async def validate_payment(
        self,
        card_id: uuid.UUID,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        note: str | None = None,
    ) -> ReceiptOut | None:
        """Marca el pago validado y entrega, en una sola operación.

        `note` es obligatoria cuando el comprobante no pasó los checks: el router lo
        exige antes de llegar acá, y esto lo vuelve a verificar porque la regla es del
        dominio, no de la forma del request.
        """
        card = await self._board.get_card(card_id, org_id)
        if card is None:
            raise NotFoundException("card no encontrada")
        target = await self._board.get_stage(
            org_id, stages.PIPELINE_HUMAN, stages.PAYMENT_VALIDATED
        )
        if target is None:
            raise ValidationException("la organización no tiene el stage 'Pago validado'")
        current = await self._board.get_stage_by_id(card.stage_id, org_id)
        if _past_validation(current, target):
            raise ValidationException(
                "este pago ya está validado y entregado: la card ya siguió su curso"
            )

        receipt = await self._receipts.latest_for_card(card_id, org_id)
        if receipt is not None and receipt.verdict != "pass" and not note:
            raise ValidationException(
                "este comprobante no pasó los checks: escribí una nota explicando por qué lo validás"
            )

        if receipt is not None:
            receipt.human_note = note or receipt.human_note
            if receipt.approved_at is None:
                # La primera aprobación es la que cuenta: si el sistema ya aprobó y la
                # entrega quedó trabada, este click solo la reintenta.
                receipt.approved_at = datetime.now(UTC)
                receipt.approved_by = str(user_id)
            await self._claim_reference(receipt, org_id)
        # El comprobante ya lo revisó una persona: el aviso deja de aplicar.
        await self._delivery.set_flags(
            card, card_flags.remove(card.flags, card_flags.RECEIPT_REVIEW)
        )
        if card.stage_id != target.id:
            await self._board_svc.move_card(card_id, target.id, user_id, org_id, reason=note)
        else:
            await self._session.commit()
        logger.info(
            "receipt.validated_by_human",
            card_id=str(card_id),
            user_id=str(user_id),
            override=bool(note),
        )
        # Entregar es parte de la misma acción: el operador apretó "validar", no
        # "validar y después acordate de entregar".
        await deliver_if_payment_validated(self._session, target.id, card_id, org_id)
        refreshed = await self._receipts.latest_for_card(card_id, org_id)
        return _to_out(refreshed) if refreshed is not None else None

    async def _claim_reference(self, receipt: PaymentReceipt, org_id: uuid.UUID) -> None:
        """Ocupa el unique anti-reuso con la transacción del comprobante aprobado a mano.

        Un comprobante que falló los checks se guarda con `reference` en NULL a propósito,
        para no bloquear al operador que después quiera aprobarlo. Cuando ese operador lo
        aprueba, hay que ocupar el unique igual: si no, esa transferencia queda reusable
        para siempre y una segunda card puede **auto-aprobarse** con la misma — sin pasar
        por ojos humanos, porque los seis checks le dan verde.

        El sha de la imagen no cubre este caso: es igualdad exacta de bytes, y una captura
        reenviada por WhatsApp se recomprime.
        """
        if receipt.reference is not None:
            return
        raw = (receipt.extracted or {}).get("reference")
        reference = str(raw).strip() if raw else ""
        if not reference:
            return
        taken = await self._receipts.get_by_reference(reference, org_id)
        if taken is not None and taken.id != receipt.id:
            # Esa transacción ya validó otro pago. El operador está aprobando algo que el
            # sistema considera reuso: se respeta su decisión — ya escribió la nota — pero
            # el unique no se toca, y queda registrado que quedó sin ocupar.
            logger.warning(
                "receipt.reference_already_taken",
                receipt_id=str(receipt.id),
                other_card_id=str(taken.card_id),
            )
            return
        receipt.reference = reference


def _past_validation(current: Stage | None, validated: Stage) -> bool:
    """La card ya pasó "Pago validado" en el pipeline humano (Entregado, Cerrado).

    Por posición y no por nombre, como el ratchet de `card_service`. "Perdido" queda
    afuera aunque sea el último stage: validar un pago sobre una oportunidad caída es una
    decisión legítima del operador (la resucita), no un doble click.
    """
    if current is None or current.pipeline_id != validated.pipeline_id:
        return False
    if current.status_code == "lost":
        return False
    return current.position > validated.position


def _to_out(receipt: PaymentReceipt) -> ReceiptOut:
    extracted = {
        key: (str(value) if value is not None else None)
        for key, value in (receipt.extracted or {}).items()
    }
    return ReceiptOut(
        id=receipt.id,
        card_id=receipt.card_id,
        verdict=receipt.verdict,
        checks=[
            ReceiptCheckOut(
                code=str(check.get("code", "")),
                passed=bool(check.get("passed")),
                detail=str(check.get("detail", "")),
            )
            for check in receipt.checks or []
        ],
        extracted=extracted,
        image_url=_image_url(receipt.media_path),
        approved_at=receipt.approved_at,
        approved_by=receipt.approved_by,
        human_confirmed_at=receipt.human_confirmed_at,
        human_rejected_at=receipt.human_rejected_at,
        human_note=receipt.human_note,
        created_at=receipt.created_at,
    )


def _image_url(media_path: str | None) -> str | None:
    if not media_path:
        return None
    return f"{get_settings().media_base_url}/media/{media_path}"
