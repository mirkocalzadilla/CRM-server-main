"""CLI administrativo de la plataforma.

Comandos para bootstrap del sistema sin pasar por la API pública:
``create-superuser`` y ``create-tenant``. Entry-point: ``marketing-cli``.
"""

from __future__ import annotations

import asyncio

import typer

from server.modules.core.domain.models import (
    Tenant,
    TenantUser,
    TenantUserRole,
    User,
)
from server.modules.core.repositories.tenant_repository import TenantRepository
from server.modules.core.repositories.tenant_user_repository import (
    TenantUserRepository,
)
from server.modules.core.repositories.user_repository import UserRepository
from server.shared.database import async_session_maker
from server.shared.security import get_password_hash

app = typer.Typer(
    help="CLI administrativo del backend Marketing Services.",
    no_args_is_help=True,
)


async def _create_superuser_async(email: str, password: str, full_name: str | None) -> User:
    """Crea un usuario marcado como superuser sin asociarlo a ningún tenant."""
    async with async_session_maker() as session:
        repo = UserRepository(session)
        if await repo.get_by_email(email) is not None:
            raise ValueError(f"Ya existe un usuario con email {email}")
        user = User(
            email=email.lower(),
            full_name=full_name,
            hashed_password=get_password_hash(password),
            is_active=True,
            is_superuser=True,
        )
        user = await repo.add(user)
        await session.commit()
        return user


@app.command("create-superuser")
def create_superuser(
    email: str = typer.Option(..., "--email", help="Email del superusuario."),
    password: str = typer.Option(
        ..., "--password", help="Password en texto plano (mínimo 8 caracteres)."
    ),
    full_name: str | None = typer.Option(None, "--full-name", help="Nombre completo (opcional)."),
) -> None:
    """Crea un superusuario global sin tenant asociado."""
    if len(password) < 8:
        typer.echo("Error: la password debe tener al menos 8 caracteres.", err=True)
        raise typer.Exit(code=1)
    try:
        user = asyncio.run(_create_superuser_async(email, password, full_name))
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Superusuario creado: {user.email} (id={user.id})")


async def _create_tenant_async(slug: str, name: str, owner_email: str) -> tuple[Tenant, User]:
    """Crea un tenant nuevo y asigna a un usuario existente como client_admin."""
    async with async_session_maker() as session:
        tenant_repo = TenantRepository(session)
        user_repo = UserRepository(session)
        membership_repo = TenantUserRepository(session)

        slug_norm = slug.lower()
        if await tenant_repo.get_by_slug(slug_norm) is not None:
            raise ValueError(f"Ya existe un tenant con slug '{slug_norm}'")
        owner = await user_repo.get_by_email(owner_email)
        if owner is None:
            raise ValueError(f"No existe un usuario con email {owner_email}")

        tenant = Tenant(name=name, slug=slug_norm)
        tenant = await tenant_repo.add(tenant)
        membership = TenantUser(
            tenant_id=tenant.id, user_id=owner.id, role=TenantUserRole.CLIENT_ADMIN
        )
        await membership_repo.add(membership)
        await session.commit()
        return tenant, owner


@app.command("create-tenant")
def create_tenant(
    slug: str = typer.Option(..., "--slug", help="Slug único del tenant."),
    name: str = typer.Option(..., "--name", help="Nombre descriptivo del tenant."),
    owner_email: str = typer.Option(
        ..., "--owner-email", help="Email del usuario que será client_admin."
    ),
) -> None:
    """Crea un tenant y asigna client_admin a un usuario existente."""
    try:
        tenant, owner = asyncio.run(_create_tenant_async(slug, name, owner_email))
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"Tenant '{tenant.slug}' creado (id={tenant.id}); client_admin asignado a {owner.email}."
    )


if __name__ == "__main__":
    app()
