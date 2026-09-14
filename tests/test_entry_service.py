"""Generación de entrada QR (#90): enviar al lead PRIMERO, mover el stage SOLO si el
envío fue exitoso. Seed mínimo del chain (conversation + pipeline human con stages
'Pago validado'→'Entregado' + card + servicio presencial) sobre SQLite; el envío se
stubea.

La entrada existe **solo para servicios presenciales** (#263): un curso virtual recibe
links y uno sin modalidad no se sabe qué entrega, así que el seed carga un servicio
presencial y hay casos explícitos para los que deben ser rechazados.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.agent.domain.catalog_models import Service
from server.modules.agent.domain.models import (
    Agent,
    AgentInstance,
    Conversation,
    Product,
)
from server.modules.crm.domain import stages
from server.modules.crm.domain.delivery import ENTRY_CAPTION, PAYMENT_CONFIRMED
from server.modules.crm.domain.event_models import Event
from server.modules.crm.domain.models import Card, CardService, Pipeline, Stage, StageStatus
from server.modules.crm.services import qr_image
from server.modules.crm.services.entry_service import EntryService
from server.shared.exceptions import ExternalServiceError, ValidationException

LEAD_WA_ID = "59170000000"
STAGE_ORIGIN = stages.PAYMENT_VALIDATED
STAGE_TARGET = stages.DELIVERED


class _StubPublisher:
    """Publisher no-op (evita Redis real en los tests)."""

    async def publish(self, channel: str, message: str) -> None:
        return None


class _StubSender:
    """Sender que registra los envíos de imagen; opcionalmente falla."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.images: list[tuple[str, str, str]] = []  # (to, link, caption)

    async def send_image(self, to: str, link: str, caption: str = "") -> None:
        if self.fail:
            raise RuntimeError("meta caído")
        self.images.append((to, link, caption))


