"""Tests de POST /auth/change-password + hardening de PATCH /users/me.

Verifica el contrato seguro de cambio de contraseña (spec
`docs/SPEC_change_password.md`): exige la contraseña actual, rechaza la nueva
débil, requiere auth; y confirma que `PATCH /users/me` ya no cambia la credencial.
"""

from __future__ import annotations

from httpx import AsyncClient

API = "/api/v1"

EMAIL = "user@example.com"
OLD_PASSWORD = "old-supersecret"
NEW_PASSWORD = "new-supersecret"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register(client: AsyncClient) -> str:
    response = await client.post(
        f"{API}/auth/register",
        json={
            "email": EMAIL,
            "password": OLD_PASSWORD,
            "full_name": "Test User",
            "tenant_name": "Acme",
            "tenant_slug": "acme",
        },
    )
    assert response.status_code == 201, response.text
    token: str = response.json()["access_token"]
    return token


async def _login(client: AsyncClient, password: str) -> int:
    response = await client.post(f"{API}/auth/login", json={"email": EMAIL, "password": password})
    return response.status_code


async def test_change_password_succeeds(client: AsyncClient) -> None:
    """Current correcta + new válida → 204; la nueva pasa a ser la credencial."""
    token = await _register(client)

    response = await client.post(
        f"{API}/auth/change-password",
        json={"current_password": OLD_PASSWORD, "new_password": NEW_PASSWORD},
        headers=_auth(token),
    )
    assert response.status_code == 204, response.text
    assert response.content == b""

    assert await _login(client, NEW_PASSWORD) == 200
    assert await _login(client, OLD_PASSWORD) == 401


async def test_change_password_wrong_current_returns_400(client: AsyncClient) -> None:
    """Current incorrecta → 400 y la contraseña no cambia."""
    token = await _register(client)

    response = await client.post(
        f"{API}/auth/change-password",
        json={"current_password": "definitely-wrong", "new_password": NEW_PASSWORD},
        headers=_auth(token),
    )
    assert response.status_code == 400, response.text

    assert await _login(client, OLD_PASSWORD) == 200
    assert await _login(client, NEW_PASSWORD) == 401


async def test_change_password_weak_new_returns_422(client: AsyncClient) -> None:
    """New con menos de 8 caracteres → 422 (validación Pydantic)."""
    token = await _register(client)

    response = await client.post(
        f"{API}/auth/change-password",
        json={"current_password": OLD_PASSWORD, "new_password": "short"},
        headers=_auth(token),
    )
    assert response.status_code == 422, response.text


async def test_change_password_without_auth_returns_401(client: AsyncClient) -> None:
    """Sin token → 401 (no se filtra a la lógica de cambio)."""
    response = await client.post(
        f"{API}/auth/change-password",
        json={"current_password": OLD_PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert response.status_code == 401, response.text


async def test_patch_me_no_longer_changes_password(client: AsyncClient) -> None:
    """Hardening: PATCH /users/me con `password` se ignora; la credencial no cambia."""
    token = await _register(client)

    response = await client.patch(
        f"{API}/users/me",
        json={"full_name": "Renamed", "password": NEW_PASSWORD},
        headers=_auth(token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["full_name"] == "Renamed"

    # El password sigue siendo el viejo: el campo del body fue ignorado.
    assert await _login(client, OLD_PASSWORD) == 200
    assert await _login(client, NEW_PASSWORD) == 401
