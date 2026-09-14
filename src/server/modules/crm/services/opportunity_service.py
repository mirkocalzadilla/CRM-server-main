"""ABM de oportunidad (#97): alta manual, edición de detalles y baja de una card.

La card es proyección 1:1 de una `conversation`, así que el alta manual reusa la
última conversación del teléfono (o abre una nueva si no hay o la última está cerrada,
#163) y crea la card en el **primer stage** del pipeline IA. La edición toca el nombre
(`conversation.full_name` + `card.title`) y las notas. La baja cierra la conversación
(`closed_at`) y borra la card: el próximo inbound del lead abre una oportunidad nueva.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.models import Conversation
from server.modules.agent.repositories.agent_instance_repository import AgentInstanceRepository
from server.modules.agent.repositories.contact_repository import ContactRepository
from server.modules.agent.repositories.conversation_repository import ConversationRepository
from server.modules.crm.api.schemas import CardCreate, CardOut, CardUpdate
from server.modules.crm.domain.lead_identity import resolve_lead_title
from server.modules.crm.domain.lead_rating import rating_for_stage
from server.modules.crm.domain.models import Card, CardMove
from server.modules.crm.repositories.board_repository import BoardRepository
from server.modules.crm.repositories.card_repository import CardRepository
from server.modules.crm.repositories.card_service_repository import CardServiceRepository
from server.shared.pubsub import Publisher, card_moved_event, crm_channel

IA_KIND = "ia"


class OpportunityService:
    def __init__(self, *, session: AsyncSession, publisher: Publisher) -> None:
        self._session = session
        self._publisher = publisher
        self._conv = ConversationRepository(session)
        self._instances = AgentInstanceRepository(session)
        self._board = BoardRepository(session)
        self._cards = CardRepository(session)
        self._card_services = CardServiceRepository(session)
        self._contacts = ContactRepository(session)

    async def create(
        self, organization_id: uuid.UUID, payload: CardCreate, created_by: uuid.UUID
    ) -> CardOut:
        """Alta manual idempotente por teléfono. ValueError si la org no tiene agente
        o pipelines configurados (→ 400 en el router)."""
        instance = await self._instances.get_for_org(organization_id)
        if instance is None:
            raise ValueError("la organización no tiene un agente configurado")
        stage = await self._board.get_first_stage(organization_id, IA_KIND)
        if stage is None:
            raise ValueError("la organización no tiene pipelines configurados")

        conversation = await self._conv.get_latest_by_external_id(payload.phone, organization_id)
        # Sin conversación o con la última ya cerrada → nueva oportunidad (#163): el alta
        # manual sobre un lead cerrado abre un trato nuevo, no resucita el cerrado.
        if conversation is None or conversation.closed_at is not None:
            conversation = await self._conv.add(
                Conversation(
                    instance_id=instance.id,
                    organization_id=organization_id,
                    external_id=payload.phone,
                    full_name=payload.full_name,
                )
            )
        elif payload.full_name and not conversation.full_name:
            # Conversación abierta reusada sin nombre: el del alta manual es mejor que nada.
            conversation.full_name = payload.full_name

        card = await self._cards.get_by_conversation(conversation.id)
        if card is None:
            # Teléfono ya registrado como contacto → la card nace linkeada y con su nombre.
            contact = await self._contacts.get_by_phone(organization_id, payload.phone)
            card = await self._cards.add(
                Card(
                    organization_id=organization_id,
                    conversation_id=conversation.id,
                    stage_id=stage.id,
                    title=resolve_lead_title(
                        payload.full_name,
                        contact.full_name if contact is not None else None,
                        payload.phone,
                    ),
                    notes=payload.notes,
                    contact_id=contact.id if contact is not None else None,
                )
            )
            await self._cards.add_move(
                CardMove(
                    card_id=card.id,
                    stage_from_id=None,
                    stage_to_id=stage.id,
                    moved_by=str(created_by),
                )
            )
            await self._session.commit()
            await self._publisher.publish(
                crm_channel(organization_id),
                card_moved_event(
                    card_id=card.id,
                    stage=stage.name,
                    conversation_id=conversation.id,
                    pipeline_kind=IA_KIND,
                ),
            )

        with_service = await self._card_services.card_ids_with_service([card.id], organization_id)
        return CardOut(
            id=card.id,
            title=card.title,
            conversation_id=conversation.id,
            stage_id=card.stage_id,
            phone=conversation.external_id,
            rating=rating_for_stage(
                conversation.funnel_stage, accepted_service=card.id in with_service
            ),
            is_ai_active=conversation.is_ai_active,
            created_at=card.created_at,
        )

    async def update(
        self, card_id: uuid.UUID, organization_id: uuid.UUID, payload: CardUpdate
    ) -> CardOut | None:
        """Edita nombre y/o notas. Campos ausentes (exclude_unset) no se tocan. El
        nombre actualiza `conversation.full_name` y `card.title` (fallback al teléfono)."""
        card = await self._board.get_card(card_id, organization_id)
        if card is None:
            return None
        fields = payload.model_dump(exclude_unset=True)
        conversation = await self._conv.get_by_id(card.conversation_id, organization_id)
        if "notes" in fields:
            card.notes = payload.notes
        if "full_name" in fields:
            if conversation is not None:
                conversation.full_name = payload.full_name
            # Sin nombre propio, el título cae al del contacto vinculado antes que al teléfono.
            contact = (
                await self._contacts.get_by_id(card.contact_id, organization_id)
                if card.contact_id is not None
                else None
            )
            card.title = resolve_lead_title(
                payload.full_name,
                contact.full_name if contact is not None else None,
                conversation.external_id if conversation is not None else card.title,
            )
        await self._session.commit()
        phone = conversation.external_id if conversation is not None else ""
        funnel_stage = conversation.funnel_stage if conversation is not None else None
        with_service = await self._card_services.card_ids_with_service([card.id], organization_id)
        return CardOut(
            id=card.id,
            title=card.title,
            conversation_id=card.conversation_id,
            stage_id=card.stage_id,
            phone=phone,
            rating=rating_for_stage(funnel_stage, accepted_service=card.id in with_service),
            is_ai_active=conversation.is_ai_active if conversation is not None else True,
            created_at=card.created_at,
        )

    async def delete(self, card_id: uuid.UUID, organization_id: uuid.UUID) -> bool:
        """Baja de una oportunidad: cierra la conversación y borra la card del tablero.
        Cerrar (`closed_at`) equivale a llevarla al stage terminal de su pipeline
        (Descalificado/Cerrado) — el webhook no reusa conversaciones cerradas, así que
        el próximo inbound del lead abre una oportunidad nueva y limpia, sin arrastrar el
        contexto viejo (#163). No dispara el hook 'won' (borrar ≠ ganar: no crea contacto).
        El historial IA queda colgado de la conversación cerrada como registro."""
        card = await self._board.get_card(card_id, organization_id)
        if card is None:
            return False
        conversation = await self._conv.get_by_id(card.conversation_id, organization_id)
        if conversation is not None and conversation.closed_at is None:
            conversation.closed_at = datetime.now(UTC)
        await self._cards.delete(card)
        await self._session.commit()
        return True
