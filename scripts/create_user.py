"""Crea un usuario en el tenant Mirko con rol por-tenant configurable.

Sin --superuser => is_superuser=False => 403 en config del agente (/agents).
Con --superuser => platform_operator (ve todo, incluida la config del agente).
Idempotente por email.

Uso:
    uv run python scripts/create_user.py --email mirko@... --password '...' \
        --name 'Mirko' [--role client_admin|staff] [--superuser]
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.core.domain.models import TenantUser, TenantUserRole, User
from server.modules.core.repositories.tenant_repository import TenantRepository
from server.shared.database import async_session_maker, dispose_engine
from server.shared.logger import configure_logging, get_logger
from server.shared.security import get_password_hash

logger = get_logger("create_user")

TENANT_SLUG = "mirko"


async def _create_user(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    full_name: str,
    role: TenantUserRole,
    is_superuser: bool,
) -> User:
    repo = TenantRepository(session)
    tenant = await repo.get_by_slug(TENANT_SLUG)
    if tenant is None:
        raise SystemExit(f"Tenant '{TENANT_SLUG}' no existe. Corré seed_mirko.py primero.")

    user = await session.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(
            email=email,
            full_name=full_name,
            hashed_password=get_password_hash(password),
            is_active=True,
        )
        session.add(user)
        await session.flush()
    user.is_superuser = is_superuser  # platform_operator => acceso a config del agente

    membership = await session.scalar(
        select(TenantUser).where(TenantUser.tenant_id == tenant.id, TenantUser.user_id == user.id)
    )
    if membership is None:
        session.add(TenantUser(tenant_id=tenant.id, user_id=user.id, role=role))
    else:
        membership.role = role
    await session.flush()
    logger.info("create_user.done", email=email, role=role.value, is_superuser=is_superuser)
    return user


async def run(args: argparse.Namespace) -> None:
    async with async_session_maker() as session:
        await _create_user(
            session,
            email=args.email,
            password=args.password,
            full_name=args.name,
            role=TenantUserRole(args.role),
            is_superuser=args.superuser,
        )
        await session.commit()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crear usuario no-superadmin en tenant Mirko")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument(
        "--role",
        default=TenantUserRole.CLIENT_ADMIN.value,
        choices=[r.value for r in TenantUserRole],
    )
    parser.add_argument(
        "--superuser",
        action="store_true",
        help="platform_operator: ve todo, incluida la config del agente",
    )
    return parser.parse_args()


async def main() -> None:
    configure_logging()
    try:
        await run(_parse_args())
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
