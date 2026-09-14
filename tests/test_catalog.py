"""Catálogo Fase 1 (SPEC_admin_catalogo_kb): CRUD de services, subida de material,
auto-publish y RBAC client_admin (+ platform_operator).

Cubre los DoD §10: CRUD + upload + sanitización + rechazo no-PDF/oversize, el snapshot
se re-proyecta solo tras cada cambio (#109, sin botón ni versión), aislamiento por
tenant. El catálogo es del tenant (endpoints /catalog/*, el agente se resuelve por
detrás). Categorías ABM (#106).
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.agent.domain.catalog_models import Asset, Service
from server.modules.agent.domain.models import Agent, AgentVersion, Product
from server.modules.agent.services import asset_service
from server.modules.agent.services.asset_service import AssetService, sanitize_filename
from server.modules.core.domain.models import TenantUserRole
from server.modules.core.domain.schemas import UserCreate
from server.modules.core.services.membership_service import MembershipService
from server.modules.core.services.tenant_service import TenantService
from server.modules.core.services.user_service import UserService
from server.shared.exceptions import ValidationException

API = "/api/v1"
OPERATOR_EMAIL = "operator@example.com"
OPERATOR_PASSWORD = "operator-secret"
STAFF_EMAIL = "staff@example.com"
STAFF_PASSWORD = "staff-secret"
TENANT_SLUG = "acme"
PDF_BYTES = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
JPG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 16
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 16

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


async def _login(client: AsyncClient, email: str, password: str) -> str:
    response = await client.post(f"{API}/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


async def _operator_token(client: AsyncClient, session_factory: SessionFactory) -> str:
    token = await _register(client, OPERATOR_EMAIL, OPERATOR_PASSWORD, TENANT_SLUG)
    async with session_factory() as session:
        user = await UserService(session).get_by_email(OPERATOR_EMAIL)
        assert user is not None
        user.is_superuser = True
        await session.commit()
    return token


async def _seed_staff_member(session_factory: SessionFactory, slug: str = TENANT_SLUG) -> None:
    async with session_factory() as session:
        user = await UserService(session).create(
            UserCreate(email=STAFF_EMAIL, password=STAFF_PASSWORD, full_name="Staff")
        )
        tenant = await TenantService(session).get_by_slug(slug)
        await MembershipService(session).assign(
            tenant_id=tenant.id, user_id=user.id, role=TenantUserRole.STAFF
        )
        await session.commit()


async def _seed_agent(
    session_factory: SessionFactory, slug: str = TENANT_SLUG, *, product_slug: str = "cursos-mirko"
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
            config={"temperature": 0.3},
        )
        session.add(agent)
        await session.commit()
        return str(agent.id)


def _service_payload(slug: str = "curso-edicion", **over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "slug": slug,
        "nombre": "Curso de Creación de Contenido",
        "resumen": "Curso híbrido en 4 módulos.",
        "precio": "650",
        "moneda": "BOB",
        "flujo_cierre": "pago_qr",
        "orden": 1,
    }
    base.update(over)
    return base


async def test_create_and_list_service(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    await _seed_agent(session_factory)

    created = await client.post(
        f"{API}/catalog/services", json=_service_payload(), headers=_auth(token)
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["slug"] == "curso-edicion"
    assert body["is_active"] is True

    listed = await client.get(f"{API}/catalog/services", headers=_auth(token))
    assert listed.status_code == 200
    assert [o["slug"] for o in listed.json()] == ["curso-edicion"]


async def test_update_and_soft_delete(client: AsyncClient, session_factory: SessionFactory) -> None:
    token = await _operator_token(client, session_factory)
    await _seed_agent(session_factory)
    service_id = (
        await client.post(f"{API}/catalog/services", json=_service_payload(), headers=_auth(token))
    ).json()["id"]

    updated = await client.put(
        f"{API}/services/{service_id}",
        json={"precio": "700", "flujo_cierre": "handoff_consultivo"},
        headers=_auth(token),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["precio"] == "700"
    assert updated.json()["flujo_cierre"] == "handoff_consultivo"

    # Despublicar (is_active=False) NO es borrar: el servicio sigue listado.
    await client.put(
        f"{API}/services/{service_id}", json={"is_active": False}, headers=_auth(token)
    )
    listed = await client.get(f"{API}/catalog/services", headers=_auth(token))
    assert listed.json()[0]["is_active"] is False

    # Borrar = baja lógica (deleted_at): desaparece del catálogo del operador,
    # pero la fila persiste para el historial del contacto (#99) / card_service.
    deleted = await client.delete(f"{API}/services/{service_id}", headers=_auth(token))
    assert deleted.status_code == 204
    listed = await client.get(f"{API}/catalog/services", headers=_auth(token))
    assert listed.json() == []

    # La fila no se borra físicamente: queda con `deleted_at` para el histórico.
    async with session_factory() as session:
        row = await session.get(Service, uuid.UUID(service_id))
        assert row is not None and row.deleted_at is not None


async def test_duplicate_slug_and_unknown_category(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    await _seed_agent(session_factory)

    first = await client.post(
        f"{API}/catalog/services", json=_service_payload(), headers=_auth(token)
    )
    assert first.status_code == 201
    dup = await client.post(
        f"{API}/catalog/services", json=_service_payload(), headers=_auth(token)
    )
    assert dup.status_code == 422  # slug duplicado por agente

    bad = await client.post(
        f"{API}/catalog/services",
        json=_service_payload(slug="otra", category_id=str(uuid.uuid4())),
        headers=_auth(token),
    )
    assert bad.status_code == 422  # category_id inexistente en la org


@pytest.mark.parametrize(
    "over",
    [
        {"nombre": "x" * 51},  # nombre máx. 50
        {"slug": "a" * 41},  # slug máx. 40
        {"resumen": "x" * 201},  # resumen máx. 200
        {"detalle": "x" * 301},  # detalle máx. 300
        {"precio": "650 Bs"},  # precio: solo números
        {"precio": "100000.01"},  # precio máx. 100000.00
        {"precio": "12.345"},  # precio máx. 2 decimales
        {"precio": "-5"},  # precio sin signo
    ],
)
async def test_create_rejects_invalid_fields(
    client: AsyncClient, session_factory: SessionFactory, over: dict[str, object]
) -> None:
    token = await _operator_token(client, session_factory)
    await _seed_agent(session_factory)

    response = await client.post(
        f"{API}/catalog/services",
        json=_service_payload(**over),
        headers=_auth(token),
    )
    assert response.status_code == 422, response.text


async def test_create_accepts_field_limits(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    await _seed_agent(session_factory)

    response = await client.post(
        f"{API}/catalog/services",
        json=_service_payload(
            slug="a" * 40,
            nombre="x" * 50,
            resumen="x" * 200,
            detalle="x" * 300,
            precio="100000.00",
        ),
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    assert response.json()["precio"] == "100000.00"


async def test_update_rejects_invalid_precio(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    await _seed_agent(session_factory)
    service_id = (
        await client.post(f"{API}/catalog/services", json=_service_payload(), headers=_auth(token))
    ).json()["id"]

    bad = await client.put(
        f"{API}/services/{service_id}", json={"precio": "gratis"}, headers=_auth(token)
    )
    assert bad.status_code == 422, bad.text


async def _upload(client: AsyncClient, token: str, name: str, data: bytes, mime: str) -> str:
    response = await client.post(
        f"{API}/assets", files={"file": (name, data, mime)}, headers=_auth(token)
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def test_upload_pdf_sanitizes_and_attaches_to_category(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    await _seed_agent(session_factory)

    upload = await client.post(
        f"{API}/assets",
        files={"file": ("Curso de Creación… Mirko.pdf", PDF_BYTES, "application/pdf")},
        headers=_auth(token),
    )
    assert upload.status_code == 201, upload.text
    asset = upload.json()
    assert asset["filename"].isascii() and " " not in asset["filename"]
    assert asset["filename"].endswith(".pdf")
    assert asset["public_url"].endswith(".pdf")
    assert asset["bytes"] == len(PDF_BYTES)

    category = await _create_category(client, token, "Cursos")
    attached = await client.put(
        f"{API}/service-categories/{category['id']}",
        json={"asset_ids": [asset["id"]]},
        headers=_auth(token),
    )
    assert attached.status_code == 200, attached.text
    assert [m["public_url"] for m in attached.json()["materials"]] == [asset["public_url"]]


async def test_upload_accepts_jpg_and_png(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    await _seed_agent(session_factory)

    jpg = await client.post(
        f"{API}/assets",
        files={"file": ("foto.jpg", JPG_BYTES, "image/jpeg")},
        headers=_auth(token),
    )
    assert jpg.status_code == 201, jpg.text
    assert jpg.json()["kind"] == "image"
    assert jpg.json()["filename"].endswith(".jpg")

    png = await client.post(
        f"{API}/assets",
        files={"file": ("captura.png", PNG_BYTES, "image/png")},
        headers=_auth(token),
    )
    assert png.status_code == 201, png.text
    assert png.json()["filename"].endswith(".png")


async def test_category_lists_multiple_materials_and_reconciles(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    await _seed_agent(session_factory)
    a1 = await _upload(client, token, "uno.pdf", PDF_BYTES, "application/pdf")
    a2 = await _upload(client, token, "dos.jpg", JPG_BYTES, "image/jpeg")
    a3 = await _upload(client, token, "tres.png", PNG_BYTES, "image/png")

    category = await _create_category(client, token, "Cursos", asset_ids=[a1, a2, a3])
    category_id = category["id"]
    assert {m["id"] for m in category["materials"]} == {a1, a2, a3}

    # Quitar uno: el material desvinculado deja de listarse (no se borra la fila).
    reduced = await client.put(
        f"{API}/service-categories/{category_id}", json={"asset_ids": [a1]}, headers=_auth(token)
    )
    assert {m["id"] for m in reduced.json()["materials"]} == {a1}

    # Omitir asset_ids no toca los materiales; mandar [] los quita todos.
    untouched = await client.put(
        f"{API}/service-categories/{category_id}", json={"orden": 5}, headers=_auth(token)
    )
    assert {m["id"] for m in untouched.json()["materials"]} == {a1}
    cleared = await client.put(
        f"{API}/service-categories/{category_id}", json={"asset_ids": []}, headers=_auth(token)
    )
    assert cleared.json()["materials"] == []


async def test_category_rejects_more_than_five_materials(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    ids = [
        await _upload(client, token, f"m{i}.pdf", PDF_BYTES, "application/pdf") for i in range(6)
    ]

    response = await client.post(
        f"{API}/service-categories",
        json={"nombre": "Cursos", "asset_ids": ids},
        headers=_auth(token),
    )
    assert response.status_code == 422, response.text  # máx. 5 materiales


async def test_upload_rejects_non_supported_type(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    await _seed_agent(session_factory)

    bad_mime = await client.post(
        f"{API}/assets",
        files={"file": ("nota.txt", b"hello", "text/plain")},
        headers=_auth(token),
    )
    assert bad_mime.status_code == 422
    bad_magic = await client.post(
        f"{API}/assets",
        files={"file": ("falso.pdf", b"not-a-pdf", "application/pdf")},
        headers=_auth(token),
    )
    assert bad_magic.status_code == 422


async def test_upload_rejects_oversize(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(asset_service, "MATERIAL_MAX_BYTES", 4)
    with pytest.raises(ValidationException, match="5 MB"):
        await AssetService(db_session).upload_material(
            organization_id=uuid.uuid4(),
            content_type="application/pdf",
            filename="big.pdf",
            data=PDF_BYTES,
        )


def test_sanitize_filename_strips_accents_and_spaces() -> None:
    assert sanitize_filename("Marcas Premium… edición.pdf").isascii()
    assert sanitize_filename("á é í.pdf") == "a-e-i.pdf"
    assert sanitize_filename("../../etc/passwd").endswith(".pdf")
    assert "/" not in sanitize_filename("a/b/c.pdf")


async def test_autopublish_keeps_snapshot_in_sync(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Sin botón de publicar (#109): cada cambio re-proyecta los activos al snapshot
    que lee el agente en vivo, y los cambios de catálogo NO versionan al agente."""
    token = await _operator_token(client, session_factory)
    agent_id = await _seed_agent(session_factory)

    async def snapshot() -> dict[str, list[dict[str, object]]]:
        async with session_factory() as session:
            agent = await session.get(Agent, uuid.UUID(agent_id))
            assert agent is not None
            return {
                "services": list(agent.config["services"]),
                "categories": list(agent.config["categories"]),
            }

    asset_id = await _upload(client, token, "portafolio.pdf", PDF_BYTES, "application/pdf")
    category = await _create_category(client, token, "Cursos", asset_ids=[asset_id])
    await client.post(
        f"{API}/catalog/services",
        json=_service_payload(slug="curso", category_id=category["id"]),
        headers=_auth(token),
    )
    marca_id = (
        await client.post(
            f"{API}/catalog/services",
            json=_service_payload(slug="marca", moneda="USD"),
            headers=_auth(token),
        )
    ).json()["id"]
    vieja_id = (
        await client.post(
            f"{API}/catalog/services",
            json=_service_payload(slug="vieja"),
            headers=_auth(token),
        )
    ).json()["id"]

    # Apenas creados (activos), los 3 ya están en el snapshot — sin publicar.
    assert {o["slug"] for o in (await snapshot())["services"]} == {"curso", "marca", "vieja"}

    # Despublicar (is_active=False) lo saca del snapshot al instante (#109).
    await client.put(f"{API}/services/{vieja_id}", json={"is_active": False}, headers=_auth(token))
    assert {o["slug"] for o in (await snapshot())["services"]} == {"curso", "marca"}

    # Borrar (baja lógica) también lo saca.
    await client.delete(f"{API}/services/{marca_id}", headers=_auth(token))
    final = await snapshot()
    assert {o["slug"] for o in final["services"]} == {"curso"}
    curso = next(o for o in final["services"] if o["slug"] == "curso")
    assert curso["categoria_slug"] == category["slug"]  # el servicio referencia su categoría
    # El material vive en la categoría del snapshot (#235), no en el servicio.
    cats = [c for c in final["categories"] if c["slug"] == category["slug"]]
    assert len(cats) == 1
    materials = cats[0]["materials"]
    assert isinstance(materials, list)
    assert materials[0]["url"].endswith(".pdf")

    async with session_factory() as session:
        agent = await session.get(Agent, uuid.UUID(agent_id))
        assert agent is not None
        assert agent.config["temperature"] == 0.3  # config previa preservada
        versions = (
            (await session.execute(select(AgentVersion).where(AgentVersion.agent_id == agent.id)))
            .scalars()
            .all()
        )
        assert versions == []  # los cambios de catálogo no versionan al agente (#109)


