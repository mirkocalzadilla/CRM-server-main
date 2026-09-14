"""Wiring del handler que consume `agent:dispatch` (M1/N) → `AgentService` (M4).

`build_agent_service` arma el servicio con las implementaciones reales de los ports
(store sobre M0, `AnthropicAdapter`, `WhatsAppSender`); `agent_dispatch_handler` es
lo que el worker inyecta en su loop. Errores del turno se loguean sin re-lanzar: el
lock se libera normalmente y el worker sigue (la persistencia del turno, si llegó a
`save_turn`, ya está commiteada).
"""

from __future__ import annotations

import uuid

import httpx
import sentry_sdk
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import get_settings
from server.modules.agent.domain.tool_catalogue import build_default_registry
from server.modules.agent.repositories.conversation_repository import ConversationRepository
from server.modules.agent.services.agent_service import AgentService
from server.modules.agent.services.conversation_store import ConversationStoreAdapter
from server.modules.agent.services.handoff_service import HandoffService
from server.modules.agent.services.llm_factory import build_llm
from server.modules.agent.services.summary_service import SummaryService
from server.modules.agent.services.whatsapp_service import WhatsAppSender
from server.modules.crm.services.card_service import CardService
from server.modules.crm.services.delivery_trigger import retry_pending_delivery
from server.modules.crm.services.payment_settings_service import PaymentSettingsService
from server.modules.crm.services.receipt_trigger import enqueue_receipts_awaiting_validation
from server.modules.crm.services.service_capture_service import ServiceCaptureService
from server.shared.dispatcher import dispatcher
from server.shared.exceptions import OutsideWindowError
from server.shared.logger import get_logger
from server.shared.pubsub import crm_channel, publisher

logger = get_logger(__name__)


def build_agent_service(
    session: AsyncSession, *, payment_qr_url: str | None = None
) -> AgentService:
    """Arma el servicio con las implementaciones reales de los ports.

    `payment_qr_url` llega resuelto por organización (config de pagos del tenant, con
    fallback al global). Se pasa explícito porque el QR es por tenant y el builder no
    conoce el tenant; `None` = usar el global (útil para smokes y scripts).
    """
    settings = get_settings()
    return AgentService(
        store=ConversationStoreAdapter(session),
        llm=build_llm(settings, role="loop"),
        registry=build_default_registry(),
        sender=WhatsAppSender(),
        handoff=HandoffService(
            session=session,
            summarizer=build_llm(settings, role="summary"),  # resumen de handoff (Haiku, barato)
            publisher=publisher,
        ),
        summary=SummaryService(
            session=session,
            summarizer=build_llm(settings, role="summary"),  # resumen barato por transición (Haiku)
        ),
        capture=ServiceCaptureService(session=session),  # bot → card_service captured (#133)
        # Imagen del QR de pago (cierre pago_qr): la de la organización si la cargó,
        # si no la global.
        payment_qr_url=payment_qr_url if payment_qr_url is not None else settings.payment_qr_url,
        summary_refresh_every_n_turns=settings.summary_refresh_every_n_turns,  # #254
    )


async def agent_dispatch_handler(
    conversation_id: uuid.UUID, tenant_id: uuid.UUID, session: AsyncSession
) -> None:
    qr_url = await PaymentSettingsService(session).resolve_qr_url(tenant_id)
    # High-water mark before the turn: what this turn answers is everything above it,
    # which is what the receipt trigger below needs to find the photos of this turn.
    answered_before = await _answered_through(session, conversation_id, tenant_id)
    service = build_agent_service(session, payment_qr_url=qr_url)
    try:
        # Re-apertura: un lead cerrado que vuelve a escribir entra como conversación
        # nueva (= oportunidad nueva en Gestión Venta, contexto limpio) decidido en el
        # webhook por `conversation.closed_at` — acá ya llega fresca (#163).
        # Los fallos del LLM ya los maneja adentro (`LLMError` → handoff agent_error);
        # lo que llega acá es lo demás: bug interno, DB, envío a Meta.
        await service.process_message(conversation_id, tenant_id)
    except Exception as exc:  # un turno que falla no debe tumbar el worker
        sentry_sdk.capture_exception(exc)
        logger.error(
            "agent.dispatch_error",
            conversation_id=str(conversation_id),
            error=str(exc),
        )
        # Visibilidad en el CRM también para estos fallos (server#288): la sesión puede
        # haber quedado en estado fallido → rollback antes de persistir el evento.
        category = (
            "delivery" if isinstance(exc, OutsideWindowError | httpx.HTTPError) else "internal"
        )
        try:
            await session.rollback()
            await ConversationStoreAdapter(session).save_agent_error(
                conversation_id, tenant_id, category=category, detail=str(exc)
            )
            await publisher.publish(
                crm_channel(tenant_id),
                {
                    "type": "agent_error",
                    "conversation_id": str(conversation_id),
                    "category": category,
                },
            )
        except Exception as visibility_exc:  # best-effort: nunca tumbar al worker por esto
            logger.error(
                "agent.dispatch_error_visibility_failed",
                conversation_id=str(conversation_id),
                error=str(visibility_exc),
            )

    # Proyección CRM: reconcilia la card desde el estado (ya commiteado) de la
    # conversación. En su propio try — el funnel ya quedó persistido aunque el
    # turno haya fallado (p. ej. el envío a Meta), y un error de CRM no debe tumbar al worker.
    try:
        await CardService(session=session, publisher=publisher).sync(conversation_id, tenant_id)
    except Exception as exc:
        sentry_sdk.capture_exception(exc)
        logger.error("crm.sync_error", conversation_id=str(conversation_id), error=str(exc))

    # Si el turno derivó por comprobante, recién ahora la card está en "Por validar pago":
    # el job de visión que encoló el webhook ya corrió y se descartó. Se re-encolan las
    # fotos de este turno (server#290). Best-effort: nunca tumba el loop del worker.
    try:
        await enqueue_receipts_awaiting_validation(
            session, dispatcher, conversation_id, tenant_id, after_order=answered_before
        )
    except Exception as exc:
        sentry_sdk.capture_exception(exc)
        logger.error("receipt.trigger_error", conversation_id=str(conversation_id), error=str(exc))

    # El lead escribió, así que la ventana de 24h de WhatsApp está abierta: si había una
    # entrega esperando justamente por eso, este es el momento en que puede salir.
    await retry_pending_delivery(session, conversation_id, tenant_id)


async def _answered_through(
    session: AsyncSession, conversation_id: uuid.UUID, tenant_id: uuid.UUID
) -> int:
    conversation = await ConversationRepository(session).get_by_id(conversation_id, tenant_id)
    return conversation.answered_through_order if conversation is not None else 0
