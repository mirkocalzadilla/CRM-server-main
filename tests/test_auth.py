"""Tests del flujo de autenticación: register → login → /auth/me."""

from __future__ import annotations

from httpx import AsyncClient

API = "/api/v1"


def _register_payload(
    email: str = "owner@example.com",
    password: str = "supersecret",
    tenant_slug: str = "acme",
) -> dict[str, str]:
    return {
        "email": email,
        "password": password,
        "full_name": "Owner User",
        "tenant_name": "Acme Corp",
        "tenant_slug": tenant_slug,
    }


async def test_register_returns_token(client: AsyncClient) -> None:
    """POST /auth/register devuelve 201 con un access_token utilizable y setea cookie."""
    response = await client.post(f"{API}/auth/register", json=_register_payload())
    assert response.status_code == 201
    body = response.json()
    assert body["token_type"] == "bearer"
    assert isinstance(body["access_token"], str)
    assert len(body["access_token"]) > 20
    assert body["expires_in"] > 0

    # Verificar cookie
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) > 0
    cookie_str = cookies[0]
    assert "access_token=" in cookie_str
    assert "Domain=.mirkocalzadilla.com" in cookie_str
    assert "Secure" in cookie_str
    assert "SameSite=lax" in cookie_str
    assert "HttpOnly" in cookie_str


async def test_login_after_register_succeeds(client: AsyncClient) -> None:
    """Tras registrarse, el usuario puede hacer login con sus credenciales y recibe cookie."""
    await client.post(f"{API}/auth/register", json=_register_payload())
    response = await client.post(
        f"{API}/auth/login",
        json={"email": "owner@example.com", "password": "supersecret"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert isinstance(body["access_token"], str)

    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) > 0
    cookie_str = cookies[0]
    assert "access_token=" in cookie_str
    assert "Domain=.mirkocalzadilla.com" in cookie_str
    assert "Secure" in cookie_str
    assert "SameSite=lax" in cookie_str
    assert "HttpOnly" in cookie_str


async def test_me_returns_user_tenant_and_client_admin_role(client: AsyncClient) -> None:
    """GET /auth/me con token válido entrega user + tenant + role=client_admin."""
    register = await client.post(f"{API}/auth/register", json=_register_payload())
    token = register.json()["access_token"]
    response = await client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["email"] == "owner@example.com"
    assert body["tenant"]["slug"] == "acme"
    assert body["role"] == "client_admin"
    assert body["is_platform_operator"] is False


async def test_me_with_cookie_returns_user_tenant(client: AsyncClient) -> None:
    """GET /auth/me con cookie válido autentica correctamente."""
    register = await client.post(f"{API}/auth/register", json=_register_payload())
    token = register.json()["access_token"]

    response = await client.get(f"{API}/auth/me", cookies={"access_token": token})
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["email"] == "owner@example.com"
    assert body["tenant"]["slug"] == "acme"


async def test_login_with_wrong_password_returns_401(client: AsyncClient) -> None:
    """Credenciales inválidas no emiten token."""
    await client.post(f"{API}/auth/register", json=_register_payload())
    response = await client.post(
        f"{API}/auth/login",
        json={"email": "owner@example.com", "password": "wrong-password"},
    )
    assert response.status_code == 401
