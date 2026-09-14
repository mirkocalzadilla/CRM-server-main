from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.models import HandoffEvent


class HandoffEventRepository:
    """Persiste eventos de derivación a humano (`handoff_event`, alimenta el inbox)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, event: HandoffEvent) -> HandoffEvent:
        self.session.add(event)
        await self.session.flush()
        await self.session.refresh(event)
        return event

    async def get_latest_reason(self, thread_id: str) -> str | None:
        """Motivo del handoff más reciente del hilo (None si no hubo handoff)."""
        result = await self.session.execute(
            select(HandoffEvent.reason)
            .where(HandoffEvent.thread_id == thread_id)
            .order_by(HandoffEvent.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def latest_reasons(self, thread_ids: list[str]) -> dict[str, str]:
        """Motivo del handoff más reciente por hilo, para un lote de hilos (read-model del
        tablero, #94). Un solo query: subconsulta con el `created_at` máximo por hilo + join.
        Hilos sin handoff no aparecen en el dict."""
        if not thread_ids:
            return {}
        latest = (
            select(
                HandoffEvent.thread_id.label("thread_id"),
                func.max(HandoffEvent.created_at).label("max_created"),
            )
            .where(HandoffEvent.thread_id.in_(thread_ids))
            .group_by(HandoffEvent.thread_id)
            .subquery()
        )
        result = await self.session.execute(
            select(HandoffEvent.thread_id, HandoffEvent.reason).join(
                latest,
                (HandoffEvent.thread_id == latest.c.thread_id)
                & (HandoffEvent.created_at == latest.c.max_created),
            )
        )
        return dict(result.tuples().all())
