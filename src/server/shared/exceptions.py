from fastapi import HTTPException, status


class DomainException(Exception):  # noqa: N818
    def __init__(self, message: str):
        self.message = message
        super().__init__(self.message)


class NotFoundException(DomainException):
    pass


class UnauthorizedException(DomainException):
    pass


class ForbiddenException(DomainException):
    pass


class ValidationException(DomainException):
    pass


class BadRequestException(DomainException):
    """Petición bien formada pero rechazada por su contenido (ej. contraseña
    actual incorrecta). Mapea a 400 (default del handler de dominio)."""


class OutsideWindowError(ValidationException):
    """Free-form WhatsApp message attempted outside the 24h customer service window."""


class WonRequiresNameError(ValidationException):
    """Move a un stage 'won' sin nombre del lead: se rechaza para no crear
    contactos sin nombre (#241). Mapea a 422 como toda ValidationException."""


class ExternalServiceError(DomainException):
    """Fallo al llamar a un servicio externo (ej. Meta/WhatsApp). Mapea a 502."""


class CredentialsException(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
