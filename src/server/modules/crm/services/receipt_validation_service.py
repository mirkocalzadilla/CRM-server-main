"""Validar el comprobante que mandó un lead, y actuar según el resultado.

El reparto es el del resto del agente: **el modelo extrae, el código decide.** Acá se
orquesta eso — leer el binario, pedirle los datos al modelo de visión, correr los checks
determinísticos, y a partir del veredicto avanzar el pipeline o dejar el caso para un
humano. Las lecturas y escrituras viven en `receipt_context`; los checks, en
`crm/domain/receipt_checks.py`.

Lo que nunca pasa:

- **No se aprueba con dudas.** Un dato ilegible, una moneda que no corresponde, un
  comprobante ya usado: todo eso deja la card donde está.
- **No se le dice al lead qué check falló.** Un mensaje neutro, uno solo. Explicar el
  criterio es enseñarle a un estafador exactamente qué editar.
- **No se pisa el trabajo de un humano.** Antes de mover la card se re-verifica que siga
  donde estaba: alguien pudo descalificarla mientras el modelo respondía.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.ports import MessageSender
from server.modules.agent.domain.vision_port import VisionPort
from server.modules.agent.repositories.conversation_repository import ConversationRepository
from server.modules.crm.domain import card_flags
from server.modules.crm.domain.models import Card
from server.modules.crm.domain.receipt_checks import ExtractedReceipt, ReceiptVerdict, evaluate
from server.modules.crm.domain.receipt_extraction import (
    EXTRACTION_INSTRUCTIONS,
    EXTRACTION_SCHEMA,
    parse_extraction,
)
from server.modules.crm.domain.receipt_summary import (
    with_validation_note,
    without_validation_note,
)
from server.modules.crm.domain.stages import PAYMENT_VALIDATED
from server.modules.crm.services.board_service import SYSTEM_ACTOR, BoardService
from server.modules.crm.services.receipt_context import (
    ReceiptContext,
    ReceiptMediaRef,
    reused_image_verdict,
    unreadable_verdict,
)
from server.modules.crm.services.receipt_media import load_receipt
from server.shared.logger import get_logger
from server.shared.pubsub import Publisher, crm_channel
from server.shared.timezone import business_today

logger = get_logger(__name__)

# Único mensaje al lead cuando el comprobante no se pudo validar solo. Neutro a
# propósito: no dice qué falló (al lead honesto no le sirve, y al deshonesto le
# regalaría el criterio) y no promete un plazo que el equipo no controla.
REVIEW_REPLY = "Recibí tu comprobante, lo estamos revisando y te confirmamos en un rato 🙌"


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    """Qué se decidió sobre un comprobante."""

    processed: bool = False
    approved: bool = False
    receipt_id: uuid.UUID | None = None
    reason: str | None = None


class ReceiptValidationService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        vision: VisionPort,
        sender: MessageSender,
        publisher: Publisher,
        today: date | None = None,
    ) -> None:
        self._session = session
        self._vision = vision
        self._sender = sender
        self._publisher = publisher
        # El día del negocio, no el UTC: la fecha del comprobante la escribe un banco
        # boliviano, y con "hoy" corrido un comprobante del límite se rechaza por un día
        # que no pasó.
        self._today = today or business_today()
        self._ctx = ReceiptContext(session)
        self._conv = ConversationRepository(session)
        self._board_svc = BoardService(session=session, publisher=publisher)

    async def validate(
        self, conversation_id: uuid.UUID, org_id: uuid.UUID, wamid: str
    ) -> ValidationOutcome:
        """Procesa el comprobante de `wamid`. Idempotente: si ya se procesó, no repite."""
        if await self._ctx.already_processed(wamid, org_id):
            return ValidationOutcome(reason="comprobante ya procesado")

        card = await self._ctx.card_awaiting_payment(conversation_id, org_id)
        if card is None:
            return ValidationOutcome(reason="la card no está esperando validación de pago")

        media = await self._ctx.media_of(wamid, org_id)
        file = load_receipt(media.media_path, media.declared_mime)
        if file is None:
            # No es un fallo del sistema: es un comprobante que no se puede leer.
            return await self._fail(
                card, org_id, wamid, media, "", ExtractedReceipt(), unreadable_verdict()
            )

        reused = await self._ctx.find_by_image(file.sha256, org_id)
        if reused is not None and reused.card_id == card.id:
            # La misma imagen en la misma card: el lead reenvió lo que ya se le respondió.
            # No se gasta una llamada al modelo y no se le repite nada.
            logger.info("receipt.duplicate_image", wamid=wamid, previous=str(reused.id))
            return ValidationOutcome(reason="imagen ya usada", receipt_id=reused.id)
        if reused is not None:
            # La misma captura desde OTRA conversación tiene forma de fraude, y descartarla
            # en silencio dejaba la card esperando con el panel vacío (UAT 2026-08-31).
            # Sin llamada al modelo: la imagen ya se conoce. El sha placeholder deja el
            # unique anti-reuso en manos de la fila original; cada reintento deja su fila.
            logger.info("receipt.duplicate_image_cross_card", wamid=wamid, previous=str(reused.id))
            return await self._fail(
                card,
                org_id,
                wamid,
                media,
                f"reused:{wamid}",
                ExtractedReceipt(),
                reused_image_verdict(reused),
                notify=True,
            )

        extracted = await self._extract(file.content, file.mime_type)
        expected = await self._ctx.expected_payment(card.id, org_id)
        verdict = await self._ctx.with_reuse_check(
            evaluate(extracted, expected, today=self._today), extracted.reference, org_id
        )

        if not verdict.passed:
            return await self._fail(
                card, org_id, wamid, media, file.sha256, extracted, verdict, notify=True
            )
        return await self._approve(card, org_id, wamid, media, file.sha256, extracted, verdict)

    async def _extract(self, content: bytes, mime_type: str) -> ExtractedReceipt:
        """Una llamada al modelo. Si falla, no se pudo leer: va a un humano."""
        try:
            raw = await self._vision.extract(
                content=content,
                mime_type=mime_type,
                instructions=EXTRACTION_INSTRUCTIONS,
                schema=EXTRACTION_SCHEMA,
            )
        except Exception as exc:
            logger.warning("receipt.vision_failed", error=str(exc))
            return ExtractedReceipt()
        return parse_extraction(raw)

    async def _fail(
        self,
        card: Card,
        org_id: uuid.UUID,
        wamid: str,
        media: ReceiptMediaRef,
        sha256: str,
        extracted: ExtractedReceipt,
        verdict: ReceiptVerdict,
        *,
        notify: bool = False,
    ) -> ValidationOutcome:
        """No se pudo validar solo: queda para un humano.

        La card **no se mueve**: sigue en "Por validar pago", que es justamente la
        bandeja de lo que hay que revisar. Se guarda todo lo leído para que el operador
        no tenga que descifrar la imagen de nuevo, y el resumen IA dice qué falló para
        que quien retome sepa qué subsanar sin abrir el panel (UAT 2026-08-31).
        """
        receipt = await self._ctx.persist(
            card=card,
            org_id=org_id,
            wamid=wamid,
            media=media,
            sha256=sha256,
            extracted=extracted,
            verdict=verdict,
        )
        await self._ctx.set_flags(card, card_flags.add(card.flags, card_flags.RECEIPT_REVIEW))
        conversation = await self._conv.get_by_id(card.conversation_id, org_id)
        if conversation is not None:
            conversation.ai_summary = with_validation_note(conversation.ai_summary, verdict)
        await self._session.commit()
        await self._publish_needs_review(card, org_id)
        if notify:
            await self._notify_lead(card, org_id)
        logger.info(
            "receipt.needs_review",
            card_id=str(card.id),
            wamid=wamid,
            failed=list(verdict.failed_codes),
        )
        return ValidationOutcome(
            processed=True, approved=False, receipt_id=receipt.id, reason="checks en rojo"
        )

    async def _approve(
        self,
        card: Card,
        org_id: uuid.UUID,
        wamid: str,
        media: ReceiptMediaRef,
        sha256: str,
        extracted: ExtractedReceipt,
        verdict: ReceiptVerdict,
    ) -> ValidationOutcome:
        """Checks en verde: se marca el pago validado, y de ahí sale la entrega."""
        receipt = await self._ctx.persist(
            card=card,
            org_id=org_id,
            wamid=wamid,
            media=media,
            sha256=sha256,
            extracted=extracted,
            verdict=verdict,
        )
        # El pago se aprobó solo, así que queda pendiente de cotejarse contra el banco:
        # un comprobante es una imagen, y una imagen se puede editar. El aviso hace
        # visible en el tablero qué falta conciliar.
        await self._ctx.set_flags(
            card,
            card_flags.add(
                card_flags.remove(card.flags, card_flags.RECEIPT_REVIEW),
                card_flags.PAYMENT_UNCONFIRMED,
            ),
        )
        # Un comprobante nuevo en verde desactualiza la nota de revisión del resumen:
        # dejarla diría "hay que subsanar algo" sobre un pago ya aprobado.
        conversation = await self._conv.get_by_id(card.conversation_id, org_id)
        if conversation is not None:
            conversation.ai_summary = without_validation_note(conversation.ai_summary)
        await self._session.commit()

        target = await self._ctx.stage_by_name(org_id, PAYMENT_VALIDATED)
        if target is None:
            logger.warning("receipt.no_validated_stage", card_id=str(card.id))
            return ValidationOutcome(processed=True, receipt_id=receipt.id, reason="falta el stage")

        fresh = await self._ctx.still_awaiting_payment(card.id, org_id)
        if fresh is None:
            # Comparar-y-mover: entre que arrancó el job y ahora, un operador pudo mover
            # o descalificar la card. Su decisión gana.
            logger.info("receipt.card_moved_meanwhile", card_id=str(card.id))
            return ValidationOutcome(
                processed=True,
                receipt_id=receipt.id,
                reason="un humano movió la card mientras se validaba",
            )
        # La aprobación se registra recién acá, en el mismo commit que el move: un
        # comprobante en verde cuya card no se pudo mover queda sin aprobar, y el panel
        # le ofrece al operador validarlo (server#292).
        receipt.approved_at = datetime.now(UTC)
        receipt.approved_by = SYSTEM_ACTOR
        await self._board_svc.move_card(fresh.id, target, SYSTEM_ACTOR, org_id)
        logger.info("receipt.approved", card_id=str(card.id), wamid=wamid)
        return ValidationOutcome(processed=True, approved=True, receipt_id=receipt.id)

    async def _publish_needs_review(self, card: Card, org_id: uuid.UUID) -> None:
        """Avisa al CRM que hay un comprobante para revisar. Best-effort: el poll del
        front lo muestra igual; este evento solo lo adelanta."""
        try:
            await self._publisher.publish(
                crm_channel(org_id),
                {
                    "type": "receipt_needs_review",
                    "card_id": str(card.id),
                    "conversation_id": str(card.conversation_id),
                },
            )
        except Exception as exc:
            logger.warning("receipt.publish_failed", card_id=str(card.id), error=str(exc))

    async def _notify_lead(self, card: Card, org_id: uuid.UUID) -> None:
        """Un mensaje, neutro. Un fallo de envío no invalida la validación."""
        conversation = await self._conv.get_by_id(card.conversation_id, org_id)
        if conversation is None:  # pragma: no cover - FK NOT NULL
            return
        try:
            await self._sender.send_text(conversation.external_id, REVIEW_REPLY)
        except Exception as exc:
            logger.warning("receipt.notify_failed", card_id=str(card.id), error=str(exc))
