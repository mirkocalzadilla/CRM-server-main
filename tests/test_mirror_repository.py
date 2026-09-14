"""MirrorRepository.app_history: aislamiento multi-tenant (FIX auditoría).

`session_id` es el teléfono del lead (external_id), único sólo dentro de una org;
dos orgs pueden compartir número. La query debe filtrar por `organization_id` para
no filtrar mensajes humanos cross-tenant al hilo espejo. Seed mínimo sobre SQLite.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.agent.domain.models import Agent, AppChatHistory, Product
from server.modules.crm.repositories.mirror_repository import MirrorRepository

SHARED_SESSION = "59170000000"  # mismo teléfono en dos orgs distintas


async def _seed_two_orgs(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID]:
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    async with session_factory() as session:
        session.add(Product(slug="cursos-mirko", display_name="Cursos Mirko"))
        await session.flush()
        for org, text in ((org_a, "soy de la org A"), (org_b, "soy de la org B")):
            agent = Agent(
                organization_id=org,
                product_slug="cursos-mirko",
                display_name="Asistente",
                system_prompt="x",
                model="claude-haiku-4-5-20251001",
            )
            session.add(agent)
            await session.flush()
            session.add(
                AppChatHistory(
                    agent_id=agent.id,
                    organization_id=org,
                    session_id=SHARED_SESSION,
                    sender="Mirko",
                    message=text,
                )
            )
        await session.commit()
    return org_a, org_b


async def test_app_history_is_scoped_to_org(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_a, org_b = await _seed_two_orgs(session_factory)
    async with session_factory() as session:
        repo = MirrorRepository(session)
        rows_a = await repo.app_history(SHARED_SESSION, org_a)
        rows_b = await repo.app_history(SHARED_SESSION, org_b)

    assert [r.message for r in rows_a] == ["soy de la org A"]
    assert [r.message for r in rows_b] == ["soy de la org B"]


async def test_app_history_since_excludes_previous_opportunity(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A returning lead opens a NEW conversation/opportunity (#163), but app_chat_histories
    has no conversation_id (keyed by phone). `since` (the new conversation's created_at)
    scopes the thread so the human messages of the previous closed opportunity don't leak."""
    org = uuid.uuid4()
    boundary = datetime(2026, 6, 28, 12, 0, tzinfo=UTC)  # new opportunity's created_at
    async with session_factory() as session:
        session.add(Product(slug="cursos-mirko", display_name="Cursos Mirko"))
        await session.flush()
        agent = Agent(
            organization_id=org,
            product_slug="cursos-mirko",
            display_name="Asistente",
            system_prompt="x",
            model="claude-haiku-4-5-20251001",
        )
        session.add(agent)
        await session.flush()
        for offset, text in (
            (-timedelta(hours=2), "oportunidad vieja"),
            (timedelta(hours=2), "oportunidad nueva"),
        ):
            session.add(
                AppChatHistory(
                    agent_id=agent.id,
                    organization_id=org,
                    session_id=SHARED_SESSION,
                    sender="Mirko",
                    message=text,
                    message_time=boundary + offset,
                )
            )
        await session.commit()

    async with session_factory() as session:
        repo = MirrorRepository(session)
        scoped = await repo.app_history(SHARED_SESSION, org, boundary)
        full = await repo.app_history(SHARED_SESSION, org)

    assert [r.message for r in scoped] == ["oportunidad nueva"]
    assert [r.message for r in full] == ["oportunidad vieja", "oportunidad nueva"]
