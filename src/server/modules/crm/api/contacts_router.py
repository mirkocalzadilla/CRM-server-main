"""API CRUD de Contactos (#101). Montado bajo `/crm/contacts`. Las ops del CRM las
pueden hacer los 3 roles (igual que el resto del tablero); todo tenant-scoped por
`ctx.tenant.id`. El alta automática al cerrar (won) vive en `board_service.move_card`."""

from __future__ import annotations

import codecs
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status

from server.modules.agent.domain.models import Contact
from server.modules.core.api.deps import CurrentUser, DbSession, require_roles
from server.modules.core.domain.models import TenantUserRole
from server.modules.core.domain.schemas import AuthenticatedUser
from server.modules.crm.api.schemas import ContactCreate, ContactOut, ContactUpdate
from server.modules.crm.services.contact_export_service import ContactExportService, to_csv
from server.modules.crm.services.contact_service import ContactService
from server.shared.timezone import business_today

router = APIRouter(prefix="/contacts", tags=["crm-contacts"])

NOT_FOUND = "contacto no encontrado"

# Exportar la base de leads es extracción masiva de datos de clientes: client_admin +
# platform_operator; staff → 403 (misma matriz que el catálogo, #176).
ContactExporter = Annotated[AuthenticatedUser, Depends(require_roles(TenantUserRole.CLIENT_ADMIN))]


def _out(contact: Contact) -> ContactOut:
    return ContactOut(
        id=contact.id,
        phone=contact.phone,
        full_name=contact.full_name,
        created_at=contact.created_at,
    )


@router.get("", response_model=list[ContactOut])
async def list_contacts(ctx: CurrentUser, session: DbSession) -> list[ContactOut]:
    contacts = await ContactService(session).list_contacts(ctx.tenant.id)
    return [_out(c) for c in contacts]


@router.get("/export")
async def export_contacts(
    ctx: ContactExporter,
    session: DbSession,
    rating: Literal["hot", "medium", "cold"] | None = None,
    scope: Literal["leads", "contacts"] = "leads",
) -> Response:
    """Export CSV (#176). `scope=leads` (default): todo el tablero, filtrable por
    calificación (`?rating=cold` para recontactar fríos). `scope=contacts`: solo la
    tabla de contactos (ya cerraron un servicio o alta manual) — ver
    `ContactExportService`. Declarado antes de `/{contact_id}` para que "export" no
    matchee como UUID."""
    rows = await ContactExportService(session).export_rows(ctx.tenant.id, rating, scope)
    prefix = "contactos" if scope == "contacts" else f"leads_{rating or 'todos'}"
    filename = f"{prefix}_{business_today().strftime('%Y%m%d')}.csv"
    return Response(
        content=codecs.BOM_UTF8 + to_csv(rows).encode("utf-8"),  # BOM: Excel + acentos
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("", response_model=ContactOut, status_code=status.HTTP_201_CREATED)
async def create_contact(
    payload: ContactCreate, ctx: CurrentUser, session: DbSession
) -> ContactOut:
    """Alta manual idempotente: si ya existe un contacto con ese phone, lo devuelve
    (eventualmente con el nombre actualizado), sin duplicar."""
    contact = await ContactService(session).create(ctx.tenant.id, payload)
    await session.commit()
    return _out(contact)


@router.get("/{contact_id}", response_model=ContactOut)
async def get_contact(contact_id: uuid.UUID, ctx: CurrentUser, session: DbSession) -> ContactOut:
    contact = await ContactService(session).get(contact_id, ctx.tenant.id)
    if contact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
    return _out(contact)


@router.patch("/{contact_id}", response_model=ContactOut)
async def update_contact(
    contact_id: uuid.UUID, payload: ContactUpdate, ctx: CurrentUser, session: DbSession
) -> ContactOut:
    contact = await ContactService(session).update(contact_id, ctx.tenant.id, payload)
    if contact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
    await session.commit()
    return _out(contact)


@router.delete(
    "/{contact_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
)
async def delete_contact(contact_id: uuid.UUID, ctx: CurrentUser, session: DbSession) -> None:
    deleted = await ContactService(session).delete(contact_id, ctx.tenant.id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
    await session.commit()
