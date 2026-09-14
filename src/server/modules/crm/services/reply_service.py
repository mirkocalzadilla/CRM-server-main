"""Reply humano desde el CRM (B3): envía por WhatsApp y persiste en el hilo espejo.

Solo con `is_ai_active=False` (takeover). Enviar y persistir son atómicos a nivel de
intención: si Meta falla no se persiste (se envía primero, se persiste después).
Además de texto: adjuntos (imagen/PDF, #251) y el QR de pago manual.
"""

from __future__ import annotations

import uuid

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.models import Conversation
from server.modules.agent.domain.ports import MessageSender
from server.modules.agent.repositories.conversation_repository import ConversationRepository
from server.modules.agent.services.whatsapp_service import WhatsAppSender
from server.modules.crm.api.schemas import ThreadMessage
from server.modules.crm.repositories.board_repository import BoardRepository
from server.modules.crm.repositories.mirror_repository import MirrorRepository
from server.modules.crm.services.media_store import StoredMedia
from server.modules.crm.services.payment_settings_service import PaymentSettingsService
from server.shared.exceptions import (
    ExternalServiceError,
    NotFoundException,
    OutsideWindowError,
    ValidationException,
)


class ReplyService:
    def __init__(self, *, session: AsyncSession, sender: MessageSender | None = None) -> None:
        self._session = session
        self._sender = sender or WhatsAppSender()
        self._board = BoardRepository(session)
        self._conv = ConversationRepository(session)
        self._mirror = MirrorRepository(session)

    async def send_human_reply(
        self,
        card_id: uuid.UUID,
        text: str,
        sent_by: uuid.UUID,
        org_id: uuid.UUID,
    ) -> ThreadMessage:
        conversation = await self._takeover_conversation(card_id, org_id)
        try:
            await self._sender.send_text(conversation.external_id, text)
        except (OutsideWindowError, httpx.HTTPError) as exc:
            raise ExternalServiceError(f"error al enviar por WhatsApp: {exc}") from exc
        return await self._mirror_human(conversation, org_id, sent_by, text)

    async def send_human_media(
        self,
        card_id: uuid.UUID,
        media: StoredMedia,
        caption: str,
        sent_by: uuid.UUID,
        org_id: uuid.UUID,
    ) -> ThreadMessage:
        """Envía un adjunto ya validado/persistido (o el QR de pago) y lo espeja (#251)."""
        conversation = await self._takeover_conversation(card_id, org_id)
        try:
            if media.media_type == "image":
                await self._sender.send_image(conversation.external_id, media.url, caption)
            else:
                await self._sender.send_document(
                    conversation.external_id, media.url, media.filename, caption
                )
        except (OutsideWindowError, httpx.HTTPError) as exc:
            raise ExternalServiceError(f"error al enviar por WhatsApp: {exc}") from exc
        return await self._mirror_human(conversation, org_id, sent_by, caption, media)

    async def send_payment_qr(
        self, card_id: uuid.UUID, sent_by: uuid.UUID, org_id: uuid.UUID
    ) -> ThreadMessage:
        """QR de pago manual: misma imagen que envía el agente (`enviar_qr_pago`) —
        la de la organización si la cargó, si no la global."""
        url = await PaymentSettingsService(self._session).resolve_qr_url(org_id)
        qr = StoredMedia(media_type="image", url=url, filename="")
        return await self.send_human_media(card_id, qr, "", sent_by, org_id)

    async def _takeover_conversation(self, card_id: uuid.UUID, org_id: uuid.UUID) -> Conversation:
        card = await self._board.get_card(card_id, org_id)
        if card is None:
            raise NotFoundException("card no encontrada")
        conversation = await self._conv.get_by_id(card.conversation_id, org_id)
        if conversation is None:
            raise NotFoundException("card no encontrada")
        if conversation.is_ai_active:
            raise ValidationException("el agente está activo; primero desactivá el takeover")
        return conversation

    async def _mirror_human(
        self,
        conversation: Conversation,
        org_id: uuid.UUID,
        sent_by: uuid.UUID,
        text: str,
        media: StoredMedia | None = None,
    ) -> ThreadMessage:
        row = await self._mirror.add_app_message(
            agent_id=conversation.instance.agent_id,
            organization_id=org_id,
            session_id=conversation.external_id,
            sender=str(sent_by),
            message=text,
            media_type=media.media_type if media else None,
            media_url=media.url if media else None,
        )
        sent_at = row.message_time
        await self._session.commit()
        return ThreadMessage(
            sender="human",
            text=text,
            at=sent_at,
            type=media.media_type if media else "text",
            media_url=media.url if media else None,
        )
