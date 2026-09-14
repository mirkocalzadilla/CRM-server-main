"""Conciliación humana de los pagos que el sistema aprobó solo.

Mientras no haya pasarela bancaria, un comprobante es una imagen — y una imagen se puede
editar. Así que el sistema aprueba y entrega (para que el lead no espere), pero cada pago
auto-validado queda en una cola hasta que alguien lo coteja contra el banco.

Es un estado del **comprobante**, no del funnel: la card sigue su curso (entregada,
cerrada) y la conciliación va en paralelo. Confirmar solo sella el registro. Rechazar es
la operación seria: **revoca la entrada** —la única defensa cuando el lead ya tiene el QR
en el teléfono es que el escáner la rechace en la puerta— y deja la oportunidad en `lost`
con el motivo escrito.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.crm.domain import card_flags
from server.modules.crm.domain.payment_models import PaymentReceipt
from server.modules.crm.repositories.board_repository import BoardRepository
from server.modules.crm.repositories.card_delivery_repository import CardDeliveryRepository
from server.modules.crm.repositories.payment_receipt_repository import PaymentReceiptRepository
from server.modules.crm.repositories.qr_entry_repository import QrEntryRepository
from server.modules.crm.services.board_service import BoardService
from server.shared.exceptions import NotFoundException, ValidationException
from server.shared.logger import get_logger
from server.shared.pubsub import Publisher

logger = get_logger(__name__)

REJECTED_MOVE_REASON = "Pago no confirmado con el banco"


class PaymentReconciliationService:
    def __init__(self, *, session: AsyncSession, publisher: Publisher) -> None:
        self._session = session
        self._receipts = PaymentReceiptRepository(session)
        self._entries = QrEntryRepository(session)
        self._board = BoardRepository(session)
        self._delivery = CardDeliveryRepository(session)
        self._board_svc = BoardService(session=session, publisher=publisher)

    async def pending(self, org_id: uuid.UUID) -> list[PaymentReceipt]:
        """Pagos aprobados por el sistema que todavía nadie cotejó con el banco."""
        return await self._receipts.list_pending_confirmation(org_id)

    async def confirmed_on(self, org_id: uuid.UUID, day: date) -> list[PaymentReceipt]:
        """Auto-validados de un día, para cotejar contra el extracto bancario."""
        return await self._receipts.list_auto_approved_on(org_id, day)

    async def confirm(
        self, receipt_id: uuid.UUID, org_id: uuid.UUID, user_id: uuid.UUID
    ) -> PaymentReceipt:
        """Sella el pago como cotejado. Idempotente: confirmar dos veces no hace nada."""
        receipt = await self._require_pending(receipt_id, org_id)
        if receipt.human_confirmed_at is not None:
            return receipt
        receipt.human_confirmed_at = datetime.now(UTC)
        receipt.human_by = user_id
        await self._clear_unconfirmed_flag(receipt, org_id)
        await self._session.commit()
        logger.info("payment.confirmed", receipt_id=str(receipt_id), user_id=str(user_id))
        return receipt

    async def reject(
        self, receipt_id: uuid.UUID, org_id: uuid.UUID, user_id: uuid.UUID, note: str
    ) -> PaymentReceipt:
        """El pago no entró: revoca la entrada y cierra la oportunidad como perdida.

        La nota es obligatoria porque esto revierte algo que ya se entregó: sin el motivo
        escrito, nadie puede reconstruir después por qué se le anuló la entrada a alguien.
        """
        cleaned = " ".join(note.split())
        if len(cleaned) < 3:
            raise ValidationException(
                "La nota es obligatoria: explicá por qué el pago no se pudo confirmar."
            )
        receipt = await self._require_pending(receipt_id, org_id)
        if receipt.human_rejected_at is not None:
            return receipt

        receipt.human_rejected_at = datetime.now(UTC)
        receipt.human_by = user_id
        receipt.human_note = cleaned

        # Revocar la entrada, si se había generado. El lead ya la tiene: lo único que
        # se puede hacer es que el escáner la rechace.
        entry = await self._entries.get_by_card(receipt.card_id)
        if entry is not None and entry.revoked_at is None:
            entry.revoked_at = datetime.now(UTC)
            entry.revoked_reason = cleaned
            logger.info("entry.revoked", card_id=str(receipt.card_id))

        await self._mark_lost(receipt, org_id, user_id, cleaned)
        logger.info("payment.rejected", receipt_id=str(receipt_id), user_id=str(user_id))
        return receipt

    async def _require_pending(self, receipt_id: uuid.UUID, org_id: uuid.UUID) -> PaymentReceipt:
        receipt = await self._receipts.get(receipt_id, org_id)
        if receipt is None:
            raise NotFoundException("comprobante no encontrado")
        return receipt

    async def _clear_unconfirmed_flag(self, receipt: PaymentReceipt, org_id: uuid.UUID) -> None:
        card = await self._board.get_card(receipt.card_id, org_id)
        if card is not None:
            await self._delivery.set_flags(
                card, card_flags.remove(card.flags, card_flags.PAYMENT_UNCONFIRMED)
            )

    async def _mark_lost(
        self, receipt: PaymentReceipt, org_id: uuid.UUID, user_id: uuid.UUID, note: str
    ) -> None:
        """Mueve la card al stage perdido del pipeline humano, con el motivo.

        Se resuelve por `status_code` y no por nombre: el stage terminal es el contrato,
        el nombre es dato de cada organización. Si el pipeline no tiene uno, no se inventa
        nada — el rechazo queda registrado igual y la card la mueve un humano.

        **Todos los caminos commitean.** `move_card` commitea sólo cuando el stage cambia,
        así que una card que ya estaba en el stage perdido salía de acá sin persistir: la
        revocación de la entrada se perdía en el rollback y el escáner la admitía en la
        puerta, con el operador viendo un 200. Lo que hace irreversible ese caso es que la
        revocación tiene un único escritor y ninguna reconciliación posterior: si no se
        graba acá, no se graba nunca.
        """
        card = await self._board.get_card(receipt.card_id, org_id)
        if card is None:
            await self._session.commit()
            return
        await self._delivery.set_flags(
            card, card_flags.remove(card.flags, card_flags.PAYMENT_UNCONFIRMED)
        )
        lost = await self._board.get_stage_by_status(org_id, "human", "lost")
        if lost is None:
            logger.warning("payment.no_lost_stage", card_id=str(card.id))
            await self._session.commit()
            return
        if card.stage_id == lost.id:
            await self._session.commit()
            return
        await self._board_svc.move_card(
            card.id, lost.id, user_id, org_id, reason=f"{REJECTED_MOVE_REASON}: {note}"
        )
