"""API CRUD de Contactos (#101): ciclo completo, idempotencia por phone y aislamiento
multi-tenant sobre `/crm/contacts`."""

from __future__ import annotations

from httpx import AsyncClient

API = "/api/v1"


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


async def test_contact_crud_lifecycle(client: AsyncClient) -> None:
    h = _auth(await _register(client, "owner@acme.com", "secret-pass", "acme"))

    created = await client.post(
        f"{API}/crm/contacts", json={"phone": "59170000001", "full_name": "Ada"}, headers=h
    )
    assert created.status_code == 201, created.text
    cid = created.json()["id"]
    assert created.json()["phone"] == "59170000001"
    assert created.json()["full_name"] == "Ada"

    listed = await client.get(f"{API}/crm/contacts", headers=h)
    assert listed.status_code == 200
    assert [c["id"] for c in listed.json()] == [cid]

    got = await client.get(f"{API}/crm/contacts/{cid}", headers=h)
    assert got.status_code == 200
    assert got.json()["full_name"] == "Ada"

    patched = await client.patch(
        f"{API}/crm/contacts/{cid}", json={"full_name": "Ada Lovelace"}, headers=h
    )
    assert patched.status_code == 200
    assert patched.json()["full_name"] == "Ada Lovelace"

    deleted = await client.delete(f"{API}/crm/contacts/{cid}", headers=h)
    assert deleted.status_code == 204

    gone = await client.get(f"{API}/crm/contacts/{cid}", headers=h)
    assert gone.status_code == 404
    empty = await client.get(f"{API}/crm/contacts", headers=h)
    assert empty.json() == []


async def test_create_contact_is_idempotent_by_phone(client: AsyncClient) -> None:
    h = _auth(await _register(client, "owner@beta.com", "secret-pass", "beta"))

    a = await client.post(
        f"{API}/crm/contacts", json={"phone": "59170000009", "full_name": "Grace"}, headers=h
    )
    assert a.status_code == 201
    assert a.json()["full_name"] == "Grace"

    b = await client.post(
        f"{API}/crm/contacts",
        json={"phone": "59170000009", "full_name": "Grace Hopper"},
        headers=h,
    )
    assert b.status_code == 201
    assert b.json()["id"] == a.json()["id"]  # mismo contacto, sin duplicar
    assert b.json()["full_name"] == "Grace Hopper"

    listed = await client.get(f"{API}/crm/contacts", headers=h)
    assert len(listed.json()) == 1


async def test_contact_requires_full_name(client: AsyncClient) -> None:
    """No puede haber un contacto sin nombre: alta y edición manual lo exigen (422)."""
    h = _auth(await _register(client, "owner@nombre.com", "secret-pass", "nombre"))

    missing = await client.post(f"{API}/crm/contacts", json={"phone": "59170000010"}, headers=h)
    assert missing.status_code == 422
    blank = await client.post(
        f"{API}/crm/contacts", json={"phone": "59170000010", "full_name": "   "}, headers=h
    )
    assert blank.status_code == 422

    created = await client.post(
        f"{API}/crm/contacts", json={"phone": "59170000010", "full_name": "Ada"}, headers=h
    )
    assert created.status_code == 201
    cid = created.json()["id"]
    cleared = await client.patch(f"{API}/crm/contacts/{cid}", json={"full_name": ""}, headers=h)
    assert cleared.status_code == 422
    unset = await client.patch(f"{API}/crm/contacts/{cid}", json={}, headers=h)
    assert unset.status_code == 422


async def test_contacts_are_tenant_scoped(client: AsyncClient) -> None:
    token_a = await _register(client, "a@one.com", "secret-pass", "one")
    token_b = await _register(client, "b@two.com", "secret-pass", "two")

    created = await client.post(
        f"{API}/crm/contacts",
        json={"phone": "59171111111", "full_name": "Solo A"},
        headers=_auth(token_a),
    )
    cid = created.json()["id"]

    # B no ve ni accede al contacto de A: sin fuga cross-tenant.
    b_list = await client.get(f"{API}/crm/contacts", headers=_auth(token_b))
    assert b_list.json() == []
    b_get = await client.get(f"{API}/crm/contacts/{cid}", headers=_auth(token_b))
    assert b_get.status_code == 404
