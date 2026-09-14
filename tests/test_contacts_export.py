"""Export CSV de leads sobre `/crm/contacts/export` (#176).

Cubre: universo = tablero (todos los leads con teléfono, no solo la tabla `contact`),
filtro por calificación (`?rating=cold` para recontacto de fríos), dedup por teléfono
(la conversación más reciente representa al lead, `fecha_alta` = primer contacto),
resolución de nombre (conversación → contacto), servicios enlazados, RBAC (staff → 403)
y aislamiento multi-tenant.
"""

from __future__ import annotations

import codecs
import csv
import io
import uuid
from datetime import UTC, datetime

from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.agent.domain.catalog_models import Service
from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.modules.agent.domain.models import Agent, Contact, Conversation, Product
from server.modules.core.domain.models import TenantUserRole
from server.modules.core.domain.schemas import UserCreate
from server.modules.core.services.membership_service import MembershipService
from server.modules.core.services.tenant_service import TenantService
from server.modules.core.services.user_service import UserService
from server.modules.crm.domain.models import Card, CardService, Pipeline, Stage

API = "/api/v1"
EXPORT = f"{API}/crm/contacts/export"
STAFF_EMAIL = "staff@example.com"
STAFF_PASSWORD = "staff-secret"
BOM = codecs.BOM_UTF8.decode("utf-8")

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
    return str(response.json()["access_token"])


async def _org_id(session_factory: SessionFactory, slug: str) -> uuid.UUID:
    async with session_factory() as session:
        tenant = await TenantService(session).get_by_slug(slug)
        return tenant.id


async def _seed_stage(session_factory: SessionFactory, org_id: uuid.UUID) -> uuid.UUID:
    """Pipeline mínimo con un stage; devuelve el stage_id destino de las cards."""
    async with session_factory() as session:
        pipeline = Pipeline(organization_id=org_id, kind="ia", name="Gestión IA", position=0)
        session.add(pipeline)
        await session.flush()
        stage = Stage(pipeline_id=pipeline.id, name="Enganchando", position=0, status_code="open")
        session.add(stage)
        await session.commit()
        return stage.id


async def _seed_lead(
    session_factory: SessionFactory,
    org_id: uuid.UUID,
    stage_id: uuid.UUID,
    phone: str,
    funnel_stage: FunnelStage,
    *,
    full_name: str | None = None,
    created_at: datetime | None = None,
) -> uuid.UUID:
    """Conversación + card en el stage dado; devuelve el card_id."""
    async with session_factory() as session:
        conv = Conversation(
            instance_id=uuid.uuid4(),
            organization_id=org_id,
            external_id=phone,
            funnel_stage=funnel_stage,
            full_name=full_name,
        )
        if created_at is not None:
            conv.created_at = created_at
            conv.updated_at = created_at
        session.add(conv)
        await session.flush()
        card = Card(organization_id=org_id, conversation_id=conv.id, stage_id=stage_id, title="L")
        session.add(card)
        await session.commit()
        return card.id


def _rows(response: Response) -> list[dict[str, str]]:
    assert response.text.startswith(BOM)  # BOM para Excel
    return list(csv.DictReader(io.StringIO(response.text.lstrip(BOM))))


