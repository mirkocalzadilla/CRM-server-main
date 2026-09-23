import os
import uuid

import sentry_sdk
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import get_settings
from server.modules.agent.domain.models import AgentInstance, AiChatHistory, Conversation
from server.modules.agent.domain.ports import MessageSender
from server.modules.agent.domain.whatsapp_schemas import (
    WAIncomingMessage,
    WAValue,
    WAWebhookPayload,
)
from server.modules.agent.repositories.agent_instance_repository import AgentInstanceRepository
from server.modules.agent.repositories.ai_chat_history_repository import AiChatHistoryRepository
from server.modules.agent.repositories.conversation_repository import ConversationRepository
from server.modules.agent.services.whatsapp_service import WhatsAppSender
from server.modules.crm.services.extra_receipt_service import flag_extra_receipt_if_processed
from server.modules.outbound.domain.opt_out import is_opt_out_request
from server.modules.outbound.services.opt_out_service import OptOutService
from server.modules.outbound.services.status_service import OutboundStatusService
from server.shared.dispatcher import Dispatcher
from server.shared.logger import get_logger

logger = get_logger(__name__)

_SUPPORTED_MEDIA_TYPES = {"image", "document"}

_MIME_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "video/mp4": ".mp4",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
}


def _ext_for(mime_type: str) -> str:
    return _MIME_EXT.get(mime_type, ".bin")


