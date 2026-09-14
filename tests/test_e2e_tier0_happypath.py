"""E2E del happy path núcleo (Tier 0), determinístico (LLM stubeado).

Recorre el flujo completo del sprint Tier 0 sobre las piezas reales, integradas:

  Fase A — agente (WhatsApp): lead nuevo → saludo → servicios del catálogo → elige un
    servicio → recibe el PDF correcto → califica → declara pago → se le pide el
    comprobante; la card cruza new→engaging→qualifying→qualified→handed_off y el
    agente queda silenciado (cubre #77, #87, #89, #84).
  Fase B — operador (CRM): valida el pago y genera la entrada de un curso presencial
    → se envía el QR al lead y la card pasa a "Entregado" (cubre #90, #263).
  Fase C — re-apertura: el lead cerrado vuelve a escribir → se abre una conversación
    nueva (= oportunidad nueva en Gestión IA, contexto limpio); la cerrada queda como
    histórico (cubre #76, #163).

VERDE = todos los stages se movieron, todos los outbound se enviaron, sin estados
huérfanos. La validación de lenguaje natural con LLM real es el harness Docker aparte
(ESTADO_Y_RUNBOOK); acá se asertan ruteo/FSM/stages/dispatch, que son determinísticos.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.agent.domain.catalog_models import Service
from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.modules.agent.domain.llm_port import Message, Role, ToolSpec, ToolUse, Turn
from server.modules.agent.domain.models import (
    Agent,
    AgentInstance,
    Conversation,
    Product,
)
from server.modules.agent.domain.ports import ConversationSnapshot, OutboundMedia
from server.modules.agent.domain.tool_catalogue import build_default_registry
from server.modules.agent.services.agent_service import AgentService
from server.modules.agent.services.conversation_store import window_up_to_latest_user
from server.modules.agent.services.router import Flow
from server.modules.agent.services.webhook_service import WhatsAppWebhookService
from server.modules.crm.domain import stages as stage_names
from server.modules.crm.domain.event_models import Event
from server.modules.crm.domain.models import Card, CardService, Pipeline, Stage, StageStatus
from server.modules.crm.services import qr_image
from server.modules.crm.services.entry_service import EntryService

LEAD_WA_ID = "+59169005037"
_CATALOG: dict[str, object] = {
    "services": [
        {
            "slug": "curso-edicion",
            "nombre": "Curso de edición",
            "precio": "480 Bs",
            "categoria": "Cursos",
            "categoria_slug": "cursos",
        },
        {
            "slug": "produccion-musical",
            "nombre": "Producción musical",
            "precio": "600 Bs",
            "categoria": "Cursos",
            "categoria_slug": "cursos",
        },
    ],
    "categories": [
        {
            "slug": "cursos",
            "nombre": "Cursos",
            "materials": [
                {"url": "https://media.test/curso-edicion.pdf", "filename": "curso-edicion.pdf"}
            ],
        },
    ],
}
_SYSTEM = "Sos el asistente de Mirko."


# --------------------------- Fase A: agente (stubs) ---------------------------


@dataclass
class _Conv:
    stage: FunnelStage = FunnelStage.NEW
    is_ai_active: bool = True
    window: list[tuple[int, Message]] = field(default_factory=list)  # (message_order, msg)
    answered_through_order: int = 0


class _StatefulStore:
    """ConversationStore en memoria que persiste el turno para el siguiente. Emula el
    `message_order` (contador monótono) y el high-water mark como el store real (#240)."""

    def __init__(self) -> None:
        self.conv = _Conv()
        self.agent_id = uuid.uuid4()
        self._order = 0

    def _next_order(self) -> int:
        self._order += 1
        return self._order

    def push_inbound(self, text: str) -> None:
        self.conv.window.append((self._next_order(), Message(role=Role.USER, text=text)))

    async def load(
        self, conversation_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> ConversationSnapshot | None:
        window, latest_user_order = window_up_to_latest_user(self.conv.window[-10:])
        return ConversationSnapshot(
            external_id=LEAD_WA_ID,
            agent_id=self.agent_id,
            funnel_stage=self.conv.stage,
            is_ai_active=self.conv.is_ai_active,
            system_prompt=_SYSTEM,
            config=_CATALOG,
            messages_window=window,
            latest_user_order=latest_user_order,
            answered_through_order=self.conv.answered_through_order,
        )

    async def save_turn(
        self,
        conversation_id: uuid.UUID,
        tenant_id: uuid.UUID,
        *,
        reply_text: str,
        funnel_stage: FunnelStage,
        is_ai_active: bool,
        answered_order: int,
        handoff_reason: str | None = None,
        media: Sequence[OutboundMedia] = (),
    ) -> None:
        self.conv.stage = funnel_stage
        self.conv.is_ai_active = is_ai_active
        self.conv.answered_through_order = answered_order
        if reply_text:
            reply = Message(role=Role.ASSISTANT, text=reply_text)
            self.conv.window.append((self._next_order(), reply))

    async def save_agent_error(
        self, conversation_id: uuid.UUID, tenant_id: uuid.UUID, *, category: str, detail: str
    ) -> None:  # contrato server#288; el happy path no lo ejercita
        return None


class _RecordingSender:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.documents: list[str] = []  # filenames
        self.images: list[str] = []  # links

    async def send_text(self, to: str, body: str) -> None:
        self.texts.append(body)

    async def send_image(self, to: str, link: str, caption: str = "") -> None:
        self.images.append(link)

    async def send_document(self, to: str, link: str, filename: str, caption: str = "") -> None:
        self.documents.append(filename)


@dataclass
class _ScriptedLLM:
    """Router-classify devuelve `classify_flow`; el loop generativo saca el próximo Turn."""

    loop_turns: list[Turn] = field(default_factory=list)
    classify_flow: Flow = Flow.UNKNOWN

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float | None = None,
    ) -> Turn:
        if any(t.name == "classify_intent" for t in tools):
            return Turn(
                "",
                (
                    ToolUse(
                        id="cls",
                        name="classify_intent",
                        input={"flow": self.classify_flow.value, "confidence": 0.9},
                    ),
                ),
                "tool_use",
            )
        if self.loop_turns:
            return self.loop_turns.pop(0)
        return Turn("ok", (), "end_turn")


def _u(name: str, payload: dict[str, object]) -> ToolUse:
    return ToolUse(id=f"u_{name}", name=name, input=payload)


async def _drive(store: _StatefulStore, sender: _RecordingSender, text: str, *turns: Turn) -> None:
    store.push_inbound(text)
    service = AgentService(
        store=store,
        llm=_ScriptedLLM(list(turns)),
        registry=build_default_registry(),
        sender=sender,
    )
    await service.process_message(uuid.uuid4(), uuid.uuid4())


async def test_e2e_tier0_agent_journey_to_payment_handoff() -> None:
    store, sender = _StatefulStore(), _RecordingSender()
    stages: list[FunnelStage] = []

    # 1) saludo (0 IA): new → engaging
    await _drive(store, sender, "hola")
    stages.append(store.conv.stage)
    assert "asistente de Mirko" in sender.texts[-1]

    # 2) muestra el catálogo (get_service sobre la lista publicada)
    await _drive(
        store,
        sender,
        "que servicios ofrecen?",
        Turn("", (_u("get_service", {"linea": "cursos"}),), "tool_use"),
        Turn("Tenemos el Curso de edición (480 Bs) y Producción musical 🙂", (), "end_turn"),
    )
    stages.append(store.conv.stage)

    # 3) elige una categoría → recibe el PDF de la categoría + avanza a qualifying (#235)
    await _drive(
        store,
        sender,
        "me interesan los cursos, mandame info",
        Turn(
            "",
            (
                _u("enviar_material", {"categoria_slug": "cursos"}),
                _u("set_lead_stage", {"etapa": "qualifying", "razon": "interes"}),
            ),
            "tool_use",
        ),
        Turn("Te paso la info de los cursos 🙌", (), "end_turn"),
    )
    stages.append(store.conv.stage)
    assert sender.documents == ["curso-edicion.pdf"]  # PDF de la categoría correcta

    # 4) quiere inscribirse → qualified
    await _drive(
        store,
        sender,
        "quiero inscribirme",
        Turn(
            "",
            (_u("set_lead_stage", {"etapa": "qualified", "razon": "inscribe"}),),
            "tool_use",
        ),
        Turn(
            "Buenisimo! Acá está el QR de pago, cuando pagues mandame el comprobante",
            (),
            "end_turn",
        ),
    )
    stages.append(store.conv.stage)

    # 5) declara pago → se le pide el comprobante, deriva a Gestión Humana (#89, #84)
    await _drive(
        store,
        sender,
        "ya pague, te paso el comprobante",
        Turn("", (_u("handoff_to_human", {"motivo": "payment_validation"}),), "tool_use"),
    )
    stages.append(store.conv.stage)

    # Todos los stages se movieron, en orden, sin huérfanos.
    assert stages == [
        FunnelStage.ENGAGING,
        FunnelStage.ENGAGING,
        FunnelStage.QUALIFYING,
        FunnelStage.QUALIFIED,
        FunnelStage.HANDED_OFF,
    ]
    assert store.conv.is_ai_active is False  # agente silenciado tras el handoff de pago
    assert "comprobante" in sender.texts[-1].lower()  # pide el comprobante (no genérico)
    assert all("te conecto con mirko" not in t.lower() for t in sender.texts)


# ------------------- Fases B y C: CRM con DB (SQLite) -------------------


class _StubImageSender:
    def __init__(self) -> None:
        self.images: list[str] = []

    async def send_image(self, to: str, link: str, caption: str = "") -> None:
        self.images.append(link)


class _StubPublisher:
    async def publish(self, channel: str, message: str) -> None:
        return None


async def _seed_human_chain(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    funnel_stage: FunnelStage,
    is_ai_active: bool,
    stage_name: str,
    stage_status: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Devuelve (org_id, card_id, entrada_enviada_stage_id)."""
    org_id = uuid.uuid4()
    async with session_factory() as session:
        for code, name in (("open", "Abierto"), ("won", "Ganado"), ("lost", "Perdido")):
            if await session.get(StageStatus, code) is None:
                session.add(StageStatus(code=code, name=name))
        if await session.get(Product, "cursos-mirko") is None:
            session.add(Product(slug="cursos-mirko", display_name="Cursos Mirko"))
        agent = Agent(
            organization_id=org_id,
            product_slug="cursos-mirko",
            display_name="Asistente",
            system_prompt="x",
            model="claude-haiku-4-5-20251001",
        )
        session.add(agent)
        await session.flush()
        instance = AgentInstance(agent_id=agent.id, display_name="WA")
        session.add(instance)
        await session.flush()
        conversation = Conversation(
            instance_id=instance.id,
            organization_id=org_id,
            external_id=LEAD_WA_ID,
            funnel_stage=funnel_stage,
            is_ai_active=is_ai_active,
        )
        session.add(conversation)
        pipeline = Pipeline(organization_id=org_id, kind="human", name="Gestión Humana", position=1)
        session.add(pipeline)
        await session.flush()
        validated = Stage(
            pipeline_id=pipeline.id,
            name=stage_names.PAYMENT_VALIDATED,
            position=1,
            status_code="open",
        )
        sent = Stage(
            pipeline_id=pipeline.id, name=stage_names.DELIVERED, position=2, status_code="open"
        )
        closed = Stage(
            pipeline_id=pipeline.id, name=stage_names.CLOSED, position=3, status_code="won"
        )
        session.add_all([validated, sent, closed])
        await session.flush()
        stage_id = {
            stage_names.PAYMENT_VALIDATED: validated.id,
            stage_names.CLOSED: closed.id,
        }[stage_name]
        card = Card(
            organization_id=org_id,
            conversation_id=conversation.id,
            stage_id=stage_id,
            title="Lead",
        )
        session.add(card)
        await session.flush()
        # La entrada solo existe para un servicio presencial (#263): sin esto, generarla
        # se rechaza — que es justamente el default seguro que se busca.
        service = Service(
            organization_id=org_id,
            agent_id=agent.id,
            slug="curso-edicion",
            nombre="Curso de edición",
            resumen="r",
            precio="480",
            moneda="BOB",
            flujo_cierre="pago_qr",
            modality="presencial",
        )
        session.add(service)
        await session.flush()
        session.add(
            CardService(
                organization_id=org_id,
                card_id=card.id,
                service_id=service.id,
                source="captured",
            )
        )
        # La entrada se emite para un evento concreto (#276).
        session.add(
            Event(
                organization_id=org_id,
                service_id=service.id,
                nombre="Edición Santa Cruz",
                starts_at=datetime.now(UTC) + timedelta(days=14),
                location="Sede central",
                status="active",
            )
        )
        await session.commit()
        return org_id, card.id, sent.id


async def test_e2e_tier0_operator_generates_entry_and_lead_reopens(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qr_image, "save_qr", lambda org_id, token: f"qr/{org_id}/{token}.png")

    # Fase B — operador valida el pago y genera la entrada (#90)
    org_id, card_id, sent_stage_id = await _seed_human_chain(
        session_factory,
        funnel_stage=FunnelStage.HANDED_OFF,
        is_ai_active=False,
        stage_name=stage_names.PAYMENT_VALIDATED,
        stage_status="open",
    )
    img_sender = _StubImageSender()
    async with session_factory() as session:
        svc = EntryService(session=session, publisher=_StubPublisher())
        svc._sender = img_sender
        await svc.generate_entry(card_id, uuid.uuid4(), org_id)
    assert len(img_sender.images) == 1  # QR enviado al lead
    async with session_factory() as session:
        card = await session.get(Card, card_id)
        assert card is not None
        assert card.stage_id == sent_stage_id  # card en "Entregado"

    # Fase C — el lead cerrado reescribe → conversación/oportunidad nueva (#76, #163)
    org2, card2, _ = await _seed_human_chain(
        session_factory,
        funnel_stage=FunnelStage.HANDED_OFF,
        is_ai_active=False,
        stage_name=stage_names.CLOSED,
        stage_status="won",
    )
    async with session_factory() as session:
        old_card = await session.get(Card, card2)
        assert old_card is not None
        old_conv = await session.get(Conversation, old_card.conversation_id)
        assert old_conv is not None
        old_conv.closed_at = datetime.now(UTC)  # cierre (lo setea el hook won, #163)
        await session.commit()
        instance = await session.get(AgentInstance, old_conv.instance_id)
        assert instance is not None
        # El lead cerrado vuelve a escribir → el webhook abre una conversación NUEVA.
        service = WhatsAppWebhookService(session, dispatcher=None)  # type: ignore[arg-type]
        new_conv = await service._get_or_create_conversation(instance, org2, LEAD_WA_ID)
        assert new_conv.id != old_conv.id  # oportunidad nueva, no la cerrada
        assert new_conv.closed_at is None
        assert new_conv.funnel_stage is FunnelStage.NEW  # Gestión IA > Nuevo
        assert new_conv.is_ai_active is True
    async with session_factory() as session:
        # La oportunidad anterior queda intacta como histórico.
        closed = await session.get(Conversation, old_conv.id)
        assert closed is not None and closed.closed_at is not None
        assert (await session.get(Card, card2)) is not None  # card sigue en "Cerrado"