async def test_export_all_leads_csv(client: AsyncClient, session_factory: SessionFactory) -> None:
    h = _auth(await _register(client, "owner@acme.com", "secret-pass", "acme"))
    org = await _org_id(session_factory, "acme")
    stage = await _seed_stage(session_factory, org)
    await _seed_lead(
        session_factory,
        org,
        stage,
        "59170000001",
        FunnelStage.NEW,
        full_name="Frio",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    await _seed_lead(
        session_factory,
        org,
        stage,
        "59170000002",
        FunnelStage.QUALIFIED,
        full_name="Caliente",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )

    response = await client.get(EXPORT, headers=h)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert "leads_todos_" in response.headers["content-disposition"]
    rows = _rows(response)
    assert [(r["nombre"], r["telefono"], r["calificacion"]) for r in rows] == [
        ("Frio", "59170000001", "cold"),
        ("Caliente", "59170000002", "hot"),
    ]
    assert rows[0]["etapa"] == "Enganchando"
    assert rows[0]["fecha_alta"] == "2025-12-31 20:00"  # 00:00 UTC = 20:00 en Bolivia


async def test_export_cold_filter(client: AsyncClient, session_factory: SessionFactory) -> None:
    h = _auth(await _register(client, "owner@beta.com", "secret-pass", "beta"))
    org = await _org_id(session_factory, "beta")
    stage = await _seed_stage(session_factory, org)
    await _seed_lead(session_factory, org, stage, "59170000001", FunnelStage.DISQUALIFIED)
    await _seed_lead(session_factory, org, stage, "59170000002", FunnelStage.QUALIFIED)

    response = await client.get(EXPORT, params={"rating": "cold"}, headers=h)

    assert response.status_code == 200
    assert "leads_cold_" in response.headers["content-disposition"]
    rows = _rows(response)
    assert [(r["telefono"], r["calificacion"]) for r in rows] == [("59170000001", "cold")]


async def test_export_dedups_by_phone_latest_conversation_wins(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Un lead que volvió (#163) sale una sola vez: calificación de la conversación
    más reciente, `fecha_alta` de la primera."""
    h = _auth(await _register(client, "owner@gamma.com", "secret-pass", "gamma"))
    org = await _org_id(session_factory, "gamma")
    stage = await _seed_stage(session_factory, org)
    phone = "59170000009"
    await _seed_lead(
        session_factory,
        org,
        stage,
        phone,
        FunnelStage.DISQUALIFIED,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    await _seed_lead(
        session_factory,
        org,
        stage,
        phone,
        FunnelStage.QUALIFIED,
        created_at=datetime(2026, 3, 1, tzinfo=UTC),
    )

    rows = _rows(await client.get(EXPORT, headers=h))

    assert len(rows) == 1
    assert rows[0]["calificacion"] == "hot"
    assert rows[0]["fecha_alta"] == "2025-12-31 20:00"  # 00:00 UTC = 20:00 en Bolivia
    assert rows[0]["ultima_actividad"] == "2026-02-28 20:00"


async def test_export_resolves_contact_name_and_services(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Sin nombre en la conversación cae al contacto vinculado; los servicios enlazados
    salen por nombre y cuentan como `accepted_service` para la calificación (#206)."""
    h = _auth(await _register(client, "owner@delta.com", "secret-pass", "delta"))
    org = await _org_id(session_factory, "delta")
    stage = await _seed_stage(session_factory, org)
    card_id = await _seed_lead(session_factory, org, stage, "59170000005", FunnelStage.QUALIFYING)
    async with session_factory() as session:
        contact = Contact(organization_id=org, phone="59170000005", full_name="Ada")
        session.add(Product(slug="p1", display_name="p1"))
        session.add(contact)
        await session.flush()
        card = await session.get(Card, card_id)
        assert card is not None
        card.contact_id = contact.id
        agent = Agent(
            organization_id=org,
            product_slug="p1",
            display_name="A",
            system_prompt="s",
            model="m",
            tools=[],
            config={},
        )
        session.add(agent)
        await session.flush()
        service = Service(
            organization_id=org,
            agent_id=agent.id,
            slug="curso",
            nombre="Curso Trading",
            resumen="r",
            precio="100",
            moneda="USD",
        )
        session.add(service)
        await session.flush()
        session.add(
            CardService(
                organization_id=org, card_id=card_id, service_id=service.id, source="captured"
            )
        )
        await session.commit()

    rows = _rows(await client.get(EXPORT, headers=h))

    assert len(rows) == 1
    assert rows[0]["nombre"] == "Ada"
    assert rows[0]["servicios"] == "Curso Trading"
    assert rows[0]["calificacion"] == "hot"  # qualifying + servicio aceptado → hot


async def test_export_scope_contacts_only_registered(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """`scope=contacts`: solo la tabla de contactos (cerraron/alta manual), no todo el
    tablero. Un contacto con conversación sale enriquecido (etapa/actividad); uno
    manual sin card sale igual, con `fecha_alta` = alta del contacto."""
    h = _auth(await _register(client, "owner@zeta.com", "secret-pass", "zeta"))
    org = await _org_id(session_factory, "zeta")
    stage = await _seed_stage(session_factory, org)
    await _seed_lead(
        session_factory, org, stage, "59170000001", FunnelStage.NEW
    )  # lead sin contacto
    await _seed_lead(
        session_factory,
        org,
        stage,
        "59170000002",
        FunnelStage.QUALIFIED,
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    async with session_factory() as session:
        session.add(Contact(organization_id=org, phone="59170000002", full_name="Cerrado"))
        session.add(Contact(organization_id=org, phone="59170000003", full_name="Manual"))
        await session.commit()

    response = await client.get(EXPORT, params={"scope": "contacts"}, headers=h)

    assert "contactos_" in response.headers["content-disposition"]
    rows = _rows(response)
    by_phone = {r["telefono"]: r for r in rows}
    assert set(by_phone) == {"59170000002", "59170000003"}  # el lead suelto no sale
    assert by_phone["59170000002"]["nombre"] == "Cerrado"
    assert by_phone["59170000002"]["etapa"] == "Enganchando"
    assert by_phone["59170000002"]["fecha_alta"] == "2026-01-31 20:00"
    assert by_phone["59170000003"]["etapa"] == ""  # contacto manual sin card


async def test_export_invalid_scope_is_422(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    h = _auth(await _register(client, "owner@iota.com", "secret-pass", "iota"))

    response = await client.get(EXPORT, params={"scope": "everything"}, headers=h)

    assert response.status_code == 422


async def test_export_rbac_staff_forbidden(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    await _register(client, "owner@rbac.com", "secret-pass", "rbac")
    async with session_factory() as session:
        user = await UserService(session).create(
            UserCreate(email=STAFF_EMAIL, password=STAFF_PASSWORD, full_name="Staff")
        )
        tenant = await TenantService(session).get_by_slug("rbac")
        await MembershipService(session).assign(
            tenant_id=tenant.id, user_id=user.id, role=TenantUserRole.STAFF
        )
        await session.commit()
    login = await client.post(
        f"{API}/auth/login", json={"email": STAFF_EMAIL, "password": STAFF_PASSWORD}
    )
    assert login.status_code == 200

    response = await client.get(EXPORT, headers=_auth(str(login.json()["access_token"])))

    assert response.status_code == 403


async def test_export_invalid_rating_is_422(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    h = _auth(await _register(client, "owner@eps.com", "secret-pass", "eps"))

    response = await client.get(EXPORT, params={"rating": "warm"}, headers=h)

    assert response.status_code == 422


async def test_export_is_tenant_scoped(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token_a = await _register(client, "a@one.com", "secret-pass", "one")
    token_b = await _register(client, "b@two.com", "secret-pass", "two")
    org_a = await _org_id(session_factory, "one")
    stage_a = await _seed_stage(session_factory, org_a)
    await _seed_lead(session_factory, org_a, stage_a, "59171111111", FunnelStage.NEW)

    rows_a = _rows(await client.get(EXPORT, headers=_auth(token_a)))
    rows_b = _rows(await client.get(EXPORT, headers=_auth(token_b)))

    assert [r["telefono"] for r in rows_a] == ["59171111111"]
    assert rows_b == []  # sin fuga cross-tenant
