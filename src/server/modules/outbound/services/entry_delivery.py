"""Delivers an issued entry (QR) through the `entry_qr_ready` template.

Used when the 24h window is closed: a free-form image send is rejected by Meta, but a
utility template with the QR as header image is allowed. Etapa B of M-Outbound.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.models import Conversation
from server.modules.crm.domain.models import Card, QrEntry
from server.modules.crm.services.qr_image import public_url
from server.modules.outbound.domain.models import PURPOSE_ENTRY
from server.modules.outbound.domain.templates import ENTRY_QR_READY
from server.modules.outbound.services.template_sender import (
    SendRequest,
    TemplateSender,
    TemplateSenderPort,
)
from server.shared.timezone import to_business_time

# "Hola {{1}}!" must read well without a name: "Hola de nuevo!".
_NO_NAME = "de nuevo"


def first_name(full_name: str | None) -> str:
    parts = (full_name or "").split()
    return parts[0] if parts else _NO_NAME


def event_date_text(starts_at: datetime) -> str:
    local = to_business_time(starts_at)
    return f"{local:%d/%m/%Y} a las {local:%H:%M}"


class EntryTemplateDelivery:
    def __init__(self, session: AsyncSession, sender: TemplateSenderPort) -> None:
        self._templates = TemplateSender(session, sender)

    async def send(
        self,
        *,
        card: Card,
        org_id: uuid.UUID,
        conversation: Conversation,
        entry: QrEntry,
        event_name: str,
        starts_at: datetime,
    ) -> bool:
        """True when Meta accepted the template. The QR image is the header."""
        qr_url = public_url(entry.qr_ref)
        result = await self._templates.send(
            SendRequest(
                organization_id=org_id,
                wa_id=conversation.external_id,
                template=ENTRY_QR_READY,
                variables=[
                    first_name(conversation.full_name),
                    event_name,
                    event_date_text(starts_at),
                ],
                purpose=PURPOSE_ENTRY,
                conversation_id=conversation.id,
                card_id=card.id,
                agent_id=conversation.instance.agent_id,
                dedupe_key=f"entry:{entry.id}",
                header_image_url=qr_url,
                mirror_media_url=qr_url,
            )
        )
        return result.sent
