from fastapi import APIRouter, Response, status

from server.modules.core.api.deps import CurrentUser, DbSession
from server.modules.core.domain.schemas import (
    AuthenticatedUser,
    ChangePasswordRequest,
    LoginRequest,
    MagicLinkConsume,
    MagicLinkRequest,
    RegisterRequest,
    TokenResponse,
)
from server.modules.core.services.auth_service import AuthService
from server.modules.core.services.magic_link_service import MagicLinkService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=status.HTTP_201_CREATED, response_model=TokenResponse)
async def register(
    payload: RegisterRequest, session: DbSession, response: Response
) -> TokenResponse:
    """Crea usuario, tenant y devuelve un JWT con rol client_admin."""
    service = AuthService(session)
    _user, _tenant, _membership, token = await service.register(payload)
    await session.commit()

    response.set_cookie(
        key="access_token",
        value=token.access_token,
        domain=".mirkocalzadilla.com",
        secure=True,
        samesite="lax",
        httponly=True,
    )
    return token


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, session: DbSession, response: Response) -> TokenResponse:
    """Login con email + password. Usa el primer tenant del usuario."""
    service = AuthService(session)
    _user, _tenant, _membership, token = await service.login(payload)

    response.set_cookie(
        key="access_token",
        value=token.access_token,
        domain=".mirkocalzadilla.com",
        secure=True,
        samesite="lax",
        httponly=True,
    )
    return token


@router.get("/me", response_model=AuthenticatedUser)
async def me(current: CurrentUser) -> AuthenticatedUser:
    """Devuelve el usuario y tenant activo del JWT."""
    return current


@router.post(
    "/change-password",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
)
async def change_password(
    payload: ChangePasswordRequest,
    session: DbSession,
    current: CurrentUser,
) -> None:
    """Cambia la contraseña del usuario autenticado.

    Verifica la contraseña actual (400 si no coincide). La nueva reusa la
    validación min 8 / max 128 (un valor débil → 422 antes de llegar al service).
    """
    await AuthService(session).change_password(
        current.user.id, payload.current_password, payload.new_password
    )
    await session.commit()


@router.post("/magic-link/request", status_code=status.HTTP_202_ACCEPTED)
async def request_magic_link(payload: MagicLinkRequest, session: DbSession) -> None:
    """Genera un token de un solo uso para el email indicado.

    Responde 202 sin revelar si el email existe; el token plano se loggea
    en esta fase. El envío real por email queda para la fase DevOps.
    """
    service = MagicLinkService(session)
    await service.request(payload.email)
    await session.commit()


@router.post("/magic-link/consume", response_model=TokenResponse)
async def consume_magic_link(
    payload: MagicLinkConsume, session: DbSession, response: Response
) -> TokenResponse:
    """Canjea un magic link y devuelve un JWT con (sub, tenant_id, role)."""
    service = MagicLinkService(session)
    _user, _tenant, _membership, token = await service.consume(payload.token)
    await session.commit()

    response.set_cookie(
        key="access_token",
        value=token.access_token,
        domain=".mirkocalzadilla.com",
        secure=True,
        samesite="lax",
        httponly=True,
    )
    return token
