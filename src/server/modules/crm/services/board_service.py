"""Lecturas y operaciones del tablero CRM (slice 2). Todo tenant-scoped.

Lectura del board + detalle de card con hilo espejo; movimiento manual de card por
el humano (`card_move(moved_by=user_id)`) y toggle de takeover (`is_ai_active`). Cada
mutación publica a `crm:events:{tenant}` para el realtime (consumido en slice 2b).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from server.config import get_settings
from server.modules.agent.domain.funnel_fsm import reopen
from server.modules.agent.domain.models import Conversation
from server.modules.agent.repositories.ai_chat_history_repository import AiChatHistoryRepository
from server.modules.agent.repositories.contact_repository import ContactRepository
from server.modules.agent.repositories.conversation_repository import ConversationRepository
from server.modules.agent.repositories.handoff_event_repository import HandoffEventRepository
from server.modules.crm.api.schemas import (
    BoardOut,
    CardContactOut,
    CardDetailOut,
    CardMoveOut,
    CardOut,
    CardServiceOut,
    PipelineOut,
    StageOut,
)
from server.modules.crm.domain import attention, card_flags
from server.modules.crm.domain.lead_identity import resolve_lead_title
from server.modules.crm.domain.lead_rating import rating_for_stage
from server.modules.crm.domain.models import CardMove
from server.modules.crm.repositories.board_repository import BoardCardRow, BoardRepository
from server.modules.crm.repositories.card_repository import CardRepository
from server.modules.crm.repositories.card_service_repository import CardServiceRepository
from server.modules.crm.repositories.mirror_repository import MirrorRepository
from server.modules.crm.services.card_service import apply_closed_state
from server.modules.crm.services.mirror import build_thread
from server.shared.exceptions import WonRequiresNameError
from server.shared.logger import get_logger
from server.shared.pubsub import Publisher, card_moved_event, crm_channel

logger = get_logger(__name__)

# Motivos de handoff que se muestran como etiqueta de alerta en la card. Derivado del
# último `handoff_event.reason`; sin campo nuevo ni migración (#94). `agent_error`
# (server#288): el agente no pudo responder (proveedor caído/config) y derivó.
_ALERT_REASONS = frozenset({"unknown_service", "agent_error"})

# Actor de un move hecho por el sistema (entrega automática, cierre automático), a
# diferencia del `user_id` de un operador o del `'agent'` del bot en su pipeline.
# `card_move.moved_by` es texto libre, así que el literal es aditivo: el CRM lo traduce.
SYSTEM_ACTOR = "system"
# Quién movió la card: un operador (su id) o un actor del sistema.
MovedBy = uuid.UUID | str


def _alert_for_reason(reason: str | None) -> str | None:
    return reason if reason in _ALERT_REASONS else None


class BoardService:
    def __init__(self, *, session: AsyncSession, publisher: Publisher) -> None:
        self._session = session
        self._publisher = publisher
        self._board = BoardRepository(session)
        self._cards = CardRepository(session)
        self._card_services = CardServiceRepository(session)
        self._mirror = MirrorRepository(session)
        self._conv = ConversationRepository(session)
        self._contacts = ContactRepository(session)
        self._handoff = HandoffEventRepository(session)
        self._ai_history = AiChatHistoryRepository(session)

    async def get_board(self, organization_id: uuid.UUID) -> BoardOut:
        pipelines = await self._board.list_pipelines(organization_id)
        cards = await self._board.list_cards(organization_id)
        reasons = await self._handoff.latest_reasons(
            [str(row.card.conversation_id) for row in cards]
        )
        activity = await self._activity(cards, organization_id)
        with_service = await self._card_services.card_ids_with_service(
            [row.card.id for row in cards], organization_id
        )
        by_stage: dict[uuid.UUID, list[CardOut]] = {}
        for row in cards:
            card = row.card
            by_stage.setdefault(card.stage_id, []).append(
                CardOut(
                    id=card.id,
                    # Nombre de la conversación → contacto vinculado → teléfono: una card
                    # linkeada nunca muestra el wa_id crudo aunque nadie la haya renombrado.
                    title=resolve_lead_title(row.full_name, row.contact_name, row.phone),
                    conversation_id=card.conversation_id,
                    stage_id=card.stage_id,
                    phone=row.phone or "",
                    rating=rating_for_stage(
                        row.funnel_stage, accepted_service=card.id in with_service
                    ),
                    alert=_alert_for_reason(reasons.get(str(card.conversation_id))),
                    is_ai_active=row.is_ai_active if row.is_ai_active is not None else True,
                    awaiting_human=attention.is_awaiting(
                        activity.get(card.conversation_id),
                        is_ai_active=row.is_ai_active is not False,
                        closed_at=row.closed_at,
                        attended_at=card.attended_at,
                    ),
                    flags=card_flags.normalize(card.flags),
                    last_activity_at=attention.last_activity(activity.get(card.conversation_id)),
                    attended_at=card.attended_at,
                    created_at=card.created_at,
                )
            )
        return BoardOut(
            pipelines=[
                PipelineOut(
                    id=p.id,
                    kind=p.kind,
                    name=p.name,
                    position=p.position,
                    stages=[
                        StageOut(
                            id=s.id,
                            name=s.name,
                            position=s.position,
                            status_code=s.status_code,
                            cards=by_stage.get(s.id, []),
                        )
                        for s in p.stages
                    ],
                )
                for p in pipelines
            ]
        )

    async def _activity(
        self,
        cards: list[BoardCardRow],
        organization_id: uuid.UUID,
    ) -> dict[uuid.UUID, attention.Activity]:
        """Últimos timestamps de cada conversación del board, en dos queries batch.

        Alimenta dos cosas: la señal "sin responder" y `last_activity_at`, que es lo que
        ordena la cola de atención. Se calcula para **todas** las cards, no solo las de IA
        apagada: cuánto lleva parada una oportunidad se pregunta igual con el agente activo.
        La decisión de si alguien espera vive en `domain/attention.py`."""
        if not cards:
            return {}
        ai_activity = await self._ai_history.last_activity_by_thread(
            [str(row.card.conversation_id) for row in cards]
        )
        human_replies = await self._mirror.last_human_reply_by_session(
            [row.phone for row in cards if row.phone], organization_id
        )
        return {
            row.card.conversation_id: attention.Activity(
                *ai_activity.get(str(row.card.conversation_id), (None, None)),
                last_human=human_replies.get(row.phone or ""),
            )
            for row in cards
        }

    async def mark_attended(
        self, card_id: uuid.UUID, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool:
        """Registra que una persona se hizo cargo: la card sale de la cola de atención.

        Es la salida para lo que ninguna regla acierta — la conversación que termina con un
        "gracias" y no necesita respuesta. No cierra la oportunidad ni toca sus avisos: un
        aviso de entrega (comprobante en revisión, falta un link) es trabajo real y tiene su
        propio camino de resolución. Y no es definitivo: si el lead vuelve a escribir, la
        señal se reenciende sola porque su mensaje es posterior a `attended_at`.

        False si la card no existe en la organización."""
        card = await self._board.get_card(card_id, organization_id)
        if card is None:
            return False
        card.attended_at = datetime.now(UTC)
        card.attended_by = str(user_id)
        await self._session.commit()
        await self._publisher.publish(
            crm_channel(organization_id),
            {
                "type": "card_attended",
                "card_id": str(card_id),
                "conversation_id": str(card.conversation_id),
            },
        )
        logger.info("crm.card_attended", card_id=str(card_id), user_id=str(user_id))
        return True

    async def get_card_detail(
        self, card_id: uuid.UUID, organization_id: uuid.UUID
    ) -> CardDetailOut | None:
        card = await self._board.get_card(card_id, organization_id)
        if card is None:
            return None
        conversation = await self._conv.get_by_id(card.conversation_id, organization_id)
        session_id = conversation.external_id if conversation is not None else ""
        ai_rows = await self._mirror.ai_history(str(card.conversation_id))
        # Scope human messages to THIS opportunity's lifetime: a returning lead opens a
        # new conversation (#163), but app_chat_histories is keyed by phone, so without
        # `since` the new card would show the previous closed opportunity's human messages.
        since = conversation.created_at if conversation is not None else None
        app_rows = await self._mirror.app_history(session_id, organization_id, since)
        moves = await self._cards.list_moves(card.id)
        contact = (
            await self._contacts.get_by_id(card.contact_id, organization_id)
            if card.contact_id is not None
            else None
        )
        services = await self._card_services.list_for_card(card.id, organization_id)
        reason = await self._handoff.get_latest_reason(str(card.conversation_id))
        return CardDetailOut(
            id=card.id,
            title=resolve_lead_title(
                conversation.full_name if conversation is not None else None,
                contact.full_name if contact is not None else None,
                session_id or card.title,
            ),
            conversation_id=card.conversation_id,
            stage_id=card.stage_id,
            is_ai_active=conversation.is_ai_active if conversation is not None else True,
            phone=session_id,
            full_name=conversation.full_name if conversation is not None else None,
            notes=card.notes,
            rating=rating_for_stage(
                conversation.funnel_stage if conversation is not None else None,
                accepted_service=bool(services),
            ),
            alert=_alert_for_reason(reason),
            ai_summary=conversation.ai_summary if conversation is not None else None,
            thread=build_thread(ai_rows, app_rows, get_settings().media_base_url),
            moves=[
                CardMoveOut(
                    stage_from_name=m.stage_from_name,
                    stage_from_color=m.stage_from_color,
                    stage_to_name=m.stage_to_name,
                    stage_to_color=m.stage_to_color,
                    moved_by=m.moved_by,
                    reason=m.reason,
                    moved_at=m.moved_at,
                )
                for m in moves
            ],
            contact=(
                CardContactOut(id=contact.id, full_name=contact.full_name)
                if contact is not None
                else None
            ),
            services=[
                CardServiceOut(
                    id=s.id,
                    service_id=s.service_id,
                    nombre=s.nombre,
                    precio=s.precio,
                    moneda=s.moneda,
                    source=s.source,
                )
                for s in services
            ],
            flags=card_flags.normalize(card.flags),
            created_at=card.created_at,
        )

    async def set_card_services(
        self,
        card_id: uuid.UUID,
        organization_id: uuid.UUID,
        service_ids: list[uuid.UUID],
    ) -> list[CardServiceOut] | None:
        """Asigna manualmente (source='assigned') el set de servicios a la card (#132).
        Devuelve la lista resultante (incluye los `captured` del bot), o None si la card
        no existe en la organización. `ValueError` si algún servicio no es del catálogo."""
        card = await self._board.get_card(card_id, organization_id)
        if card is None:
            return None
        unique_ids = list(dict.fromkeys(service_ids))
        valid = await self._card_services.existing_service_ids(unique_ids, organization_id)
        if set(unique_ids) - valid:
            raise ValueError("algún servicio no existe en el catálogo de la organización")
        await self._card_services.set_assigned(card_id, organization_id, unique_ids)
        await self._session.commit()
        rows = await self._card_services.list_for_card(card_id, organization_id)
        return [
            CardServiceOut(
                id=r.id,
                service_id=r.service_id,
                nombre=r.nombre,
                precio=r.precio,
                moneda=r.moneda,
                source=r.source,
            )
            for r in rows
        ]

    async def _ensure_lead_name(
        self, organization_id: uuid.UUID, conversation: Conversation
    ) -> None:
        """WonRequiresNameError si el lead no tiene nombre en ningún lado (#241)."""
        if (conversation.full_name or "").strip():
            return
        contact = await self._contacts.get_by_phone(organization_id, conversation.external_id)
        if contact is not None and (contact.full_name or "").strip():
            return
        raise WonRequiresNameError(
            "la oportunidad necesita el nombre del lead antes de marcarse como ganada"
        )

    async def move_card(
        self,
        card_id: uuid.UUID,
        stage_id: uuid.UUID,
        moved_by: MovedBy,
        organization_id: uuid.UUID,
        reason: str | None = None,
    ) -> CardOut | None:
        """Mueve la card a un stage de la misma org. None si la card no existe;
        ValueError si el stage no pertenece a la organización; WonRequiresNameError
        (422) si se intenta ganar sin nombre del lead (#241). `reason`: motivo opcional
        del move manual, persistido en `card_move` y logueado para audit (#253).

        `moved_by` es el id del operador, o `SYSTEM_ACTOR` cuando mueve el sistema (la
        entrega automática). Es el **único** camino de move con el hook de won, así que
        el sistema pasa por acá en vez de duplicar esa lógica."""
        card = await self._board.get_card(card_id, organization_id)
        if card is None:
            return None
        stage = await self._board.get_stage_by_id(stage_id, organization_id)
        if stage is None:
            raise ValueError("stage no encontrado en la organización")

        conversation = await self._conv.get_by_id(card.conversation_id, organization_id)
        if card.stage_id != stage.id:
            # Ganar exige nombre: sin `conversation.full_name` ni un contacto ya nombrado
            # para el teléfono, el hook 'won' crearía un contacto "Sin nombre" (#241).
            if stage.status_code == "won" and conversation is not None:
                await self._ensure_lead_name(organization_id, conversation)
            stage_from_id = card.stage_id  # captura antes de reasignar (para el audit log)
            await self._cards.add_move(
                CardMove(
                    card_id=card.id,
                    stage_from_id=card.stage_id,
                    stage_to_id=stage.id,
                    moved_by=str(moved_by),
                    reason=reason,
                )
            )
            card.stage_id = stage.id
            # Hook 'won': al cerrar la oportunidad, crear/actualizar el contacto del lead
            # (idempotente por org+phone) con teléfono y nombre, en la misma transacción
            # que el move. Stages no-'won' no tocan contactos (#101).
            if stage.status_code == "won" and conversation is not None:
                contact = await self._contacts.upsert(
                    organization_id, conversation.external_id, conversation.full_name
                )
                # Linkea TODAS las cards del teléfono (no solo la ganada): las oportunidades
                # previas/futuras del mismo lead comparten identidad.
                await self._cards.link_to_contact_by_phone(
                    organization_id, conversation.external_id, contact.id
                )
                card.contact_id = contact.id  # keep the in-memory card consistent (#139)
            if conversation is not None:
                # Cerrar/reabrir la oportunidad: una card en won/lost no se reusa — el
                # próximo inbound del lead abre una oportunidad nueva (#163).
                apply_closed_state(conversation, stage.status_code)
            await self._session.commit()
            await self._publisher.publish(
                crm_channel(organization_id),
                card_moved_event(
                    card_id=card.id,
                    stage=stage.name,
                    conversation_id=card.conversation_id,
                    pipeline_kind=stage.pipeline.kind,
                ),
            )
            logger.info(
                "crm.card_moved",
                card_id=str(card.id),
                stage_from=str(stage_from_id),
                stage_to=stage.name,
                moved_by=str(moved_by),
                reason=reason,
            )
        handoff_reason = await self._handoff.get_latest_reason(str(card.conversation_id))
        with_service = await self._card_services.card_ids_with_service([card.id], organization_id)
        linked_contact = (
            await self._contacts.get_by_id(card.contact_id, organization_id)
            if card.contact_id is not None
            else None
        )
        return CardOut(
            id=card.id,
            title=resolve_lead_title(
                conversation.full_name if conversation is not None else None,
                linked_contact.full_name if linked_contact is not None else None,
                conversation.external_id if conversation is not None else card.title,
            ),
            conversation_id=card.conversation_id,
            stage_id=card.stage_id,
            phone=conversation.external_id if conversation is not None else "",
            rating=rating_for_stage(
                conversation.funnel_stage if conversation is not None else None,
                accepted_service=card.id in with_service,
            ),
            alert=_alert_for_reason(handoff_reason),
            is_ai_active=conversation.is_ai_active if conversation is not None else True,
            # Un move es acción humana sobre la card: deja de estar "en espera". El próximo
            # board refresh recomputa la señal contra el hilo real si siguiera sin responder.
            awaiting_human=False,
            flags=card_flags.normalize(card.flags),
            attended_at=card.attended_at,
            created_at=card.created_at,
        )

    async def set_ai_active(
        self, conversation_id: uuid.UUID, is_ai_active: bool, organization_id: uuid.UUID
    ) -> bool:
        """Toggle de takeover. False si la conversación no existe en la org."""
        conversation = await self._conv.get_by_id(conversation_id, organization_id)
        if conversation is None:
            return False
        conversation.is_ai_active = is_ai_active
        if is_ai_active:
            # Reactivating over a terminal handoff: re-open the funnel so the agent
            # continues with context (→ engaging) instead of being stuck terminal,
            # which would bounce the card back to Gestión Postventa (New#1c). No-op for
            # already-active stages.
            conversation.funnel_stage = reopen(conversation.funnel_stage, fresh=False)
        await self._session.commit()
        await self._publisher.publish(
            crm_channel(organization_id),
            {
                "type": "ai_active_changed",
                "conversation_id": str(conversation_id),
                "is_ai_active": is_ai_active,
            },
        )
        return True
