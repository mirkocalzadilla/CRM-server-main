"""Reunir lo que hace falta para validar un comprobante, y persistir el resultado.

Separado del orquestador para que ese quede legible: acá vive el trabajo de ir a buscar
datos (la card, el archivo, el precio esperado, los duplicados) y el de guardar, y allá
la decisión de qué hacer con el veredicto.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.repositories.ai_chat_history_repository import AiChatHistoryRepository
from server.modules.crm.domain import stages
from server.modules.crm.domain.models import Card
from server.modules.crm.domain.payment_models import PaymentReceipt
from server.modules.crm.domain.receipt_checks import (
    VERDICT_FAIL,
    CheckResult,
    ExpectedPayment,
    ExtractedReceipt,
    ReceiptVerdict,
)
from server.modules.crm.domain.receipt_extraction import describe_for_operator
from server.modules.crm.repositories.board_repository import BoardRepository
from server.modules.crm.repositories.card_delivery_repository import CardDeliveryRepository
from server.modules.crm.repositories.card_repository import CardRepository
from server.modules.crm.repositories.payment_receipt_repository import PaymentReceiptRepository
from server.modules.crm.services.payment_settings_service import PaymentSettingsService
from server.shared.timezone import to_business_time

CHECK_REUSED = "reused"
CHECK_MEDIA = "media"


@dataclass(frozen=True, slots=True)
class ReceiptMediaRef:
    """Dónde está el archivo del comprobante y qué dice ser."""

    media_path: str | None
    declared_mime: str | None


class ReceiptContext:
    """Lecturas y escrituras alrededor de un comprobante."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._board = BoardRepository(session)
        self._cards = CardRepository(session)
        self._receipts = PaymentReceiptRepository(session)
        self._delivery = CardDeliveryRepository(session)
        self._history = AiChatHistoryRepository(session)
        self._settings = PaymentSettingsService(session)

    async def already_processed(self, wamid: str, org_id: uuid.UUID) -> bool:
        return await self._receipts.get_by_wamid(wamid, org_id) is not None

    async def card_awaiting_payment(
        self, conversation_id: uuid.UUID, org_id: uuid.UUID
    ) -> Card | None:
        """La card de la conversación, solo si está esperando validación de pago."""
        card = await self._cards.get_by_conversation(conversation_id)
        if card is None:
            return None
        stage = await self._board.get_stage_by_id(card.stage_id, org_id)
        if stage is None or stage.pipeline.kind != stages.PIPELINE_HUMAN:
            return None
        return card if stage.name == stages.PAYMENT_VALIDATION else None

    async def media_of(self, wamid: str, org_id: uuid.UUID) -> ReceiptMediaRef:
        row = await self._history.get_by_wamid(wamid, org_id)
        if row is None:
            return ReceiptMediaRef(None, None)
        path = row.message.get("media_path")
        mime = row.message.get("media_mime")
        return ReceiptMediaRef(
            media_path=str(path) if path else None,
            declared_mime=str(mime) if mime else None,
        )

    async def expected_payment(self, card_id: uuid.UUID, org_id: uuid.UUID) -> ExpectedPayment:
        """Contra qué se compara el comprobante.

        Con más de un servicio aceptado no hay un precio único, así que se deja sin
        monto: los checks lo leen como "no comparable" y el caso va a un humano.
        """
        services = await self._delivery.services_for_card(card_id, org_id)
        beneficiary = await self._settings.expected_beneficiary(org_id)
        if len(services) != 1:
            return ExpectedPayment(amount=None, currency="BOB", beneficiary=beneficiary)
        service = services[0]
        return ExpectedPayment(
            amount=service.price_amount, currency=service.moneda, beneficiary=beneficiary
        )

    async def find_by_image(self, sha256: str, org_id: uuid.UUID) -> PaymentReceipt | None:
        return await self._receipts.get_by_sha(sha256, org_id) if sha256 else None

    async def with_reuse_check(
        self, verdict: ReceiptVerdict, reference: str | None, org_id: uuid.UUID
    ) -> ReceiptVerdict:
        """Suma el check anti-reuso por número de transacción.

        Vive acá y no en el dominio puro porque es el único check que depende de lo que
        ya pasó, no solo de lo que dice el comprobante.
        """
        cleaned = (reference or "").strip()
        if not cleaned:
            return verdict  # sin identificador, el check de referencia ya falló
        previous = await self._receipts.get_by_reference(cleaned, org_id)
        if previous is None:
            return _with(verdict, CheckResult(CHECK_REUSED, True, "transacción nueva"), fail=False)
        return _with(
            verdict,
            CheckResult(
                CHECK_REUSED,
                False,
                f"esa transacción ya se usó para validar otro pago ({cleaned})",
            ),
            fail=True,
        )

    async def persist(
        self,
        *,
        card: Card,
        org_id: uuid.UUID,
        wamid: str,
        media: ReceiptMediaRef,
        sha256: str,
        extracted: ExtractedReceipt,
        verdict: ReceiptVerdict,
    ) -> PaymentReceipt:
        return await self._receipts.add(
            PaymentReceipt(
                organization_id=org_id,
                card_id=card.id,
                wamid=wamid,
                media_path=media.media_path,
                # Sin sha (no se pudo abrir el archivo) se marca con el wamid: la columna
                # es NOT NULL y su unique no debe agrupar todos los ilegibles en uno.
                image_sha256=sha256 or f"unreadable:{wamid}",
                amount=extracted.amount,
                currency=extracted.currency,
                paid_at=extracted.paid_at,
                beneficiary=extracted.beneficiary,
                # La referencia se guarda solo si el comprobante pasó. Guardarla en uno
                # rechazado dejaría el unique ocupado y bloquearía al operador que
                # después quiera aprobarlo a mano (un sobrepago legítimo, por ejemplo).
                reference=extracted.reference if verdict.passed else None,
                bank=extracted.bank,
                verdict=verdict.verdict,
                checks=[
                    {"code": c.code, "passed": c.passed, "detail": c.detail} for c in verdict.checks
                ],
                extracted=describe_for_operator(extracted),
            )
        )

    async def set_flags(self, card: Card, flags: list[str]) -> None:
        await self._delivery.set_flags(card, flags)

    async def stage_by_name(self, org_id: uuid.UUID, name: str) -> uuid.UUID | None:
        stage = await self._board.get_stage(org_id, stages.PIPELINE_HUMAN, name)
        return stage.id if stage is not None else None

    async def still_awaiting_payment(self, card_id: uuid.UUID, org_id: uuid.UUID) -> Card | None:
        """Re-lee la card **de la base** y confirma que sigue esperando validación.

        Entre que arrancó el job y ahora pasaron segundos: un operador pudo mover o
        descalificar la card, y su decisión gana siempre. La lectura fuerza ir a la base
        en vez de confiar en lo que la sesión cargó al empezar — que es justamente el
        estado viejo que este chequeo tiene que detectar.
        """
        fresh = await self._board.get_card_fresh(card_id, org_id)
        if fresh is None:
            return None
        stage = await self._board.get_stage_by_id(fresh.stage_id, org_id)
        if stage is None or stage.name != stages.PAYMENT_VALIDATION:
            return None
        return fresh


def unreadable_verdict() -> ReceiptVerdict:
    """Veredicto cuando el archivo no se pudo ni abrir."""
    return ReceiptVerdict(
        verdict=VERDICT_FAIL,
        checks=(CheckResult(CHECK_MEDIA, False, "no se pudo leer el archivo: revisalo a mano"),),
    )


def reused_image_verdict(previous: PaymentReceipt) -> ReceiptVerdict:
    """Veredicto cuando la misma captura ya es el comprobante de otra conversación."""
    seen = (
        f" (procesada el {to_business_time(previous.created_at):%d/%m/%Y})"
        if previous.created_at is not None
        else ""
    )
    return ReceiptVerdict(
        verdict=VERDICT_FAIL,
        checks=(
            CheckResult(
                CHECK_REUSED,
                False,
                f"esa imagen ya se usó como comprobante en otra conversación{seen}: "
                "revisala a mano",
            ),
        ),
    )


def _with(verdict: ReceiptVerdict, check: CheckResult, *, fail: bool) -> ReceiptVerdict:
    return ReceiptVerdict(
        verdict=VERDICT_FAIL if fail else verdict.verdict,
        checks=(*verdict.checks, check),
    )
