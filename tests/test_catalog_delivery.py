"""CR1 (server#268): modalidad, precio estructurado, links tipados y config de pagos.

Cubre los AC de la fase: defaults seguros (sin modalidad y sin monto no hay entrega
ni auto-validación), ABM de links con su RBAC, y la config de pagos por organización
con fallback al QR global. Reusa los helpers de `test_catalog.py` (mismo tenant/agente
seedeado) para no duplicar el andamiaje de auth.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import select

from server.config import get_settings
from server.modules.agent.domain.catalog_models import Service
from server.modules.agent.domain.models import Agent
from server.modules.crm.domain.payment_models import PaymentSettings

from .test_catalog import (
    API,
    STAFF_EMAIL,
    STAFF_PASSWORD,
    SessionFactory,
    _auth,
    _login,
    _operator_token,
    _seed_agent,
    _seed_staff_member,
    _service_payload,
)


async def _create_service(client: AsyncClient, token: str, **over: object) -> dict[str, object]:
    response = await client.post(
        f"{API}/catalog/services",
        json=_service_payload(**over),
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    return body


# --------------------------- modalidad + precio estructurado ---------------------------


async def test_service_defaults_have_no_modality_and_no_amount(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Default seguro: un servicio nuevo no entrega nada ni se auto-valida."""
    token = await _operator_token(client, session_factory)
    await _seed_agent(session_factory)
    created = await _create_service(client, token)
    assert created["modality"] is None
    assert created["price_amount"] is None