class WhatsAppWebhookService:
    """Processes incoming WhatsApp events.

    Routes text and media messages to the appropriate handler, persists turns
    in `ai_chat_histories`, and enqueues `agent:dispatch` for the worker.
    """

    def __init__(
        self,
        session: AsyncSession,
        dispatcher: Dispatcher,
        sender: MessageSender | None = None,
    ) -> None:
        self.session = session
        self.dispatcher = dispatcher
        self._sender: MessageSender = sender if sender is not None else WhatsAppSender()
        self.status_service = OutboundStatusService(session)
        self.opt_out_service = OptOutService(session, self._sender)
        self.instance_repo = AgentInstanceRepository(session)
        self.conv_repo = ConversationRepository(session)
        self.history_repo = AiChatHistoryRepository(session)

    async def process(self, payload: WAWebhookPayload) -> None:
        for entry in payload.entry:
            for change in entry.changes:
                if change.field != "messages":
                    continue
                await self._handle_change(change.value)

    async def _handle_change(self, value: WAValue) -> None:
        # Before the instance lookup: a status-only change has no messages, and an
        # unknown instance must not hide a delivery failure.
        self._log_failed_statuses(value)
        await self.status_service.apply(value.statuses)
        instance = await self.instance_repo.get_by_whatsapp_number(
            value.metadata.display_phone_number
        )
        if instance is None:
            logger.warning(
                "whatsapp.unknown_instance",
                display_phone_number=value.metadata.display_phone_number,
            )
            return

        organization_id = instance.agent.organization_id
        for msg in value.messages:
            # Dedup por wamid ANTES de tocar cualquier cosa: Meta reintenta la entrega
            # del webhook, y un reintento acá abajo crearía una conversación (= una
            # oportunidad fantasma en el tablero si la anterior quedó cerrada), volvería
            # a descargar el media de Meta y encolaría el trabajo de nuevo.
            if await self.history_repo.exists_by_wamid(msg.id, organization_id):
                logger.info("whatsapp.duplicate_wamid", wamid=msg.id, wa_id=msg.from_)
                continue
            if msg.type == "text" and msg.text is not None:
                await self._handle_text(instance, msg.from_, msg.text.body, msg.id)
            elif msg.type in _SUPPORTED_MEDIA_TYPES:
                await self._handle_media(instance, msg.from_, msg)

    def _log_failed_statuses(self, value: WAValue) -> None:
        """A message Meta accepted (2xx) can still die asynchronously — invalid media,
        closed window, filtering — and the only signal is a `failed` status here.
        Dropping them made those deaths invisible: the thread mirrored the message as
        sent and the card closed as won while the lead received nothing (#298). The
        log is the durable record; Sentry gets an explicit capture because the
        logging integration has `event_level=None`, without the phone number (PII
        posture of `observability.py`)."""
        for status in value.statuses:
            if status.get("status") != "failed":
                continue
            wamid = status.get("id")
            errors = status.get("errors", [])
            logger.error(
                "whatsapp.delivery_failed",
                wamid=wamid,
                recipient=status.get("recipient_id"),
                errors=errors,
            )
            with sentry_sdk.new_scope() as scope:
                scope.set_context("whatsapp_status", {"wamid": wamid, "errors": errors})
                sentry_sdk.capture_message("whatsapp.delivery_failed", level="error")

    async def _get_or_create_conversation(
        self,
        instance: AgentInstance,
        organization_id: uuid.UUID,
        wa_id: str,
    ) -> Conversation:
        conversation = await self.conv_repo.get_latest_by_external_id(wa_id, organization_id)
        # A closed lead (its last opportunity is closed) that writes again starts a
        # fresh conversation = a new opportunity in Gestión Venta, with clean context; the
        # closed one stays as history (#163). An open conversation is reused so the
        # ongoing chat (or a human mid-handling) keeps its thread.
        if conversation is None or conversation.closed_at is not None:
            previous_id = conversation.id if conversation is not None else None
            conversation = await self.conv_repo.add(
                Conversation(
                    instance_id=instance.id,
                    organization_id=organization_id,
                    external_id=wa_id,
                )
            )
            logger.info(
                "whatsapp.conversation_created",
                wa_id=wa_id,
                conv_id=str(conversation.id),
                reopened_from=str(previous_id) if previous_id is not None else None,
            )
        return conversation

    async def _handle_text(
        self,
        instance: AgentInstance,
        wa_id: str,
        text: str,
        wamid: str,
    ) -> None:
        organization_id = instance.agent.organization_id
        conversation = await self._get_or_create_conversation(instance, organization_id, wa_id)
        await self.history_repo.add(
            AiChatHistory(
                agent_id=instance.agent_id,
                organization_id=organization_id,
                thread_id=str(conversation.id),
                session_id=wa_id,
                message={"role": "user", "content": text, "wamid": wamid, "channel": "whatsapp"},
            )
        )
        logger.info("whatsapp.text_stored", wa_id=wa_id, wamid=wamid, conv_id=str(conversation.id))
        if is_opt_out_request(text):
            # Policy reply, not an agent turn: the lead asked not to be contacted.
            await self.opt_out_service.register(
                organization_id=organization_id,
                agent_id=instance.agent_id,
                conversation_id=conversation.id,
                wa_id=wa_id,
            )
            return
        await self.dispatcher.enqueue(conversation_id=conversation.id, tenant_id=organization_id)

    async def _handle_media(
        self,
        instance: AgentInstance,
        wa_id: str,
        msg: WAIncomingMessage,
    ) -> None:
        organization_id = instance.agent.organization_id
        conversation = await self._get_or_create_conversation(instance, organization_id, wa_id)

        if msg.type == "image" and msg.image is not None:
            media_id = msg.image.id
            caption = msg.image.caption
        elif msg.type == "document" and msg.document is not None:
            media_id = msg.document.id
            caption = msg.document.caption
        else:
            return

        sender = WhatsAppSender()
        content, mime_type = await sender.download_media(media_id)
        media_path = self._save_media(str(organization_id), msg.id, mime_type, content)

        await self.history_repo.add(
            AiChatHistory(
                agent_id=instance.agent_id,
                organization_id=organization_id,
                thread_id=str(conversation.id),
                session_id=wa_id,
                message={
                    "role": "user",
                    "content": f"[{msg.type}: {caption or 'sin descripción'}]",
                    "wamid": msg.id,
                    "channel": "whatsapp",
                    "media_type": msg.type,
                    "media_path": media_path,
                    # El mime real, no deducible de la extensión (un mime desconocido se
                    # guarda como `.bin`). Lo necesita la validación por visión para
                    # armar el bloque correcto sin adivinar.
                    "media_mime": mime_type,
                    "media_caption": caption,
                },
            )
        )
        logger.info("whatsapp.media_stored", wa_id=wa_id, wamid=msg.id, type=msg.type)
        # Un comprobante que llega con el pago ya procesado (card en "Pago validado" o
        # más adelante) no es el que se está esperando: se avisa y no se toca nada. Acá
        # y no en el turno del agente, porque en el pipeline humano la IA está callada.
        extra = await flag_extra_receipt_if_processed(
            conversation.id, organization_id, self.session
        )
        if not extra:
            # Solo se manda a validar lo que puede ser el comprobante esperado. Un
            # comprobante extra ya quedó marcado para que lo mire un humano.
            await self.dispatcher.enqueue_vision(
                conversation_id=conversation.id, tenant_id=organization_id, wamid=msg.id
            )
        await self.dispatcher.enqueue(conversation_id=conversation.id, tenant_id=organization_id)

    def _save_media(self, organization_id: str, wamid: str, mime_type: str, content: bytes) -> str:
        """Saves binary to MEDIA_ROOT and returns the relative path."""
        settings = get_settings()
        org_dir = os.path.join(settings.media_root, organization_id)
        os.makedirs(org_dir, exist_ok=True)
        filename = f"{wamid}{_ext_for(mime_type)}"
        with open(os.path.join(org_dir, filename), "wb") as f:
            f.write(content)
        return f"{organization_id}/{filename}"
