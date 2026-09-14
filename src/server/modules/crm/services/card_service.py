"""Proyección CRM del estado de la conversación (M-CRM-api, slice 1).

`funnel_stage` es la verdad del agente; la card es su proyección. `sync` reconcilia
idempotentemente la card desde el estado **ya persistido** de la conversación: la crea
si falta y la mueve al stage espejo (pipeline IA) o, en handoff/silencio, al pipeline
Gestión Postventa. Cada movimiento escribe `card_move(moved_by='agent')` y publica al
canal `crm:events:{tenant}`. Mover por arrastre del humano = API CRM (slice 2).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.modules.agent.domain.models import Conversation
from server.modules.agent.repositories.contact_repository import ContactRepository
from server.modules.agent.repositories.conversation_repository import ConversationRepository
from server.modules.agent.repositories.handoff_event_repository import HandoffEventRepository
from server.modules.crm.domain import stages
from server.modules.crm.domain.lead_identity import resolve_lead_title
from server.modules.crm.domain.models import Card, CardMove, Stage
from server.modules.crm.repositories.board_repository import BoardRepository
from server.modules.crm.repositories.card_repository import CardRepository
from server.shared.logger import get_logger
from server.shared.pubsub import Publisher, card_moved_event, crm_channel

logger = get_logger(__name__)

# Stages terminales del CRM = oportunidad cerrada (#163).
CLOSED_STATUSES = frozenset({"won", "lost"})


def apply_closed_state(conversation: Conversation, status_code: str) -> None:
    """Marca/desmarca `conversation.closed_at` según el stage destino de su card:
    terminal (won/lost) → cerrada (no se reusa: el próximo inbound abre oportunidad
    nueva); stage abierto → reabierta. Idempotente (#163)."""
    conversation.closed_at = datetime.now(UTC) if status_code in CLOSED_STATUSES else None


# Stages del pipeline "Gestión Postventa" usados como destino del handoff. Los nombres
# viven en `crm/domain/stages.py` (fuente única); se reexportan por compatibilidad.
HUMAN_INTAKE_STAGE = stages.HUMAN_INTAKE  # intake genérico (pidió humano / falló el bot)
PAYMENT_VALIDATION_STAGE = stages.PAYMENT_VALIDATION  # mandó comprobante → validar pago

# motivo del handoff → stage destino. Default (motivo desconocido / takeover manual sin
# evento) = intake genérico, nunca "Por validar pago" (pedir humano ≠ validar un pago).
_HUMAN_STAGE_BY_REASON: dict[str, str] = {
    "payment_validation": PAYMENT_VALIDATION_STAGE,
    "explicit_request": HUMAN_INTAKE_STAGE,
    "agent_error": HUMAN_INTAKE_STAGE,
    "unknown_service": HUMAN_INTAKE_STAGE,  # servicio fuera del catálogo → intake (#94)
}

# funnel_stage → nombre del stage en el pipeline IA (espejo). `handed_off` no está acá:
# cruza al pipeline human (ver `target_stage`).
_IA_STAGE_BY_FUNNEL: dict[FunnelStage, str] = {
    FunnelStage.NEW: stages.IA_NEW,
    FunnelStage.ENGAGING: stages.IA_ENGAGING,
    FunnelStage.QUALIFYING: stages.IA_QUALIFYING,
    FunnelStage.QUALIFIED: stages.IA_QUALIFIED,
    FunnelStage.DISQUALIFIED: stages.IA_DISQUALIFIED,
}


def target_stage(
    funnel_stage: FunnelStage, is_ai_active: bool, handoff_reason: str | None = None
) -> tuple[str, str]:
    """(kind, stage_name) destino de la card. Handoff/silencio → Gestión Postventa,
    en la stage que corresponda al motivo (`payment_validation` → validar pago; el
    resto → intake genérico)."""
    if not is_ai_active or funnel_stage is FunnelStage.HANDED_OFF:
        return ("human", _HUMAN_STAGE_BY_REASON.get(handoff_reason or "", HUMAN_INTAKE_STAGE))
    return ("ia", _IA_STAGE_BY_FUNNEL[funnel_stage])


class CardService:
    def __init__(self, *, session: AsyncSession, publisher: Publisher) -> None:
        self._session = session
        self._publisher = publisher
        self._conv = ConversationRepository(session)
        self._board = BoardRepository(session)
        self._cards = CardRepository(session)
        self._contacts = ContactRepository(session)
        self._handoff = HandoffEventRepository(session)

    async def _would_regress(
        self, current_stage_id: uuid.UUID, target: Stage, tenant_id: uuid.UUID
    ) -> bool:
        """True si el sync haría retroceder la card dentro del pipeline humano.

        El sync deriva el stage del estado de la conversación, que después del handoff
        deja de contar la historia: con la conversación abierta y el último handoff
        `payment_validation`, cualquier inbound del lead ("gracias", un sticker) volvía
        a apuntar a "Por validar pago" y deshacía el trabajo del operador (una card ya
        entregada volvía a la bandeja de pendientes).

        Regla: **dentro del pipeline humano el stage solo avanza**; para volver atrás
        está el move manual, que es deliberado y queda auditado. Cruzar de pipeline
        (handoff, o reactivar la IA con el toggle) sí se permite: no es un retroceso
        sino un cambio de dueño de la conversación.
        """
        current = await self._board.get_stage_by_id(current_stage_id, tenant_id)
        if current is None:  # pragma: no cover - la card siempre apunta a un stage vivo
            return False
        if current.pipeline.kind != stages.PIPELINE_HUMAN:
            return False
        if target.pipeline_id != current.pipeline_id:
            return False
        if target.position > current.position:
            return False
        logger.info(
            "crm.sync_regression_skipped",
            tenant_id=str(tenant_id),
            stage_from=current.name,
            stage_to=target.name,
        )
        return True

    async def sync(self, conversation_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        conversation = await self._conv.get_by_id(conversation_id, tenant_id)
        if conversation is None:
            return

        reason: str | None = None
        if not conversation.is_ai_active or conversation.funnel_stage is FunnelStage.HANDED_OFF:
            reason = await self._handoff.get_latest_reason(str(conversation_id))
        kind, stage_name = target_stage(
            conversation.funnel_stage, conversation.is_ai_active, reason
        )
        stage = await self._board.get_stage(tenant_id, kind, stage_name)
        if stage is None:  # pipelines no seedeados para la org
            logger.warning(
                "crm.stage_not_found", kind=kind, stage=stage_name, tenant_id=str(tenant_id)
            )
            return

        card = await self._cards.get_by_conversation(conversation_id)
        if card is None:
            # Un lead que vuelve ya puede ser contacto: la card nueva nace linkeada y
            # titulada con su nombre, no con el wa_id crudo.
            contact = await self._contacts.get_by_phone(tenant_id, conversation.external_id)
            card = await self._cards.add(
                Card(
                    organization_id=tenant_id,
                    conversation_id=conversation_id,
                    stage_id=stage.id,
                    title=resolve_lead_title(
                        conversation.full_name,
                        contact.full_name if contact is not None else None,
                        conversation.external_id,
                    ),
                    contact_id=contact.id if contact is not None else None,
                )
            )
            await self._cards.add_move(
                CardMove(
                    card_id=card.id, stage_from_id=None, stage_to_id=stage.id, moved_by="agent"
                )
            )
        elif card.stage_id != stage.id:
            if await self._would_regress(card.stage_id, stage, tenant_id):
                return
            await self._cards.add_move(
                CardMove(
                    card_id=card.id,
                    stage_from_id=card.stage_id,
                    stage_to_id=stage.id,
                    moved_by="agent",
                )
            )
            card.stage_id = stage.id
        else:
            return  # card ya está en el stage correcto → sin movimiento ni evento

        apply_closed_state(conversation, stage.status_code)  # cierra si cae en won/lost (#163)
        await self._session.commit()
        await self._publisher.publish(
            crm_channel(tenant_id),
            card_moved_event(
                card_id=card.id,
                stage=stage_name,
                conversation_id=conversation_id,
                pipeline_kind=kind,
            ),
        )
        logger.info(
            "crm.card_synced", conversation_id=str(conversation_id), kind=kind, stage=stage_name
        )