async def _seed_card_in_origin(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    modality: str | None = "presencial",
    services: int = 1,
    with_event: bool = True,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Crea el chain mínimo. Devuelve (org_id, card_id, origin_stage_id, target_stage_id)."""
    org_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(StageStatus(code="open", name="Abierto"))
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
            is_ai_active=False,
        )
        session.add(conversation)
        pipeline = Pipeline(organization_id=org_id, kind="human", name="Gestión Humana", position=1)
        session.add(pipeline)
        await session.flush()
        origin = Stage(pipeline_id=pipeline.id, name=STAGE_ORIGIN, position=1, status_code="open")
        target = Stage(pipeline_id=pipeline.id, name=STAGE_TARGET, position=2, status_code="open")
        session.add_all([origin, target])
        await session.flush()
        card = Card(
            organization_id=org_id,
            conversation_id=conversation.id,
            stage_id=origin.id,
            title="Lead",
        )
        session.add(card)
        await session.flush()
        for index in range(services):
            service = Service(
                organization_id=org_id,
                agent_id=agent.id,
                slug=f"curso-{index}",
                nombre=f"Curso {index}",
                resumen="r",
                precio="650",
                moneda="BOB",
                flujo_cierre="pago_qr",
                modality=modality,
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
            # Un presencial (o híbrido) necesita evento al que ligar la entrada (#276).
            # Los casos de rechazo por falta de evento viven en test_events.
            if with_event and modality in ("presencial", "hibrido"):
                session.add(
                    Event(
                        organization_id=org_id,
                        service_id=service.id,
                        nombre="Edición de prueba",
                        starts_at=datetime.now(UTC) + timedelta(days=7),
                        location="Sede",
                        status="active",
                    )
                )
        await session.commit()
        return org_id, card.id, origin.id, target.id


def _service(session: AsyncSession, sender: _StubSender) -> EntryService:
    svc = EntryService(session=session, publisher=_StubPublisher())
    svc._sender = sender  # inyección del canal de salida para el test
    return svc


@pytest.fixture(autouse=True)
def _no_qr_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Evita escribir el PNG real en disco; el QR en sí no es lo que se prueba acá."""
    monkeypatch.setattr(qr_image, "save_qr", lambda org_id, token: f"qr/{org_id}/{token}.png")


async def test_generate_entry_sends_qr_then_moves_stage(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id, card_id, _origin_id, target_id = await _seed_card_in_origin(session_factory)
    sender = _StubSender()
    async with session_factory() as session:
        entry = await _service(session, sender).generate_entry(card_id, uuid.uuid4(), org_id)
        assert entry.card_id == card_id
    # T1: se envió la entrada/QR al lead.
    assert len(sender.images) == 1
    assert sender.images[0][0] == LEAD_WA_ID
    # T2: la card pasó a "Entrada enviada".
    async with session_factory() as session:
        card = await session.get(Card, card_id)
        assert card is not None
        assert card.stage_id == target_id


async def test_generate_entry_sends_farewell_with_qr(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # #92: la despedida viaja JUNTO con la entrada, en el mismo mensaje (un solo envío,
    # como caption del QR; no se duplica con un texto aparte).
    org_id, card_id, _origin_id, _target_id = await _seed_card_in_origin(session_factory)
    sender = _StubSender()
    async with session_factory() as session:
        await _service(session, sender).generate_entry(card_id, uuid.uuid4(), org_id)
    assert len(sender.images) == 1  # un único mensaje (entrada + despedida)
    assert sender.images[0][2].startswith(ENTRY_CAPTION)
    assert "esperamos" in ENTRY_CAPTION.lower()


async def test_generate_entry_sends_the_same_message_as_the_automatic_delivery(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """server#290: el botón pasa por el mismo plan que el fulfillment, así que el lead
    recibe la confirmación del pago con fecha, hora, modalidad y lugar del evento."""
    org_id, card_id, _origin_id, _target_id = await _seed_card_in_origin(session_factory)
    sender = _StubSender()
    async with session_factory() as session:
        await _service(session, sender).generate_entry(card_id, uuid.uuid4(), org_id)
    caption = sender.images[0][2]
    assert caption.startswith(PAYMENT_CONFIRMED)
    assert "Fecha:" in caption and "Hora:" in caption
    assert "Modalidad: presencial" in caption
    assert "Lugar: Sede" in caption


async def test_generate_entry_for_hybrid_uses_the_hybrid_copy(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Antes el botón mandaba el caption presencial pelado a un híbrido: sin ubicación
    ni links de Zoom, menos que lo que entregaba el camino automático."""
    org_id, card_id, _origin_id, _target_id = await _seed_card_in_origin(
        session_factory, modality="hibrido"
    )
    sender = _StubSender()
    async with session_factory() as session:
        await _service(session, sender).generate_entry(card_id, uuid.uuid4(), org_id)
    caption = sender.images[0][2]
    assert "módulo presencial" in caption
    assert "Modalidad: presencial + virtual (Zoom)" in caption


async def test_generate_entry_does_not_move_stage_if_send_fails(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # #90 T3: si el envío falla, el stage NO avanza (sin falso positivo) y no queda
    # una QrEntry huérfana.
    org_id, card_id, origin_id, _target_id = await _seed_card_in_origin(session_factory)
    sender = _StubSender(fail=True)
    async with session_factory() as session:
        with pytest.raises(ExternalServiceError):
            await _service(session, sender).generate_entry(card_id, uuid.uuid4(), org_id)
    async with session_factory() as session:
        card = await session.get(Card, card_id)
        assert card is not None
        assert card.stage_id == origin_id  # sigue en "Pago validado"


async def test_generate_entry_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id, card_id, _origin_id, target_id = await _seed_card_in_origin(session_factory)
    sender = _StubSender()
    async with session_factory() as session:
        first = await _service(session, sender).generate_entry(card_id, uuid.uuid4(), org_id)
    async with session_factory() as session:
        second = await _service(session, sender).generate_entry(card_id, uuid.uuid4(), org_id)
    assert first.token == second.token  # misma entrada
    assert len(sender.images) == 1  # no se reenvía si ya está en "Entrada enviada"
    async with session_factory() as session:
        card = await session.get(Card, card_id)
        assert card is not None
        assert card.stage_id == target_id


async def test_generate_entry_rejected_for_virtual_service(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """#263: un curso virtual recibe links, no una entrada. Antes se le mandaba un QR
    inútil a cualquiera que llegara a "Pago validado"."""
    org_id, card_id, origin_id, _target_id = await _seed_card_in_origin(
        session_factory, modality="virtual"
    )
    sender = _StubSender()
    async with session_factory() as session:
        with pytest.raises(ValidationException):
            await _service(session, sender).generate_entry(card_id, uuid.uuid4(), org_id)
    assert sender.images == []
    async with session_factory() as session:
        card = await session.get(Card, card_id)
        assert card is not None and card.stage_id == origin_id


async def test_generate_entry_rejected_without_modality(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Default seguro: sin modalidad cargada no se genera nada."""
    org_id, card_id, _origin_id, _target_id = await _seed_card_in_origin(
        session_factory, modality=None
    )
    sender = _StubSender()
    async with session_factory() as session:
        with pytest.raises(ValidationException):
            await _service(session, sender).generate_entry(card_id, uuid.uuid4(), org_id)
    assert sender.images == []


async def test_generate_entry_rejected_without_service(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id, card_id, _origin_id, _target_id = await _seed_card_in_origin(
        session_factory, services=0
    )
    sender = _StubSender()
    async with session_factory() as session:
        with pytest.raises(ValidationException):
            await _service(session, sender).generate_entry(card_id, uuid.uuid4(), org_id)
    assert sender.images == []


async def test_generate_entry_rejected_with_two_services(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Con dos servicios aceptados no se puede saber qué entrada generar."""
    org_id, card_id, _origin_id, _target_id = await _seed_card_in_origin(
        session_factory, services=2
    )
    sender = _StubSender()
    async with session_factory() as session:
        with pytest.raises(ValidationException):
            await _service(session, sender).generate_entry(card_id, uuid.uuid4(), org_id)
    assert sender.images == []


async def test_generate_entry_requires_an_event(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """El botón del CRM no puede saltear el gate del evento (#276): una entrada sin
    fecha ni lugar no valida nada en la puerta."""
    org_id, card_id, origin_id, _target = await _seed_card_in_origin(
        session_factory, with_event=False
    )
    sender = _StubSender()
    async with session_factory() as session:
        with pytest.raises(ValidationException):
            await _service(session, sender).generate_entry(card_id, uuid.uuid4(), org_id)
    assert sender.images == []
    async with session_factory() as session:
        card = await session.get(Card, card_id)
        assert card is not None and card.stage_id == origin_id
