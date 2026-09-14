"""RBAC 3 niveles: platform_operator (is_superuser) vs client_admin/staff.

- platform_operator: config/users + cross-tenant.
- client_admin/staff: NO config/users; sí ops (CRM) + leer su roster.
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.core.domain.models import TenantUserRole
from server.modules.core.domain.schemas import UserCreate
from server.modules.core.services.membership_service import MembershipService
from server.modules.core.services.tenant_service import TenantService
from server.modules.core.services.user_service import UserService

API = "/api/v1"

ADMIN_EMAIL = "client-admin@example.com"
ADMIN_PASSWORD = "client-admin-secret"
STAFF_EMAIL = "staff@example.com"
STAFF_PASSWORD = "staff-secret"
TENANT_SLUG = "acme"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register(client: AsyncClient, email: str, password: str, slug: str) -> str:
    """Registra un usuario+tenant (rol client_admin) y devuelve su token."""
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


async def _make_superuser(session_factory: async_sessionmaker[AsyncSession], email: str) -> None:
    """Marca a un usuario existente como platform_operator (is_superuser)."""
    async with session_factory() as session:
        user = await UserService(session).get_by_email(email)
        assert user is not None
        user.is_superuser = True
        await session.commit()


async def _seed_staff_member(session_factory: async_sessionmaker[AsyncSession], slug: str) -> str:
    """Crea un usuario staff y lo asocia al tenant `slug`. Devuelve su id."""
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


# --- client_admin: sin acceso a config/users ---


async def test_client_admin_cannot_mutate_tenant(client: AsyncClient) -> None:
    token = await _register(client, ADMIN_EMAIL, ADMIN_PASSWORD, TENANT_SLUG)
    response = await client.patch(
        f"{API}/tenants/me", json={"name": "Renamed"}, headers=_auth(token)
    )
    assert response.status_code == 403


async def test_client_admin_cannot_manage_members(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _register(client, ADMIN_EMAIL, ADMIN_PASSWORD, TENANT_SLUG)
    async with session_factory() as session:
        other = await UserService(session).create(
            UserCreate(email="other@example.com", password="other-secret")
        )
        await session.commit()
        other_id = str(other.id)
    response = await client.post(
        f"{API}/tenants/me/members",
        json={"user_id": other_id, "role": "staff"},
        headers=_auth(token),
    )
    assert response.status_code == 403


async def test_client_admin_cannot_list_tenants(client: AsyncClient) -> None:
    token = await _register(client, ADMIN_EMAIL, ADMIN_PASSWORD, TENANT_SLUG)
    response = await client.get(f"{API}/tenants", headers=_auth(token))
    assert response.status_code == 403


# --- platform_operator: acceso pleno ---


async def test_platform_operator_full_access(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _register(client, ADMIN_EMAIL, ADMIN_PASSWORD, TENANT_SLUG)
    await _make_superuser(session_factory, ADMIN_EMAIL)

    # El mismo token resuelve platform_operator (is_platform_operator se deriva de DB).
    patched = await client.patch(
        f"{API}/tenants/me", json={"name": "Renamed by operator"}, headers=_auth(token)
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "Renamed by operator"

    async with session_factory() as session:
        member = await UserService(session).create(
            UserCreate(email="new-member@example.com", password="member-secret")
        )
        await session.commit()
        member_id = str(member.id)

    added = await client.post(
        f"{API}/tenants/me/members",
        json={"user_id": member_id, "role": "staff"},
        headers=_auth(token),
    )
    assert added.status_code == 201, added.text

    listed = await client.get(f"{API}/tenants", headers=_auth(token))
    assert listed.status_code == 200


# --- staff: sin config; sí ops + roster ---


async def test_staff_cannot_mutate_tenant(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _register(client, ADMIN_EMAIL, ADMIN_PASSWORD, TENANT_SLUG)
    await _seed_staff_member(session_factory, TENANT_SLUG)
    staff_token = await _login(client, STAFF_EMAIL, STAFF_PASSWORD)
    response = await client.patch(
        f"{API}/tenants/me", json={"name": "Renamed by staff"}, headers=_auth(staff_token)
    )
    assert response.status_code == 403


async def test_client_admin_and_staff_can_read_roster(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin_token = await _register(client, ADMIN_EMAIL, ADMIN_PASSWORD, TENANT_SLUG)
    await _seed_staff_member(session_factory, TENANT_SLUG)
    staff_token = await _login(client, STAFF_EMAIL, STAFF_PASSWORD)

    for token in (admin_token, staff_token):
        roster = await client.get(f"{API}/tenants/me/members", headers=_auth(token))
        assert roster.status_code == 200, roster.text
        assert len(roster.json()) == 2  # client_admin + staff


# --- ops (CRM) abiertas a los 3 ---


async def test_crm_board_accessible_to_client_admin_and_staff(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin_token = await _register(client, ADMIN_EMAIL, ADMIN_PASSWORD, TENANT_SLUG)
    await _seed_staff_member(session_factory, TENANT_SLUG)
    staff_token = await _login(client, STAFF_EMAIL, STAFF_PASSWORD)

    for token in (admin_token, staff_token):
        board = await client.get(f"{API}/crm/boards", headers=_auth(token))
        assert board.status_code == 200, board.text
