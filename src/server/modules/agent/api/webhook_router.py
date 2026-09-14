import hashlib
import hmac

import sentry_sdk
from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import get_settings
from server.modules.agent.domain.whatsapp_schemas import WAWebhookPayload
from server.modules.agent.services.webhook_service import WhatsAppWebhookService
from server.shared.database import get_db_session
from server.shared.dispatcher import dispatcher
from server.shared.logger import get_logger

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = get_logger(__name__)


def _verify_signature(body: bytes, signature_header: str, app_secret: str) -> bool:
    """Verifica que el payload viene de Meta usando HMAC-SHA256."""
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(
        app_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    received = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, received)


@router.get("/whatsapp")
async def verify_webhook(
    hub_mode: str = Query(default="", alias="hub.mode"),
    hub_challenge: str = Query(default="", alias="hub.challenge"),
    hub_verify_token: str = Query(default="", alias="hub.verify_token"),
) -> Response:
    """Verificación de webhook que exige Meta antes de activar la suscripción."""
    settings = get_settings()
    if hub_mode == "subscribe" and hub_verify_token == settings.whatsapp_verify_token:
        return Response(content=hub_challenge, media_type="text/plain")
    return Response(status_code=status.HTTP_403_FORBIDDEN)


@router.post("/whatsapp", status_code=status.HTTP_200_OK)
async def receive_webhook(
    request: Request,
    payload: WAWebhookPayload,
    session: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> dict[str, str]:
    """Recibe eventos de Meta (mensajes entrantes, estados de entrega, etc.).

    Verifica la firma HMAC-SHA256 si WHATSAPP_APP_SECRET está configurado.
    Meta requiere siempre un 200 OK; los errores internos solo se loggean.
    """
    settings = get_settings()

    if settings.whatsapp_app_secret:
        signature = request.headers.get("X-Hub-Signature-256", "")
        body = await request.body()
        if not _verify_signature(body, signature, settings.whatsapp_app_secret):
            logger.warning("whatsapp.invalid_signature")
            return Response(status_code=status.HTTP_403_FORBIDDEN)  # type: ignore[return-value]

    try:
        service = WhatsAppWebhookService(session, dispatcher)
        await service.process(payload)
        await session.commit()
    except Exception as exc:
        sentry_sdk.capture_exception(exc)
        logger.error("whatsapp.webhook_processing_error", error=str(exc))
    return {"status": "ok"}