async def test_create_service_with_modality_and_amount(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    await _seed_agent(session_factory)
    created = await _create_service(client, token, modality="presencial", price_amount="650.00")
    assert created["modality"] == "presencial"
    assert Decimal(str(created["price_amount"])) == Decimal("650.00")

    async with session_factory() as session:
        service = (
            await session.execute(select(Service).where(Service.slug == "curso-edicion"))
        ).scalar_one()
        assert service.modality == "presencial"
        assert service.price_amount == Decimal("650.00")


async def test_update_service_modality_and_amount(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    await _seed_agent(session_factory)
    created = await _create_service(client, token)

    response = await client.put(
        f"{API}/services/{created['id']}",
        json={"modality": "virtual", "price_amount": "480.50"},
        headers=_auth(token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["modality"] == "virtual"
    assert Decimal(str(response.json()["price_amount"])) == Decimal("480.50")


async def test_update_can_clear_modality_and_amount(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Poder volver al default seguro importa: un servicio mal marcado debe poder
    dejar de entregar sin borrarlo del catálogo."""
    token = await _operator_token(client, session_factory)
    await _seed_agent(session_factory)
    created = await _create_service(client, token, modality="presencial", price_amount="650.00")

    response = await client.put(
        f"{API}/services/{created['id']}",
        json={"modality": None, "price_amount": None},
        headers=_auth(token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["modality"] is None
    assert response.json()["price_amount"] is None


async def test_invalid_modality_is_rejected(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """`híbrido` no existe como modalidad (decisión cerrada: presencial|virtual|NULL)."""
    token = await _operator_token(client, session_factory)
    await _seed_agent(session_factory)
    response = await client.post(
        f"{API}/catalog/services",
        json=_service_payload(modality="híbrido"),
        headers=_auth(token),
    )
    assert response.status_code == 422, response.text


async def test_non_positive_amount_is_rejected(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    await _seed_agent(session_factory)
    for bad in ("0", "-5", "100000.01"):
        response = await client.post(
            f"{API}/catalog/services",
            json=_service_payload(
                slug=f"s-{bad.replace('.', '-').replace('-', 'x')}", price_amount=bad
            ),
            headers=_auth(token),
        )
        assert response.status_code == 422, f"{bad}: {response.text}"


async def test_modality_and_amount_are_not_projected_to_agent_snapshot(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """El snapshot alimenta el contexto del LLM: el monto estructurado no tiene por
    qué estar ahí (el lead ya ve `precio`), y la modalidad tampoco."""
    token = await _operator_token(client, session_factory)
    agent_id = await _seed_agent(session_factory)
    await _create_service(client, token, modality="presencial", price_amount="650.00")

    async with session_factory() as session:
        agent = await session.get(Agent, uuid.UUID(agent_id))
        assert agent is not None
        services = agent.config["services"]
        assert isinstance(services, list) and services
        entry = services[0]
        assert isinstance(entry, dict)
        assert "modality" not in entry
        assert "price_amount" not in entry
        assert entry["precio"] == "650"  # el texto display sí viaja


# --------------------------- links tipados ---------------------------


async def test_service_links_abm(client: AsyncClient, session_factory: SessionFactory) -> None:
    token = await _operator_token(client, session_factory)
    await _seed_agent(session_factory)
    service = await _create_service(client, token, modality="virtual")
    service_id = service["id"]

    created = await client.post(
        f"{API}/services/{service_id}/links",
        json={
            "kind": "whatsapp_group",
            "url": "https://chat.whatsapp.com/ABC123",
            "label": "Grupo del curso",
        },
        headers=_auth(token),
    )
    assert created.status_code == 201, created.text
    link_id = created.json()["id"]
    assert created.json()["kind"] == "whatsapp_group"

    listed = await client.get(f"{API}/services/{service_id}/links", headers=_auth(token))
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    updated = await client.put(
        f"{API}/service-links/{link_id}",
        json={"kind": "meeting", "url": "https://meet.google.com/xyz-abcd-efg"},
        headers=_auth(token),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["kind"] == "meeting"
    assert updated.json()["label"] == "Grupo del curso"  # no se pisa lo no enviado

    deleted = await client.delete(f"{API}/service-links/{link_id}", headers=_auth(token))
    assert deleted.status_code == 204
    empty = await client.get(f"{API}/services/{service_id}/links", headers=_auth(token))
    assert empty.json() == []


async def test_link_url_must_be_http(client: AsyncClient, session_factory: SessionFactory) -> None:
    """Un link roto no se descubre en el ABM, se descubre cuando el lead lo recibe."""
    token = await _operator_token(client, session_factory)
    await _seed_agent(session_factory)
    service = await _create_service(client, token)
    for bad in ("chat.whatsapp.com/ABC", "javascript:alert(1)", "https://con espacio.com"):
        response = await client.post(
            f"{API}/services/{service['id']}/links",
            json={"kind": "other", "url": bad},
            headers=_auth(token),
        )
        assert response.status_code == 422, f"{bad}: {response.text}"


async def test_invalid_link_kind_is_rejected(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    await _seed_agent(session_factory)
    service = await _create_service(client, token)
    response = await client.post(
        f"{API}/services/{service['id']}/links",
        json={"kind": "telegram", "url": "https://t.me/x"},
        headers=_auth(token),
    )
    assert response.status_code == 422, response.text


async def test_links_capped_per_service(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    await _seed_agent(session_factory)
    service = await _create_service(client, token)
    for index in range(5):
        ok = await client.post(
            f"{API}/services/{service['id']}/links",
            json={"kind": "other", "url": f"https://example.com/{index}"},
            headers=_auth(token),
        )
        assert ok.status_code == 201, ok.text
    over = await client.post(
        f"{API}/services/{service['id']}/links",
        json={"kind": "other", "url": "https://example.com/6"},
        headers=_auth(token),
    )
    assert over.status_code == 422, over.text


async def test_links_of_unknown_service_are_404(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    await _seed_agent(session_factory)
    response = await client.get(f"{API}/services/{uuid.uuid4()}/links", headers=_auth(token))
    assert response.status_code == 404


async def test_staff_cannot_manage_links(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    await _seed_agent(session_factory)
    service = await _create_service(client, token)
    await _seed_staff_member(session_factory)
    staff_token = await _login(client, STAFF_EMAIL, STAFF_PASSWORD)

    response = await client.post(
        f"{API}/services/{service['id']}/links",
        json={"kind": "other", "url": "https://example.com"},
        headers=_auth(staff_token),
    )
    assert response.status_code == 403, response.text


# --------------------------- config de pagos por organización ---------------------------


async def test_payment_settings_default_falls_back_to_global_qr(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Sin fila de config, el QR es el global: producción venía funcionando así y no
    debe cambiar de comportamiento al aplicar la migración."""
    token = await _operator_token(client, session_factory)
    response = await client.get(f"{API}/crm/payment-settings", headers=_auth(token))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["expected_beneficiary"] is None
    assert body["payment_qr_url"] == get_settings().payment_qr_url
    assert body["is_qr_url_custom"] is False


async def test_payment_settings_update_and_read_back(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    response = await client.put(
        f"{API}/crm/payment-settings",
        json={
            "expected_beneficiary": "  Mirko   Calzadilla ",
            "payment_qr_url": "https://cdn.example.com/qr-mirko.png",
        },
        headers=_auth(token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["expected_beneficiary"] == "Mirko Calzadilla"  # espacios normalizados
    assert body["payment_qr_url"] == "https://cdn.example.com/qr-mirko.png"
    assert body["is_qr_url_custom"] is True

    again = await client.get(f"{API}/crm/payment-settings", headers=_auth(token))
    assert again.json()["expected_beneficiary"] == "Mirko Calzadilla"

    async with session_factory() as session:
        rows = (await session.execute(select(PaymentSettings))).scalars().all()
        assert len(rows) == 1  # upsert, no una fila por edición


async def test_payment_settings_clearing_qr_returns_to_global(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    await client.put(
        f"{API}/crm/payment-settings",
        json={"payment_qr_url": "https://cdn.example.com/qr.png"},
        headers=_auth(token),
    )
    cleared = await client.put(
        f"{API}/crm/payment-settings",
        json={"payment_qr_url": None},
        headers=_auth(token),
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["payment_qr_url"] == get_settings().payment_qr_url
    assert cleared.json()["is_qr_url_custom"] is False


async def test_payment_settings_rejects_non_http_qr(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    response = await client.put(
        f"{API}/crm/payment-settings",
        json={"payment_qr_url": "ftp://example.com/qr.png"},
        headers=_auth(token),
    )
    assert response.status_code == 422, response.text


async def test_staff_cannot_edit_payment_settings(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    await _operator_token(client, session_factory)
    await _seed_staff_member(session_factory)
    staff_token = await _login(client, STAFF_EMAIL, STAFF_PASSWORD)
    response = await client.put(
        f"{API}/crm/payment-settings",
        json={"expected_beneficiary": "Otro"},
        headers=_auth(staff_token),
    )
    assert response.status_code == 403, response.text
