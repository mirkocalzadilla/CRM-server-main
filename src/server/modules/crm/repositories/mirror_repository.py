"""Lectura de las fuentes del hilo espejo (read model del CRM, §"M-CRM").

`ai_chat_histories` (entrantes del lead + respuestas del agente, por `thread_id`) y
`app_chat_histories` (mensajes del humano en takeover, por `session_id`). El merge
ordenado lo hace `services.mirror.build_thread`.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.models import AiChatHistory, AppChatHistory


class MirrorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add_app_message(
        self,
        *,
        agent_id: uuid.UUID,
        organization_id: uuid.UUID,
        session_id: str,
        sender: str,
        message: str,
        media_type: str | None = None,
        media_url: str | None = None,
    ) -> AppChatHistory:
        """Inserta un mensaje humano (takeover). El caller hace commit (§capa servicio)."""
        row = AppChatHistory(
            agent_id=agent_id,
            organization_id=organization_id,
            session_id=session_id,
            sender=sender,
            message=message,
            media_type=media_type,
            media_url=media_url,
        )
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)  # carga message_time (server_default now())
        return row

    async def last_human_reply_by_session(
        self, session_ids: Sequence[str], organization_id: uuid.UUID
    ) -> dict[str, datetime]:
        """Por cada `session_id` (teléfono del lead), el timestamp de la última respuesta
        humana (takeover). Batch para el badge de "sin responder" del board, tenant-scoped.
        Sin `since`: una respuesta de una oportunidad anterior es más vieja que el inbound
        actual, así que no apaga el badge por error (#163)."""
        if not session_ids:
            return {}
        result = await self.session.execute(
            select(AppChatHistory.session_id, func.max(AppChatHistory.message_time))
            .where(
                AppChatHistory.session_id.in_(session_ids),
                AppChatHistory.organization_id == organization_id,
            )
            .group_by(AppChatHistory.session_id)
        )
        return dict(result.tuples().all())

    async def ai_history(self, thread_id: str) -> list[AiChatHistory]:
        result = await self.session.execute(
            select(AiChatHistory)
            .where(AiChatHistory.thread_id == thread_id)
            .order_by(AiChatHistory.message_order)
        )
        return list(result.scalars().all())

    async def app_history(
        self, session_id: str, organization_id: uuid.UUID, since: datetime | None = None
    ) -> list[AppChatHistory]:
        # Tenant-scoped: session_id is the lead's phone (external_id), which is unique
        # only within an org — two orgs can share a number, so filter by org to avoid
        # leaking human messages cross-tenant into the mirror thread.
        # `app_chat_histories` has no conversation_id (keyed by phone), so a returning
        # lead's NEW opportunity (#163) would otherwise inherit the human messages of the
        # previous closed one. `since` (the conversation's created_at) scopes the thread
        # to this opportunity's lifetime.
        conditions = [
            AppChatHistory.session_id == session_id,
            AppChatHistory.organization_id == organization_id,
        ]
        if since is not None:
            conditions.append(AppChatHistory.message_time >= since)
        result = await self.session.execute(
            select(AppChatHistory).where(*conditions).order_by(AppChatHistory.message_time)
        )
        return list(result.scalars().all())
