# ruff: noqa: E402
"""Fixtures comunes para la suite del módulo Core.

Levantamos una base SQLite in-memory (aiosqlite) con ``StaticPool`` para que
todas las sesiones — incluida la que inyecta FastAPI — vean la misma BD.

Notas de compatibilidad:

- ``sqlalchemy.dialects.postgresql.UUID`` hereda de ``sqlalchemy.types.Uuid``
  en SQLAlchemy 2.0+, por lo que con SQLite cae al fallback ``CHAR(32)``
  automáticamente. No hace falta workaround.
- ``Enum(TenantUserRole, name="tenant_user_role")`` se materializa en SQLite
  como ``VARCHAR`` + ``CHECK constraint``, no como un tipo ENUM nativo
  (que sólo existe en Postgres). Tests siguen siendo funcionalmente válidos.
- No usamos Alembic en tests: ``Base.metadata.create_all`` arma el esquema
  directamente. La migración Postgres con su ENUM real se prueba en runtime.
- Si el sistema tiene DATABASE_URL con scheme obsoleto ``postgres://``
  (deprecado en SQLAlchemy 2.x), se normaliza antes de que cualquier import
  del server dispare la creación del engine.
"""

from __future__ import annotations

import os as _os

# Normalise legacy postgres:// scheme before server imports trigger engine creation.
_db_url = _os.environ.get("DATABASE_URL", "")
if _db_url.startswith("postgres://"):
    _os.environ["DATABASE_URL"] = _db_url.replace("postgres://", "postgresql+asyncpg://", 1)

import itertools
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from server.main import create_app
from server.modules.agent.domain.models import AiChatHistory
from server.modules.core.domain import models as _core_models  # noqa: F401
from server.shared.base_model import Base
from server.shared.database import get_db_session
from server.shared.pubsub import Publisher

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

# `ai_chat_histories.message_order` es BIGSERIAL en Postgres (`FetchedValue`), pero
# SQLite solo autoincrementa una INTEGER PRIMARY KEY, así que cualquier código de
# producción que inserte un turno fallaba en los tests con NOT NULL. Por eso el repo
# venía delegando esa cobertura al e2e contra Postgres.
#
# Este listener asigna el orden en el cliente **solo en los tests**: un contador
# monótono por proceso, que es todo lo que la semántica de la columna necesita (orden
# relativo dentro del hilo). Producción sigue usando la secuencia de la base.
_test_message_order = itertools.count(1)


@event.listens_for(AiChatHistory, "before_insert")
def _assign_message_order(_mapper: Any, _connection: Any, target: AiChatHistory) -> None:
    if getattr(target, "message_order", None) is None:
        target.message_order = next(_test_message_order)


@pytest.fixture(autouse=True)
def _no_redis_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silencia el Pub/Sub del CRM en los tests.

    Los eventos realtime se publican a un Redis real, así que cualquier test que mueva
    una card **por HTTP** (donde el publisher es el singleton del módulo, no uno
    inyectable) fallaba con un error de resolución de nombre — un fallo confuso, muy
    lejos de lo que el test estaba probando. Los tests que instancian los servicios a
    mano ya pasan su propio publisher no-op; esto cubre el camino por HTTP.
    """

    async def _noop(self: Publisher, channel: str, payload: dict[str, object]) -> None:
        return None

    monkeypatch.setattr(Publisher, "publish", _noop)


@pytest_asyncio.fixture
async def db_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Motor SQLite in-memory compartido por todas las sesiones del test."""
    engine = create_async_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(
    db_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Factory de sesiones async ligada al motor de tests."""
    return async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )


@pytest_asyncio.fixture
async def db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    """Sesión directa para seed/inspección desde los tests."""
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient, None]:
    """Cliente HTTP ASGI con la dependencia ``get_db_session`` overrideada."""

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db_session] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()
