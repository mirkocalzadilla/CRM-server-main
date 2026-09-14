"""Export CSV para recontacto manual (#176). Dos poblaciones (`scope`):

- `leads` (default): el tablero completo — cards/conversaciones con teléfono, tengan o
  no contacto registrado. Los fríos ("preguntas") casi nunca tienen contacto (el
  contacto nace al ganar o por alta manual) y son justo el target del recontacto.
- `contacts`: solo la tabla `contact` (cerraron un servicio o alta manual), enriquecida
  con los datos de su conversación más reciente cuando existe.

Dedup por teléfono: la conversación más reciente representa al lead (calificación,
etapa, servicios, última actividad); `fecha_alta` es el primer contacto. Filtro
opcional por calificación (hot/medium/cold, #96). Tenant-scoped por `organization_id`.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime
from typing import NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.models import Contact
from server.modules.agent.repositories.contact_repository import ContactRepository
from server.modules.crm.domain.lead_identity import resolve_lead_title
from server.modules.crm.domain.lead_rating import rating_for_stage
from server.modules.crm.repositories.board_repository import BoardRepository, ExportCardRow
from server.modules.crm.repositories.card_service_repository import CardServiceRepository
from server.shared.timezone import to_business_time

CSV_HEADERS = (
    "nombre",
    "telefono",
    "calificacion",
    "etapa",
    "servicios",
    "fecha_alta",
    "ultima_actividad",
)

# (phone, card/conversación más reciente si existe, contacto si el scope es 'contacts')
_Entry = tuple[str, ExportCardRow | None, Contact | None]


class ExportRow(NamedTuple):
    full_name: str
    phone: str
    rating: str
    stage_name: str
    services: str
    first_contact_at: datetime
    last_activity_at: datetime


class ContactExportService:
    def __init__(self, session: AsyncSession) -> None:
        self._board = BoardRepository(session)
        self._card_services = CardServiceRepository(session)
        self._contacts = ContactRepository(session)

    async def export_rows(
        self, organization_id: uuid.UUID, rating: str | None, scope: str = "leads"
    ) -> list[ExportRow]:
        """Una fila por teléfono; `rating` filtra (None = todos)."""
        cards = await self._board.list_cards_for_export(organization_id)
        latest: dict[str, ExportCardRow] = {}
        first_contact: dict[str, datetime] = {}
        for row in cards:  # asc por alta: la primera fija fecha_alta, la última pisa
            first_contact.setdefault(row.phone, row.created_at)
            latest[row.phone] = row

        entries: list[_Entry]
        if scope == "contacts":
            contacts = await self._contacts.list_for_org(organization_id)
            entries = [(c.phone, latest.get(c.phone), c) for c in contacts]
        else:
            entries = [(phone, row, None) for phone, row in latest.items()]

        rows = await self._build_rows(organization_id, entries, first_contact, rating)
        rows.sort(key=lambda r: (r.first_contact_at, r.phone))
        return rows

    async def _build_rows(
        self,
        organization_id: uuid.UUID,
        entries: list[_Entry],
        first_contact: dict[str, datetime],
        rating: str | None,
    ) -> list[ExportRow]:
        card_ids = [row.card_id for _, row, _ in entries if row is not None]
        with_service = await self._card_services.card_ids_with_service(card_ids, organization_id)
        service_names = await self._card_services.service_names_by_card(card_ids, organization_id)
        rows: list[ExportRow] = []
        for phone, row, contact in entries:
            lead_rating = rating_for_stage(
                row.funnel_stage if row is not None else None,
                accepted_service=row is not None and row.card_id in with_service,
            )
            if rating is not None and lead_rating != rating:
                continue
            # Scope 'contacts': manda el nombre curado del ABM; scope 'leads': la misma
            # precedencia que el tablero (conversación → contacto).
            if contact is not None:
                name = resolve_lead_title(contact.full_name, row.full_name if row else None)
                fallback_at = contact.created_at
            else:
                assert row is not None  # scope 'leads': toda entrada viene de una card
                name = resolve_lead_title(row.full_name, row.contact_name)
                fallback_at = row.created_at
            rows.append(
                ExportRow(
                    full_name=name,
                    phone=phone,
                    rating=lead_rating,
                    stage_name=row.stage_name if row is not None else "",
                    services="; ".join(service_names.get(row.card_id, [])) if row else "",
                    first_contact_at=first_contact.get(phone, fallback_at),
                    last_activity_at=row.updated_at if row is not None else fallback_at,
                )
            )
        return rows


def to_csv(rows: list[ExportRow]) -> str:
    """CSV con headers en español; fechas `YYYY-MM-DD HH:MM` (legibles en Excel)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_HEADERS)
    for row in rows:
        writer.writerow(
            (
                row.full_name,
                row.phone,
                row.rating,
                row.stage_name,
                row.services,
                to_business_time(row.first_contact_at).strftime("%Y-%m-%d %H:%M"),
                to_business_time(row.last_activity_at).strftime("%Y-%m-%d %H:%M"),
            )
        )
    return buffer.getvalue()
