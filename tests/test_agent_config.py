"""M-Config (agente): GET/PUT /agents/{id} versionado, platform_operator-only.

PUT → nueva `agent_version` (number incremental, snapshot completo, created_by)
+ `Agent.current_version_id` actualizado — verificado con query directa a la DB.
ABM de users/roles: ver tests/test_users_roles.py.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.agent.domain.models import Agent, AgentVersion, Product
from server.modules.core.domain.models import TenantUserRole
from server.modules.core.domain.schemas import UserCreate
from server.modules.core.services.membership_service import MembershipService
from server.modules.core.services.tenant_service import TenantService
from server.modules.core.services.user_service import UserService

API = "/api/v1"

OPERATOR_EMAIL = "operator@example.com"
OPERATOR_PASSWORD = "operator-secret"
STAFF_EMAIL = "staff@example.com"
STAFF_PASSWORD = "staff-secret"
TENANT_SLUG = "acme"

SessionFactory = async_sessionmaker[AsyncSession]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register(client: AsyncClient, email: str, password: str, slug: str) -> str:
    response = await client.post(
        f"{API}/auth/register",
        json={
            "email": email,
            "password": password,
            "full_name": "Test User",
            "tenant_name": slug.capitalize(),
            "tenant_slug": slug,
        },
    )
    assert response.status_code == 201, response.text
    token: str = response.json()["access_token"]
    return token


async def _login(client: AsyncClient, email: str, password: str) -> str:
    response = await client.post(f"{API}/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token


async def _operator_token(client: AsyncClient, session_factory: SessionFactory) -> str:
    """Registra al operador (client_admin de `acme`) y lo marca is_superuser."""
    token = await _register(client, OPERATOR_EMAIL, OPERATOR_PASSWORD, TENANT_SLUG)
    async with session_factory() as session:
        user = await UserService(session).get_by_email(OPERATOR_EMAIL)
        assert user is not None
        user.is_superuser = True
        await session.commit()
    return token


async def _seed_staff_member(session_factory: SessionFactory, slug: str = TENANT_SLUG) -> str:
    async with session_factory() as session:
        user = await UserService(session).create(
            UserCreate(email=STAFF_EMAIL, password=STAFF_PASSWORD, full_name="Staff")
        )
        tenant = await TenantService(session).get_by_slug(slug)
        await MembershipService(session).assign(
            tenant_id=tenant.id, user_id=user.id, role=TenantUserRole.STAFF
        )
        await session.commit()
        return str(user.id)


async def _seed_agent(
    session_factory: SessionFactory,
    slug: str = TENANT_SLUG,
    *,
    product_slug: str = "cursos-mirko",
) -> str:
    async with session_factory() as session:
        tenant = await TenantService(session).get_by_slug(slug)
        if await session.get(Product, product_slug) is None:
            session.add(Product(slug=product_slug, display_name=product_slug))
            await session.flush()
        agent = Agent(
            organization_id=tenant.id,
            product_slug=product_slug,
            display_name="Agente Mirko",
            system_prompt="Sos el asistente de Mirko.",
            model="claude-haiku-4-5-20251001",
            tools=[],
            config={"emojis": True},
        )
        session.add(agent)
        await session.commit()
        return str(agent.id)


async def test_operator_reads_agent_config(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    agent_id = await _seed_agent(session_factory)

    response = await client.get(f"{API}/agents/{agent_id}", headers=_auth(token))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["system_prompt"] == "Sos el asistente de Mirko."
    assert body["model"] == "claude-haiku-4-5-20251001"
    assert body["config"] == {"emojis": True}
    assert body["current_version"] is None  # el seed no versiona


async def test_operator_lists_agents_with_active_version(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    agent_id = await _seed_agent(session_factory)

    # Sin versiones: la lista trae el agente con current_version None.
    listed = await client.get(f"{API}/agents", headers=_auth(token))
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert [a["id"] for a in body] == [agent_id]
    assert body[0]["config"] == {"emojis": True}
    assert body[0]["current_version"] is None

    # Tras un PUT, la lista refleja la versión activa.
    await client.put(
        f"{API}/agents/{agent_id}",
        json={"model": "claude-sonnet-4-6", "change_summary": "x"},
        headers=_auth(token),
    )
    relisted = await client.get(f"{API}/agents", headers=_auth(token))
    assert relisted.json()[0]["current_version"]["version_number"] == 1

    # Scoping por tenant: el agente de otra org no aparece en la lista.
    await _register(client, "otra-admin@example.com", "otra-secret", "otra")
    await _seed_agent(session_factory, "otra", product_slug="otro-producto")
    scoped = await client.get(f"{API}/agents", headers=_auth(token))
    assert [a["id"] for a in scoped.json()] == [agent_id]


async def test_operator_lists_model_catalog(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)

    # /models se resuelve sin chocar con /{agent_id} (no se parsea como UUID).
    response = await client.get(f"{API}/agents/models", headers=_auth(token))
    assert response.status_code == 200, response.text
    body = response.json()
    ids = [m["id"] for m in body]
    assert "claude-haiku-4-5-20251001" in ids
    assert "gpt-5.4-mini-2026-03-17" in ids
    assert "gpt-5.4-2026-03-05" in ids

    gpt = next(m for m in body if m["id"] == "gpt-5.4-mini-2026-03-17")
    assert gpt["provider"] == "openai"
    assert gpt["reasoning"] is True
    assert gpt["reasoning_effort"] == "medium"
    assert gpt["pricing"] == {"input": 0.75, "cached_input": 0.08, "output": 4.5}

    haiku = next(m for m in body if m["id"] == "claude-haiku-4-5-20251001")
    assert haiku["provider"] == "anthropic"
    assert haiku["pricing"] is None


async def test_non_operator_gets_403_on_model_catalog(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    admin_token = await _register(client, OPERATOR_EMAIL, OPERATOR_PASSWORD, TENANT_SLUG)
    await _seed_staff_member(session_factory)
    staff_token = await _login(client, STAFF_EMAIL, STAFF_PASSWORD)

    for token in (admin_token, staff_token):
        assert (await client.get(f"{API}/agents/models", headers=_auth(token))).status_code == 403


async def test_non_operator_gets_403_on_agent_list(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    admin_token = await _register(client, OPERATOR_EMAIL, OPERATOR_PASSWORD, TENANT_SLUG)
    await _seed_staff_member(session_factory)
    staff_token = await _login(client, STAFF_EMAIL, STAFF_PASSWORD)

    for token in (admin_token, staff_token):
        assert (await client.get(f"{API}/agents", headers=_auth(token))).status_code == 403


async def test_put_versions_agent_and_persists(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    agent_id = await _seed_agent(session_factory)

    first = await client.put(
        f"{API}/agents/{agent_id}",
        json={
            "system_prompt": "Prompt editado",
            "config": {"emojis": False, "temperature": 0.3},
            "change_summary": "ajuste de tono",
        },
        headers=_auth(token),
    )
    assert first.status_code == 200, first.text
    assert first.json()["version_number"] == 1

    second = await client.put(
        f"{API}/agents/{agent_id}",
        json={"model": "claude-sonnet-4-6"},
        headers=_auth(token),
    )
    assert second.status_code == 200, second.text
    assert second.json()["version_number"] == 2

    # Verificación directa en DB (DoD): filas de agent_version + Agent actualizado.
    async with session_factory() as session:
        result = await session.execute(
            select(AgentVersion)
            .where(AgentVersion.agent_id == uuid.UUID(agent_id))
            .order_by(AgentVersion.version_number)
        )
        versions = list(result.scalars().all())
        assert [v.version_number for v in versions] == [1, 2]
        assert versions[0].system_prompt == "Prompt editado"
        assert versions[0].config == {"emojis": False, "temperature": 0.3}
        assert versions[0].change_summary == "ajuste de tono"
        assert versions[1].model == "claude-sonnet-4-6"
        assert versions[1].system_prompt == "Prompt editado"  # snapshot acumulado

        operator = await UserService(session).get_by_email(OPERATOR_EMAIL)
        assert operator is not None
        assert all(v.created_by == operator.id for v in versions)

        agent = await session.get(Agent, uuid.UUID(agent_id))
        assert agent is not None
        assert agent.current_version_id == versions[1].id
        assert agent.system_prompt == "Prompt editado"
        assert agent.model == "claude-sonnet-4-6"

    read = await client.get(f"{API}/agents/{agent_id}", headers=_auth(token))
    assert read.json()["current_version"]["version_number"] == 2


async def test_put_rejects_empty_changes_and_bad_temperature(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    agent_id = await _seed_agent(session_factory)

    empty = await client.put(f"{API}/agents/{agent_id}", json={}, headers=_auth(token))
    assert empty.status_code == 422

    for bad_temperature in (1.5, "alta"):
        response = await client.put(
            f"{API}/agents/{agent_id}",
            json={"config": {"temperature": bad_temperature}},
            headers=_auth(token),
        )
        assert response.status_code == 422

    # emojis es un switch booleano (#104): cualquier no-bool (string, número) se rechaza.
    for bad_emojis in ("alto", 1):
        response = await client.put(
            f"{API}/agents/{agent_id}",
            json={"config": {"emojis": bad_emojis}},
            headers=_auth(token),
        )
        assert response.status_code == 422


async def test_put_drops_manual_catalog_nodes(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    agent_id = await _seed_agent(session_factory)

    # ofertas/faq/resumen ya no se cargan a mano (vienen del Catálogo / deprecados, #105):
    # el update los descarta y solo persiste la config real.
    response = await client.put(
        f"{API}/agents/{agent_id}",
        json={
            "config": {
                "temperature": 0.5,
                "ofertas": {"cursos": "x"},
                "faq": {"ubicacion": "La Paz"},
                "resumen": "viejo",
            }
        },
        headers=_auth(token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["config"] == {"temperature": 0.5}


async def test_put_preserves_server_owned_services(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    agent_id = await _seed_agent(session_factory)

    # El catálogo (server-owned) ya dejó un snapshot en config.services.
    snapshot = [{"slug": "curso", "nombre": "Curso"}]
    async with session_factory() as session:
        agent = await session.get(Agent, uuid.UUID(agent_id))
        assert agent is not None
        agent.config = {**agent.config, "services": snapshot}
        await session.commit()

    # Un guardado del form (sin services, o intentando pisarlo) no toca el snapshot.
    response = await client.put(
        f"{API}/agents/{agent_id}",
        json={"config": {"temperature": 0.2, "services": [{"slug": "hack"}]}},
        headers=_auth(token),
    )
    assert response.status_code == 200, response.text
    config = response.json()["config"]
    assert config["services"] == snapshot  # preservado, no pisado por el form
    assert config["temperature"] == 0.2


async def test_put_updates_display_name(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    agent_id = await _seed_agent(session_factory)

    # display_name solo (sin otros cambios) es un cambio válido y versiona.
    response = await client.put(
        f"{API}/agents/{agent_id}",
        json={"display_name": "Servicios de producción audiovisual de Mirko"},
        headers=_auth(token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["version_number"] == 1

    read = await client.get(f"{API}/agents/{agent_id}", headers=_auth(token))
    assert read.json()["display_name"] == "Servicios de producción audiovisual de Mirko"


async def test_unknown_or_foreign_agent_is_404(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)

    missing = await client.get(f"{API}/agents/{uuid.uuid4()}", headers=_auth(token))
    assert missing.status_code == 404

    # Agente de otra org: el scoping por tenant activo lo oculta (404, no leak).
    await _register(client, "otra-admin@example.com", "otra-secret", "otra")
    foreign_id = await _seed_agent(session_factory, "otra", product_slug="otro-producto")
    assert (await client.get(f"{API}/agents/{foreign_id}", headers=_auth(token))).status_code == 404
    update = await client.put(
        f"{API}/agents/{foreign_id}", json={"model": "x"}, headers=_auth(token)
    )
    assert update.status_code == 404


async def test_non_operator_gets_403_on_agent_config(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    admin_token = await _register(client, OPERATOR_EMAIL, OPERATOR_PASSWORD, TENANT_SLUG)
    await _seed_staff_member(session_factory)
    staff_token = await _login(client, STAFF_EMAIL, STAFF_PASSWORD)
    agent_id = await _seed_agent(session_factory)

    for token in (admin_token, staff_token):
        headers = _auth(token)
        assert (await client.get(f"{API}/agents/{agent_id}", headers=headers)).status_code == 403
        put_agent = await client.put(
            f"{API}/agents/{agent_id}", json={"model": "x"}, headers=headers
        )
        assert put_agent.status_code == 403


async def test_put_preserves_every_server_owned_key_not_just_services(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Regresión: guardar el form borraba `categories` y `events` del contexto del bot.

    La proyección del catálogo escribe tres keys (`services`, `categories`, `events`), pero
    la lista de protegidas se había quedado con la primera: las otras dos se agregaron
    después (#235 materiales por categoría, #276 fechas de los cursos) y nadie volvió acá.
    El síntoma no es un error sino el bot diciendo que no tiene fechas, o no encontrando el
    material de una categoría, hasta que alguien vuelva a tocar el catálogo o la agenda.
    """
    token = await _operator_token(client, session_factory)
    agent_id = await _seed_agent(session_factory)

    services = [{"slug": "curso", "nombre": "Curso"}]
    categories = [{"slug": "edicion", "nombre": "Edición"}]
    events = [{"nombre": "Edición septiembre", "starts_at": "2026-09-10T19:00:00+00:00"}]
    async with session_factory() as session:
        agent = await session.get(Agent, uuid.UUID(agent_id))
        assert agent is not None
        agent.config = {
            **agent.config,
            "services": services,
            "categories": categories,
            "events": events,
        }
        await session.commit()

    # Un guardado del form que no las manda (es lo que hace el front real).
    response = await client.put(
        f"{API}/agents/{agent_id}",
        json={"config": {"temperature": 0.3}},
        headers=_auth(token),
    )

    assert response.status_code == 200, response.text
    config = response.json()["config"]
    assert config["services"] == services
    assert config["categories"] == categories
    assert config["events"] == events
    assert config["temperature"] == 0.3


async def test_put_cannot_overwrite_a_server_owned_key_with_a_stale_copy(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Las server-owned se toman de la base, no de lo que llegue.

    Si el form manda una copia, es la que leyó al abrir la pantalla: guardarla revertiría
    cualquier cambio del catálogo hecho en el medio.
    """
    token = await _operator_token(client, session_factory)
    agent_id = await _seed_agent(session_factory)

    current = [{"nombre": "Edición octubre"}]
    async with session_factory() as session:
        agent = await session.get(Agent, uuid.UUID(agent_id))
        assert agent is not None
        agent.config = {**agent.config, "events": current}
        await session.commit()

    response = await client.put(
        f"{API}/agents/{agent_id}",
        json={"config": {"events": [{"nombre": "copia vieja"}]}},
        headers=_auth(token),
    )

    assert response.status_code == 200, response.text
    assert response.json()["config"]["events"] == current
