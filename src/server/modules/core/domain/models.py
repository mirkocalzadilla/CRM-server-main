import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.shared.base_model import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    pass


class TenantUserRole(enum.StrEnum):
    """Roles por-tenant (RBAC 3 niveles, Opción A).

    ``platform_operator`` no vive acá: es la dimensión global ``User.is_superuser``.
    El rol efectivo lo resuelve el enforcement (deps.py).
    """

    CLIENT_ADMIN = "client_admin"
    STAFF = "staff"


class Tenant(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Organización/workspace raíz del modelo multi-tenant."""

    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    memberships: Mapped[list["TenantUser"]] = relationship(
        back_populates="tenant",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Usuario global. La pertenencia a un tenant se modela en TenantUser."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hashed_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_superuser: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    memberships: Mapped[list["TenantUser"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class TenantUser(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Tabla pivote que relaciona Users con Tenants y asigna un rol."""

    __tablename__ = "tenant_users"
    __table_args__ = (UniqueConstraint("tenant_id", "user_id", name="uq_tenant_users_tenant_user"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[TenantUserRole] = mapped_column(
        Enum(
            TenantUserRole,
            name="tenant_user_role",
            values_callable=lambda obj: [e.value for e in obj],
        ),
        nullable=False,
        default=TenantUserRole.STAFF,
    )

    tenant: Mapped[Tenant] = relationship(back_populates="memberships", lazy="joined")
    user: Mapped[User] = relationship(back_populates="memberships", lazy="joined")


class MagicLinkToken(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Token de un solo uso para login passwordless por email.

    Solo se persiste el hash sha256 del token. El valor plano se
    devuelve una sola vez al solicitarlo y nunca se almacena en DB.
    """

    __tablename__ = "magic_link_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(lazy="joined")
