from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from server.shared.exceptions import (
    DomainException,
    ExternalServiceError,
    ForbiddenException,
    NotFoundException,
    UnauthorizedException,
    ValidationException,
)
from server.shared.logger import get_logger

logger = get_logger("exception_handlers")


async def domain_exception_handler(request: Request, exc: DomainException) -> JSONResponse:
    status_code = 400
    if isinstance(exc, NotFoundException):
        status_code = 404
    elif isinstance(exc, UnauthorizedException):
        status_code = 401
    elif isinstance(exc, ForbiddenException):
        status_code = 403
    elif isinstance(exc, ValidationException):
        status_code = 422
    elif isinstance(exc, ExternalServiceError):
        # Lo que su docstring siempre dijo. Sin esta rama, un fallo de Meta salía 400
        # ("pediste algo mal") en cualquier endpoint que no pusiera su propio `except`
        # — los del CRM lo hacen, pero era una trampa para el próximo que se agregara.
        status_code = 502

    logger.warning(
        "domain_exception", status_code=status_code, detail=exc.message, path=request.url.path
    )

    return JSONResponse(
        status_code=status_code,
        content={"detail": exc.message},
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainException, domain_exception_handler)  # type: ignore[arg-type]
