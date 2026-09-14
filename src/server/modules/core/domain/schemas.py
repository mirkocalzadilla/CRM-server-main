import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from server.modules.core.domain.models import TenantUserRole


class TenantBase(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    slug: str = Field(min_length=2, max_length=120, pattern=r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")


class TenantCreate(TenantBase):
    pass


class TenantUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=255)
    is_active: bool | None = None


class TenantRead(TenantBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    is_active: bool
    created_at: datetime
    updated_at: datetime


class UserBase(BaseModel):
    email: EmailStr
    full_name: str | None = Field(default=None, max_length=255)


class UserCreate(UserBase):
    password: str = Field(min_length=8, max_length=128)


class UserCreateInTenant(UserCreate):
    """Alta de usuario en el tenant activo (ABM de M-Config): suma el rol por-tenant."""

    role: TenantUserRole = TenantUserRole.STAFF


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, max_length=255)
    is_active: bool | None = None


class UserRead(UserBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    is_active: bool
    is_superuser: bool
    created_at: datetime
    updated_at: datetime


class UserWithRoleRead(UserRead):
    """Usuario del tenant activo con su rol de membresía (ABM de M-Config)."""

    role: TenantUserRole


class TenantUserCreate(BaseModel):
    user_id: uuid.UUID
    role: TenantUserRole = TenantUserRole.STAFF


class TenantUserUpdate(BaseModel):
    role: TenantUserRole


class TenantUserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    role: TenantUserRole
    created_at: datetime
    updated_at: datetime


class TenantUserDetailRead(TenantUserRead):
    user: UserRead
    tenant: TenantRead


class RegisterRequest(BaseModel):
    """Registro inicial: crea usuario, tenant y membresía client_admin en un solo paso."""

    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = Field(default=None, max_length=255)
    tenant_name: str = Field(min_length=2, max_length=255)
    tenant_slug: str = Field(min_length=2, max_length=120, pattern=r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class ChangePasswordRequest(BaseModel):
    """Cambio de contraseña del usuario autenticado: prueba la actual + define la nueva."""

    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class AuthenticatedUser(BaseModel):
    """Usuario autenticado + tenant activo + rol efectivo (RBAC 3 niveles).

    ``role`` es el rol por-tenant (client_admin/staff). ``is_platform_operator``
    es la dimensión global (``User.is_superuser``): si es True, el usuario opera
    cross-tenant y accede a config/users/agente, sin importar ``role``.
    """

    model_config = ConfigDict(from_attributes=True)

    user: UserRead
    tenant: TenantRead
    role: TenantUserRole
    is_platform_operator: bool = False


class MagicLinkRequest(BaseModel):
    """Solicitud de magic link: el sistema generará un token de un solo uso."""

    email: EmailStr


class MagicLinkConsume(BaseModel):
    """Canje del magic link: el cliente envía el token plano recibido por email."""

    token: str = Field(min_length=16, max_length=256)
