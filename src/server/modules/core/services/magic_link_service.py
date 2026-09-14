import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from server.config import get_settings
from server.modules.core.domain.models import (
    MagicLinkToken,
    Tenant,
    TenantUser,
    User,
)
from server.modules.core.domain.schemas import TokenResponse
from server.modules.core.repositories.magic_link_repository import MagicLinkRepository
from server.modules.core.services.tenant_service import TenantService
from server.modules.core.services.user_service import UserService
from server.shared.exceptions import UnauthorizedException
from server.shared.logger import get_logger
from server.shared.security import create_access_token

logger = get_logger(__name__)


def _hash_token(token: str) -> str:
    """Calcula el hash sha256 del token plano (hex). Lo único que se persiste."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class MagicLinkService:
    """Genera y canjea tokens de un solo uso para login passwordless."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = MagicLinkRepository(session)
        self.user_service = UserService(session)
        self.tenant_service = TenantService(session)

    async def request(self, email: str) -> str | None:
        """Genera un token de un solo uso y devuelve el valor plano.

        Por seguridad, no se revela si el email existe: si no hay usuario o
        está inactivo, se devuelve None y el llamador responde 202 igualmente.
        El token plano se loggea en esta fase; el envío real por email queda
        para la fase DevOps.
        """
        user = await self.user_service.get_by_email(email)
        if user is None or not user.is_active:
            logger.info("magic_link.request.skip", email=email)
            return None

        settings = get_settings()
        plain_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(minutes=settings.magic_link_expire_minutes)

        record = MagicLinkToken(
            token_hash=_hash_token(plain_token),
            user_id=user.id,
            expires_at=expires_at,
        )
        await self.repo.add(record)

        logger.info(
            "magic_link.request.issued",
            user_id=str(user.id),
            expires_at=expires_at.isoformat(),
            token=plain_token,
        )
        return plain_token

    async def consume(self, token: str) -> tuple[User, Tenant, TenantUser, TokenResponse]:
        """Canjea un magic link y emite un JWT como AuthService.login."""
        record = await self.repo.get_by_hash(_hash_token(token))
        if record is None:
            raise UnauthorizedException("Magic link inválido")
        if record.used_at is not None:
            raise UnauthorizedException("Magic link ya utilizado")
        if record.expires_at <= datetime.now(UTC):
            raise UnauthorizedException("Magic link expirado")

        user = record.user
        if not user.is_active:
            raise UnauthorizedException("Usuario inactivo")

        memberships = user.memberships
        if not memberships:
            raise UnauthorizedException("El usuario no pertenece a ningún tenant")

        primary = memberships[0]
        tenant = await self.tenant_service.get(primary.tenant_id)

        record.used_at = datetime.now(UTC)
        await self.repo.update(record)

        token_response = _issue_token(user, tenant, primary)
        return user, tenant, primary, token_response


def _issue_token(user: User, tenant: Tenant, membership: TenantUser) -> TokenResponse:
    """Emite un JWT con el mismo shape que AuthService.login (sub/tenant_id/role)."""
    settings = get_settings()
    expires = timedelta(minutes=settings.jwt_access_token_expire_minutes)
    access_token = create_access_token(
        data={
            "sub": str(user.id),
            "tenant_id": str(tenant.id),
            "role": membership.role.value,
            "is_superuser": user.is_superuser,
        },
        expires_delta=expires,
    )
    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=int(expires.total_seconds()),
    )
