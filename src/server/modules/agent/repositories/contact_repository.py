import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.models import Contact


class ContactRepository:
    """Acceso a `contact`, siempre tenant-scoped por `organization_id`.
    Un contacto por (organization_id, phone) — UNIQUE."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_phone(self, organization_id: uuid.UUID, phone: str) -> Contact | None:
        result = await self.session.execute(
            select(Contact).where(
                Contact.organization_id == organization_id,
                Contact.phone == phone,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, contact_id: uuid.UUID, organization_id: uuid.UUID) -> Contact | None:
        result = await self.session.execute(
            select(Contact).where(
                Contact.id == contact_id,
                Contact.organization_id == organization_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_for_org(self, organization_id: uuid.UUID) -> list[Contact]:
        """Contactos de la org, orden estable (created_at, id)."""
        result = await self.session.execute(
            select(Contact)
            .where(Contact.organization_id == organization_id)
            .order_by(Contact.created_at, Contact.id)
        )
        return list(result.scalars().all())

    async def upsert(
        self, organization_id: uuid.UUID, phone: str, full_name: str | None
    ) -> Contact:
        """Idempotente por (organization_id, phone). Si el contacto existe, actualiza
        `full_name` sólo cuando llega un valor nuevo no nulo (no pisa un nombre ya
        cargado con None). Si no existe, lo crea. Hace flush; el commit lo hace el caller.

        Portable (select-then-insert/update) en vez de `ON CONFLICT` de Postgres, para
        que corra igual sobre el SQLite de los tests."""
        existing = await self.get_by_phone(organization_id, phone)
        if existing is not None:
            if full_name is not None and full_name != existing.full_name:
                existing.full_name = full_name
                await self.session.flush()
            return existing
        contact = Contact(organization_id=organization_id, phone=phone, full_name=full_name)
        self.session.add(contact)
        await self.session.flush()
        await self.session.refresh(contact)
        return contact

    async def update_full_name(self, contact: Contact, full_name: str | None) -> Contact:
        contact.full_name = full_name
        await self.session.flush()
        return contact

    async def delete(self, contact: Contact) -> None:
        await self.session.delete(contact)
        await self.session.flush()
