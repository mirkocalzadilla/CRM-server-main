"""ABM de contactos (M-CRM). El alta principal es automática vía el hook de 'won'
(ver `board_service.move_card`); este servicio cubre el CRUD que consume la sección
de Contactos del CRM (#58 FE). Tenant-scoped por `organization_id`."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.models import Contact
from server.modules.agent.repositories.contact_repository import ContactRepository
from server.modules.crm.api.schemas import ContactCreate, ContactUpdate
from server.modules.crm.repositories.card_repository import CardRepository


class ContactService:
    """No commitea: la transacción la maneja el caller (el router en el CRUD)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._contacts = ContactRepository(session)
        self._cards = CardRepository(session)

    async def list_contacts(self, organization_id: uuid.UUID) -> list[Contact]:
        return await self._contacts.list_for_org(organization_id)

    async def get(self, contact_id: uuid.UUID, organization_id: uuid.UUID) -> Contact | None:
        return await self._contacts.get_by_id(contact_id, organization_id)

    async def create(self, organization_id: uuid.UUID, payload: ContactCreate) -> Contact:
        """Alta manual: upsert idempotente por phone (no duplica si ya existe) y linkea
        la(s) card(s) de la org con ese teléfono al contacto (FK, #139). El link reusa el
        upsert: no duplica el contacto y materializa el vínculo card↔contacto."""
        contact = await self._contacts.upsert(organization_id, payload.phone, payload.full_name)
        await self._cards.link_to_contact_by_phone(organization_id, payload.phone, contact.id)
        return contact

    async def update(
        self, contact_id: uuid.UUID, organization_id: uuid.UUID, payload: ContactUpdate
    ) -> Contact | None:
        contact = await self._contacts.get_by_id(contact_id, organization_id)
        if contact is None:
            return None
        return await self._contacts.update_full_name(contact, payload.full_name)

    async def delete(self, contact_id: uuid.UUID, organization_id: uuid.UUID) -> bool:
        contact = await self._contacts.get_by_id(contact_id, organization_id)
        if contact is None:
            return False
        await self._contacts.delete(contact)
        return True