async def test_rbac_client_admin_allowed_staff_forbidden(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """El catálogo lo administra el client_admin; el staff → 403 (require_roles)."""
    admin_token = await _register(client, OPERATOR_EMAIL, OPERATOR_PASSWORD, TENANT_SLUG)
    await _seed_staff_member(session_factory)
    staff_token = await _login(client, STAFF_EMAIL, STAFF_PASSWORD)
    await _seed_agent(session_factory)

    # staff → 403 en toda la superficie del catálogo.
    hs = _auth(staff_token)
    assert (await client.get(f"{API}/catalog/services", headers=hs)).status_code == 403
    assert (
        await client.post(f"{API}/catalog/services", json=_service_payload(), headers=hs)
    ).status_code == 403
    assert (
        await client.post(
            f"{API}/assets",
            files={"file": ("x.pdf", PDF_BYTES, "application/pdf")},
            headers=hs,
        )
    ).status_code == 403

    # client_admin → administra el catálogo (sin ser platform_operator).
    ha = _auth(admin_token)
    created = await client.post(f"{API}/catalog/services", json=_service_payload(), headers=ha)
    assert created.status_code == 201, created.text
    listed = await client.get(f"{API}/catalog/services", headers=ha)
    assert listed.status_code == 200
    assert [s["slug"] for s in listed.json()] == ["curso-edicion"]


async def test_tenant_scoping_isolates_catalog(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """El catálogo se auto-scopea al tenant del caller: no ve ni edita el de otro."""
    token = await _operator_token(client, session_factory)
    await _seed_agent(session_factory)
    service_id = (
        await client.post(f"{API}/catalog/services", json=_service_payload(), headers=_auth(token))
    ).json()["id"]

    # Otro tenant, con su propio admin y agente.
    foreign_token = await _register(client, "otra-admin@example.com", "otra-secret", "otra")
    await _seed_agent(session_factory, "otra", product_slug="otro-producto")

    # El catálogo del otro tenant no ve el servicio ajeno.
    listed = await client.get(f"{API}/catalog/services", headers=_auth(foreign_token))
    assert listed.status_code == 200
    assert listed.json() == []

    # Ni puede editar/borrar el servicio ajeno por id → 404 (fuera de su org).
    assert (
        await client.put(
            f"{API}/services/{service_id}", json={"precio": "1"}, headers=_auth(foreign_token)
        )
    ).status_code == 404
    assert (
        await client.delete(f"{API}/services/{service_id}", headers=_auth(foreign_token))
    ).status_code == 404


# ---------- Categorías del catálogo (ABM, #106) ----------


async def _create_category(
    client: AsyncClient,
    token: str,
    nombre: str = "Capacitación personalizada",
    orden: int = 0,
    asset_ids: list[str] | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {"nombre": nombre, "orden": orden}
    if asset_ids is not None:
        body["asset_ids"] = asset_ids
    response = await client.post(
        f"{API}/service-categories",
        json=body,
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def test_category_crud(client: AsyncClient, session_factory: SessionFactory) -> None:
    token = await _operator_token(client, session_factory)

    created = await _create_category(client, token, "Producción audiovisual", 1)
    assert created["nombre"] == "Producción audiovisual"

    listed = await client.get(f"{API}/service-categories", headers=_auth(token))
    assert listed.status_code == 200
    assert [c["nombre"] for c in listed.json()] == ["Producción audiovisual"]

    updated = await client.put(
        f"{API}/service-categories/{created['id']}",
        json={"nombre": "Producción", "orden": 3},
        headers=_auth(token),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["nombre"] == "Producción"
    assert updated.json()["orden"] == 3

    deleted = await client.delete(f"{API}/service-categories/{created['id']}", headers=_auth(token))
    assert deleted.status_code == 204
    assert (await client.get(f"{API}/service-categories", headers=_auth(token))).json() == []
    # Borrar de nuevo la misma categoría → 404.
    assert (
        await client.delete(f"{API}/service-categories/{created['id']}", headers=_auth(token))
    ).status_code == 404


async def test_duplicate_category_nombre(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    await _create_category(client, token, "Paquetes de producción")
    dup = await client.post(
        f"{API}/service-categories",
        json={"nombre": "Paquetes de producción"},
        headers=_auth(token),
    )
    assert dup.status_code == 422


async def test_service_with_category_and_uncategorize_on_delete(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    await _seed_agent(session_factory)
    category = await _create_category(client, token, "Capacitación personalizada")

    created = await client.post(
        f"{API}/catalog/services",
        json=_service_payload(category_id=category["id"]),
        headers=_auth(token),
    )
    assert created.status_code == 201, created.text
    assert created.json()["category_id"] == category["id"]
    assert created.json()["category"]["nombre"] == "Capacitación personalizada"

    # Borrar la categoría deja el servicio sin categoría (no lo elimina).
    deleted = await client.delete(
        f"{API}/service-categories/{category['id']}", headers=_auth(token)
    )
    assert deleted.status_code == 204
    listed = await client.get(f"{API}/catalog/services", headers=_auth(token))
    assert len(listed.json()) == 1
    assert listed.json()[0]["category_id"] is None
    assert listed.json()[0]["category"] is None


async def test_category_rbac_client_admin_allowed_staff_forbidden(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    admin_token = await _register(client, OPERATOR_EMAIL, OPERATOR_PASSWORD, TENANT_SLUG)
    await _seed_staff_member(session_factory)
    staff_token = await _login(client, STAFF_EMAIL, STAFF_PASSWORD)

    # staff → 403.
    hs = _auth(staff_token)
    assert (await client.get(f"{API}/service-categories", headers=hs)).status_code == 403
    assert (
        await client.post(f"{API}/service-categories", json={"nombre": "x"}, headers=hs)
    ).status_code == 403

    # client_admin → administra categorías.
    created = await _create_category(client, admin_token, "Producción audiovisual")
    assert created["nombre"] == "Producción audiovisual"
    listed = await client.get(f"{API}/service-categories", headers=_auth(admin_token))
    assert [c["nombre"] for c in listed.json()] == ["Producción audiovisual"]


async def test_category_slug_generated_and_unique(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    # Nombres distintos que slugifican distinto.
    a = await _create_category(client, token, "Cursos & Talleres")
    assert a["slug"] == "cursos-talleres"
    # Un nombre que colisiona en slug con otro ya existente recibe sufijo.
    b = await _create_category(client, token, "Cursos, talleres!")
    assert b["slug"] == "cursos-talleres-2"


async def test_delete_category_removes_its_materials(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    asset_id = await _upload(client, token, "folleto.pdf", PDF_BYTES, "application/pdf")
    category = await _create_category(client, token, "Cursos", asset_ids=[asset_id])
    assert {m["id"] for m in category["materials"]} == {asset_id}

    deleted = await client.delete(
        f"{API}/service-categories/{category['id']}", headers=_auth(token)
    )
    assert deleted.status_code == 204
    # La fila `asset` de la categoría se borra junto con ella (#235).
    async with session_factory() as session:
        assert await session.get(Asset, uuid.UUID(asset_id)) is None
