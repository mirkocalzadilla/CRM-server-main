"""M-Config (ABM users/roles): GET /users con rol + PUT /users/{id}/role.

Gestión de usuarios = platform_operator **o** client_admin (§RBAC #200,
`require_user_manager`), tenant-scoped al tenant activo del JWT. Guardas de la
matriz RBAC: un client_admin no toca operadores, nadie se auto-elimina / se
cambia el rol / se auto-desactiva, y ≥1 platform_operator siempre.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.core.domain.models import TenantUserRole
from server.modules.core.domain.schemas import TenantCreate, UserCreate
from server.modules.core.services.membership_service import MembershipService
from server.modules.core.services.tenant_service import TenantService
from server.modules.core.services.user_service import UserService
from server.shared.exceptions import BadRequestException

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


async def _seed_operator_member(
    session_factory: SessionFactory, email: str, slug: str = TENANT_SLUG
) -> str:
    """Miembro del tenant que además es platform_operator (is_superuser)."""
    async with session_factory() as session:
        user = await UserService(session).create(
            UserCreate(email=email, password="operator-secret1", full_name="Op")
        )
        user.is_superuser = True
        tenant = await TenantService(session).get_by_slug(slug)
        await MembershipService(session).assign(
            tenant_id=tenant.id, user_id=user.id, role=TenantUserRole.CLIENT_ADMIN
        )
        await session.commit()
        return str(user.id)


async def test_list_users_returns_tenant_roster_with_roles(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    await _seed_staff_member(session_factory)

    response = await client.get(f"{API}/users", headers=_auth(token))
    assert response.status_code == 200, response.text
    roles = {entry["email"]: entry["role"] for entry in response.json()}
    assert roles == {OPERATOR_EMAIL: "client_admin", STAFF_EMAIL: "staff"}


async def test_change_role_persists(client: AsyncClient, session_factory: SessionFactory) -> None:
    token = await _operator_token(client, session_factory)
    staff_id = await _seed_staff_member(session_factory)

    changed = await client.put(
        f"{API}/users/{staff_id}/role", json={"role": "client_admin"}, headers=_auth(token)
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["role"] == "client_admin"

    listed = await client.get(f"{API}/users", headers=_auth(token))
    roles = {entry["email"]: entry["role"] for entry in listed.json()}
    assert roles[STAFF_EMAIL] == "client_admin"

    # Vuelta atrás (client_admin ↔ staff es bidireccional).
    reverted = await client.put(
        f"{API}/users/{staff_id}/role", json={"role": "staff"}, headers=_auth(token)
    )
    assert reverted.status_code == 200
    assert reverted.json()["role"] == "staff"

    # Usuario que no pertenece al tenant → 404.
    outsider = await client.put(
        f"{API}/users/{uuid.uuid4()}/role", json={"role": "staff"}, headers=_auth(token)
    )
    assert outsider.status_code == 404


async def test_staff_gets_403_on_users_abm(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """`staff` no gestiona usuarios (403 en las 4 operaciones)."""
    await _register(client, OPERATOR_EMAIL, OPERATOR_PASSWORD, TENANT_SLUG)
    staff_id = await _seed_staff_member(session_factory)
    staff_token = await _login(client, STAFF_EMAIL, STAFF_PASSWORD)

    headers = _auth(staff_token)
    assert (await client.get(f"{API}/users", headers=headers)).status_code == 403
    put_role = await client.put(
        f"{API}/users/{staff_id}/role", json={"role": "client_admin"}, headers=headers
    )
    assert put_role.status_code == 403
    post = await client.post(
        f"{API}/users",
        json={"email": "x@example.com", "password": "newsecret1", "role": "staff"},
        headers=headers,
    )
    assert post.status_code == 403
    delete = await client.delete(f"{API}/users/{staff_id}", headers=headers)
    assert delete.status_code == 403


async def test_client_admin_manages_own_staff(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Un client_admin (no operador) gestiona el staff de su propio tenant (#200)."""
    admin_token = await _register(client, OPERATOR_EMAIL, OPERATOR_PASSWORD, TENANT_SLUG)
    staff_id = await _seed_staff_member(session_factory)
    headers = _auth(admin_token)

    # Lista, alta, cambio de rol y baja: todo permitido en su tenant.
    listed = await client.get(f"{API}/users", headers=headers)
    assert listed.status_code == 200, listed.text

    created = await client.post(
        f"{API}/users",
        json={"email": "recluta@example.com", "password": "newsecret1", "role": "staff"},
        headers=headers,
    )
    assert created.status_code == 201, created.text

    promoted = await client.put(
        f"{API}/users/{staff_id}/role", json={"role": "client_admin"}, headers=headers
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["role"] == "client_admin"

    deleted = await client.delete(f"{API}/users/{staff_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text


async def test_client_admin_cannot_touch_platform_operator(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Un client_admin no puede cambiar el rol ni eliminar a un platform_operator."""
    admin_token = await _register(client, OPERATOR_EMAIL, OPERATOR_PASSWORD, TENANT_SLUG)
    operator_id = await _seed_operator_member(session_factory, "boss@example.com")
    headers = _auth(admin_token)

    put_role = await client.put(
        f"{API}/users/{operator_id}/role", json={"role": "staff"}, headers=headers
    )
    assert put_role.status_code == 403, put_role.text

    deleted = await client.delete(f"{API}/users/{operator_id}", headers=headers)
    assert deleted.status_code == 403, deleted.text


async def test_cannot_change_own_role(client: AsyncClient, session_factory: SessionFactory) -> None:
    """Anti-self-lockout: nadie cambia su propio rol (400)."""
    admin_token = await _register(client, OPERATOR_EMAIL, OPERATOR_PASSWORD, TENANT_SLUG)
    me = await client.get(f"{API}/users/me", headers=_auth(admin_token))
    my_id = me.json()["id"]

    changed = await client.put(
        f"{API}/users/{my_id}/role", json={"role": "staff"}, headers=_auth(admin_token)
    )
    assert changed.status_code == 400, changed.text


async def test_patch_me_cannot_self_deactivate(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Anti-self-lockout: `PATCH /me` no permite auto-desactivarse (400)."""
    admin_token = await _register(client, OPERATOR_EMAIL, OPERATOR_PASSWORD, TENANT_SLUG)
    resp = await client.patch(
        f"{API}/users/me", json={"is_active": False}, headers=_auth(admin_token)
    )
    assert resp.status_code == 400, resp.text


async def test_create_user_in_tenant_and_login(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)

    created = await client.post(
        f"{API}/users",
        json={
            "email": "nuevo@example.com",
            "password": "newsecret1",
            "full_name": "Nuevo",
            "role": "staff",
        },
        headers=_auth(token),
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["email"] == "nuevo@example.com"
    assert body["role"] == "staff"

    listed = await client.get(f"{API}/users", headers=_auth(token))
    emails = {entry["email"] for entry in listed.json()}
    assert "nuevo@example.com" in emails

    # El usuario creado puede autenticarse (queda como miembro del tenant).
    login = await client.post(
        f"{API}/auth/login", json={"email": "nuevo@example.com", "password": "newsecret1"}
    )
    assert login.status_code == 200, login.text


async def test_create_user_duplicate_email_returns_422(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    payload = {"email": "dup@example.com", "password": "newsecret1", "role": "staff"}
    first = await client.post(f"{API}/users", json=payload, headers=_auth(token))
    assert first.status_code == 201, first.text
    second = await client.post(f"{API}/users", json=payload, headers=_auth(token))
    assert second.status_code == 422


async def test_delete_user_removes_from_roster(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    staff_id = await _seed_staff_member(session_factory)

    deleted = await client.delete(f"{API}/users/{staff_id}", headers=_auth(token))
    assert deleted.status_code == 204, deleted.text

    listed = await client.get(f"{API}/users", headers=_auth(token))
    emails = {entry["email"] for entry in listed.json()}
    assert STAFF_EMAIL not in emails


async def test_operator_cannot_delete_self(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    me = await client.get(f"{API}/users/me", headers=_auth(token))
    my_id = me.json()["id"]

    deleted = await client.delete(f"{API}/users/{my_id}", headers=_auth(token))
    assert deleted.status_code == 400


async def test_cannot_delete_last_platform_operator(session_factory: SessionFactory) -> None:
    """Invariante: nunca dejar el sistema sin platform_operator (a nivel servicio)."""
    async with session_factory() as session:
        operator = await UserService(session).create(
            UserCreate(email="solo@example.com", password="newsecret1", full_name="Solo")
        )
        operator.is_superuser = True
        await session.commit()

        with pytest.raises(BadRequestException):
            await UserService(session).remove_from_tenant(
                tenant_id=uuid.uuid4(),
                user_id=operator.id,
                acting_user_id=uuid.uuid4(),
                actor_is_operator=True,
            )


async def test_delete_multitenant_user_keeps_other_membership(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Borrar a un usuario multi-tenant quita solo la membresía del tenant activo.

    El `User` global sobrevive porque conserva la membresía del otro tenant.
    """
    admin_token = await _register(client, OPERATOR_EMAIL, OPERATOR_PASSWORD, TENANT_SLUG)
    staff_id = await _seed_staff_member(session_factory)

    # El staff también pertenece a un segundo tenant (globex).
    async with session_factory() as session:
        other = await TenantService(session).create(TenantCreate(name="Globex", slug="globex"))
        await MembershipService(session).assign(
            tenant_id=other.id, user_id=uuid.UUID(staff_id), role=TenantUserRole.STAFF
        )
        await session.commit()

    deleted = await client.delete(f"{API}/users/{staff_id}", headers=_auth(admin_token))
    assert deleted.status_code == 204, deleted.text

    # Fuera del roster de acme, pero el User global sigue vivo (miembro de globex).
    async with session_factory() as session:
        survivor = await UserService(session).get_by_email(STAFF_EMAIL)
        assert survivor is not None
        remaining = await MembershipService(session).list_for_user(survivor.id)
        assert {m.tenant_id for m in remaining} == {other.id}
