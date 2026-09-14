import uuid
from datetime import datetime
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import contains_eager

from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.modules.agent.domain.models import Contact, Conversation
from server.modules.crm.domain.models import Card, Pipeline, Stage


class BoardCardRow(NamedTuple):
    """Card + campos de su conversación y contacto vinculado para la proyección del
    board. `full_name`/`contact_name` alimentan el título resuelto del lead."""

    card: Card
    phone: str | None
    full_name: str | None
    funnel_stage: FunnelStage | None
    is_ai_active: bool | None
    closed_at: datetime | None
    contact_name: str | None


class ExportCardRow(NamedTuple):
    """Card + conversación + stage, aplanados para el export CSV de leads (#176)."""

    card_id: uuid.UUID
    phone: str
    full_name: str | None
    contact_name: str | None
    funnel_stage: FunnelStage | None
    stage_name: str
    created_at: datetime
    updated_at: datetime


class BoardRepository:
    """Lecturas de pipelines/stages/cards, siempre tenant-scoped por `organization_id`."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_stage(
        self, organization_id: uuid.UUID, kind: str, stage_name: str
    ) -> Stage | None:
        """Resuelve un `Stage` por (org, kind del pipeline, nombre del stage)."""
        result = await self.session.execute(
            select(Stage)
            .join(Pipeline, Stage.pipeline_id == Pipeline.id)
            .where(
                Pipeline.organization_id == organization_id,
                Pipeline.kind == kind,
                Stage.name == stage_name,
            )
        )
        return result.scalar_one_or_none()

    async def get_stage_by_status(
        self, organization_id: uuid.UUID, kind: str, status_code: str
    ) -> Stage | None:
        """Stage terminal de un pipeline por su `status_code` (`won`/`lost`).

        Resolver el cierre por status y no por nombre: `stage.name` es dato por
        organización (se renombra, se traduce), mientras que el status es el contrato
        que ya usan el hook de won y el front. Si hubiera más de uno, gana el de menor
        `position` (el primero al que llegaría una card)."""
        result = await self.session.execute(
            select(Stage)
            .join(Pipeline, Stage.pipeline_id == Pipeline.id)
            .where(
                Pipeline.organization_id == organization_id,
                Pipeline.kind == kind,
                Stage.status_code == status_code,
            )
            .order_by(Stage.position)
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_stage_by_id(
        self, stage_id: uuid.UUID, organization_id: uuid.UUID
    ) -> Stage | None:
        """Stage por id, validando que su pipeline sea de la organización.

        Carga el pipeline eager (`stage.pipeline.kind` se usa en el evento
        `card_moved` sin lazy-load async)."""
        result = await self.session.execute(
            select(Stage)
            .join(Pipeline, Stage.pipeline_id == Pipeline.id)
            .where(Stage.id == stage_id, Pipeline.organization_id == organization_id)
            .options(contains_eager(Stage.pipeline))
        )
        return result.scalar_one_or_none()

    async def list_pipelines(self, organization_id: uuid.UUID) -> list[Pipeline]:
        """Pipelines de la org (stages vienen eager por `lazy='selectin'`), ordenados."""
        result = await self.session.execute(
            select(Pipeline)
            .where(Pipeline.organization_id == organization_id)
            .order_by(Pipeline.position)
        )
        return list(result.scalars().all())

    async def list_cards(self, organization_id: uuid.UUID) -> list[BoardCardRow]:
        """Cards de la org con el teléfono (`conversation.external_id`), `full_name`,
        el `funnel_stage` (alimenta la calificación hot/medium/cold, #96), `is_ai_active`
        (badge de takeover + señal de "sin responder"), `closed_at` (oportunidad cerrada
        → no se marca "sin responder") y el nombre del contacto vinculado (título).

        LEFT JOIN: una card sin conversación (no debería ocurrir; FK NOT NULL) o sin
        contacto igual aparece con esos campos `None`, en vez de desaparecer del tablero."""
        result = await self.session.execute(
            select(
                Card,
                Conversation.external_id,
                Conversation.full_name,
                Conversation.funnel_stage,
                Conversation.is_ai_active,
                Conversation.closed_at,
                Contact.full_name,
            )
            .outerjoin(Conversation, Card.conversation_id == Conversation.id)
            .outerjoin(Contact, Card.contact_id == Contact.id)
            .where(Card.organization_id == organization_id)
        )
        return [BoardCardRow(*row) for row in result.tuples().all()]

    async def list_cards_for_export(self, organization_id: uuid.UUID) -> list[ExportCardRow]:
        """Cards de la org con teléfono, nombres (conversación + contacto vinculado),
        funnel y nombre del stage, para el export de leads (#176). INNER JOIN a
        `conversation` (FK NOT NULL: sin teléfono no hay lead que exportar). Orden asc
        por alta de la conversación: el dedup por teléfono se queda con la más reciente."""
        result = await self.session.execute(
            select(
                Card.id,
                Conversation.external_id,
                Conversation.full_name,
                Contact.full_name,
                Conversation.funnel_stage,
                Stage.name,
                Conversation.created_at,
                Conversation.updated_at,
            )
            .join(Conversation, Card.conversation_id == Conversation.id)
            .join(Stage, Card.stage_id == Stage.id)
            .outerjoin(Contact, Card.contact_id == Contact.id)
            .where(Card.organization_id == organization_id)
            .order_by(Conversation.created_at, Card.id)
        )
        return [ExportCardRow(*row) for row in result.tuples().all()]

    async def get_card(self, card_id: uuid.UUID, organization_id: uuid.UUID) -> Card | None:
        result = await self.session.execute(
            select(Card).where(Card.id == card_id, Card.organization_id == organization_id)
        )
        return result.scalar_one_or_none()

    async def get_card_fresh(self, card_id: uuid.UUID, organization_id: uuid.UUID) -> Card | None:
        """Card re-leída de la base, ignorando lo que la sesión tenga cacheado.

        `get_card` puede devolver la instancia del identity map con los valores que se
        leyeron al principio de la operación. Para un comparar-y-mover eso es una
        trampa: un trabajo largo (una llamada a un modelo) leería el estado de hace
        segundos y pisaría lo que un humano cambió mientras tanto.
        """
        result = await self.session.execute(
            select(Card)
            .where(Card.id == card_id, Card.organization_id == organization_id)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def get_first_stage(self, organization_id: uuid.UUID, kind: str) -> Stage | None:
        """Primer stage (menor `position`) del pipeline `kind` de la org. Destino del alta
        manual de oportunidad (#97)."""
        result = await self.session.execute(
            select(Stage)
            .join(Pipeline, Stage.pipeline_id == Pipeline.id)
            .where(Pipeline.organization_id == organization_id, Pipeline.kind == kind)
            .order_by(Stage.position)
            .limit(1)
        )
        return result.scalar_one_or_none()
