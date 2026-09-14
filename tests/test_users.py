"""Tests del router /users: /me y restricciones de superuser."""

from __future__ import annotations

from httpx import AsyncClient

API = "/api/v1"


async def _register_and_token(client: AsyncClient) -> str:
    response = await client.post(
        f"{API}/auth/register",
        json={
            "email": "user@example.com",
            "password": "supersecret",
            "full_name": "Initial Name",
            "tenant_name": "Acme",
            "tenant_slug": "acme",
        },
    )
    assert response.status_code == 201, response.text
    token: str = response.json()["access_token"]
    return token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_get_me_returns_current_user(client: AsyncClient) -> None:
    """GET /users/me devuelve el perfil del usuario autenticado."""
    token = await _register_and_token(client)
    response = await client.get(f"{API}/users/me", headers=_auth(token))
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "user@example.com"
    assert body["full_name"] == "Initial Name"
    assert body["is_superuser"] is False


async def test_patch_me_updates_full_name(client: AsyncClient) -> None:
    """PATCH /users/me persiste el nuevo full_name."""
    token = await _register_and_token(client)
    response = await client.patch(
        f"{API}/users/me",
        json={"full_name": "Updated Name"},
        headers=_auth(token),
    )
    assert response.status_code == 200
    assert response.json()["full_name"] == "Updated Name"

    me = await client.get(f"{API}/users/me", headers=_auth(token))
    assert me.status_code == 200
    assert me.json()["full_name"] == "Updated Name"


async def test_client_admin_can_list_users(client: AsyncClient) -> None:
    """GET /users: gestión de usuarios = operador o client_admin (#200).

    El registro crea un `client_admin`, que ahora gestiona su propio staff y ve
    el roster de su tenant. (El 403 para `staff` vive en test_users_roles.py.)
    """
    token = await _register_and_token(client)
    response = await client.get(f"{API}/users", headers=_auth(token))
    assert response.status_code == 200, response.text
