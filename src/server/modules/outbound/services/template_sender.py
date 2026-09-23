"""Single entry point for business-initiated sends (Meta templates).

Every automated or manual template send goes through `TemplateSender.send`: it
honours opt-outs, makes the send idempotent via `dedupe_key`, records the row before
calling Meta (a crash mid-send still leaves a trace), stores Meta's `wamid` for status
tracking, and mirrors the rendered text into the CRM thread.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.models import AiChatHistory
from server.modules.agent.repositories.ai_chat_history_repository import AiChatHistoryRepository
from server.modules.outbound.domain.models import (
    STATUS_FAILED,
    STATUS_SENT,
    STATUS_SKIPPED,
    OutboundMessage,
)
from server.modules.outbound.domain.templates import TemplateSpec, build_components
from server.modules.outbound.repositories.opt_out_repository import OptOutRepository
from server.modules.outbound.repositories.outbound_repository import OutboundMessageRepository
from server.shared.logger import get_logger

logger = get_logger(__name__)


@runtime_checkable
class TemplateSenderPort(Protocol):
    async def send_template(
        self, to: str, name: str, lang: str, components: list[dict[str, object]]
    ) -> str | None: ...


@dataclass(frozen=True)
class SendRequest:
    organization_id: uuid.UUID
    wa_id: str
    template: TemplateSpec
    variables: list[str]
    purpose: str
    conversation_id: uuid.UUID | None = None
    card_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    dedupe_key: str | None = None
    header_image_url: str | None = None
    mirror_media_url: str | None = None


@dataclass(frozen=True)
class SendResult:
    status: str
    message_id: uuid.UUID | None
    reason: str | None = None

    @property
    def sent(self) -> bool:
        return self.status == STATUS_SENT


class TemplateSender:
    def __init__(self, session: AsyncSession, sender: TemplateSenderPort) -> None:
        self._repo = OutboundMessageRepository(session)
        self._opt_outs = OptOutRepository(session)
        self._history = AiChatHistoryRepository(session)
        self._sender = sender

    async def send(self, req: SendRequest) -> SendResult:
        if req.dedupe_key and await self._repo.exists_dedupe(req.dedupe_key):
            return SendResult(STATUS_SKIPPED, None, "duplicate")
        if await self._opt_outs.is_opted_out(req.organization_id, req.wa_id):
            row = await self._record(req, STATUS_SKIPPED, error_detail="opt_out")
            return SendResult(STATUS_SKIPPED, row.id, "opt_out")

        components = build_components(req.template, req.variables, req.header_image_url)
        row = await self._record(req, STATUS_FAILED)  # pessimistic until Meta answers
        try:
            wamid = await self._sender.send_template(
                req.wa_id, req.template.name, req.template.language, components
            )
        except Exception as exc:
            row.error_detail = str(exc)[:500]
            logger.warning(
                "outbound.send_failed",
                template=req.template.name,
                wa_id=req.wa_id,
                error=str(exc),
            )
            return SendResult(STATUS_FAILED, row.id, "meta_error")

        row.status = STATUS_SENT
        row.wamid = wamid
        row.sent_at = datetime.now(UTC)
        row.dedupe_key = req.dedupe_key  # only a success claims the key (UNIQUE column)
        await self._mirror(req, row.rendered_text)
        logger.info("outbound.sent", template=req.template.name, purpose=req.purpose, wamid=wamid)
        return SendResult(STATUS_SENT, row.id)

    async def _record(
        self, req: SendRequest, status: str, *, error_detail: str | None = None
    ) -> OutboundMessage:
        return await self._repo.add(
            OutboundMessage(
                organization_id=req.organization_id,
                conversation_id=req.conversation_id,
                card_id=req.card_id,
                wa_id=req.wa_id,
                template_name=req.template.name,
                language=req.template.language,
                purpose=req.purpose,
                variables={"body": req.variables},
                rendered_text=req.template.render(req.variables),
                status=status,
                error_detail=error_detail,
            )
        )

    async def _mirror(self, req: SendRequest, text: str) -> None:
        """Show the template in the CRM thread as an assistant message."""
        if req.conversation_id is None or req.agent_id is None:
            return
        message: dict[str, object] = {
            "role": "assistant",
            "content": text,
            "channel": "whatsapp",
            "kind": "template",
            "template": req.template.name,
        }
        if req.mirror_media_url:
            message["media_type"] = "image"
            message["media_url"] = req.mirror_media_url
        await self._history.add(
            AiChatHistory(
                agent_id=req.agent_id,
                organization_id=req.organization_id,
                thread_id=str(req.conversation_id),
                session_id=req.wa_id,
                message=message,
            )
        )
