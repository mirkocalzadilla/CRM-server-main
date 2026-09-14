"""Entrega automática al validarse el pago (server#270, CR2).

Cubre los casos 1, 2, 19, 20, 21-bis, 30 y 31 de la matriz del handoff: happy path
presencial y virtual, lead sin nombre, virtual sin links, servicio sin modalidad,
ventana de 24h cerrada y fallo de envío a Meta.

El criterio que se está fijando: **el lead nunca recibe algo a medias, y una entrega ya
pagada no se retiene por un dato administrativo**.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.agent.domain.catalog_models import Service, ServiceLink
from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.modules.agent.domain.models import (
    Agent,
    AgentInstance,
    AiChatHistory,
    Contact,
    Conversation,
    Product,
)
from server.modules.crm.domain import card_flags, stages
from server.modules.crm.domain.delivery import PAYMENT_CONFIRMED, PAYMENT_CONFIRMED_PENDING
from server.modules.crm.domain.event_models import Event
from server.modules.crm.domain.models import Card, CardService, QrEntry, Stage
from server.modules.crm.repositories.board_repository import BoardRepository
from server.modules.crm.seed import seed_crm
from server.modules.crm.services import qr_image
from server.modules.crm.services.delivery_notice import PAYMENT_CONFIRMED_KIND
from server.modules.crm.services.fulfillment_service import DeliveryOutcome, FulfillmentService
from server.shared.exceptions import ExternalServiceError, OutsideWindowError

SessionFactory = async_sessionmaker[AsyncSession]
LEAD_WA_ID = "59170000123"


class _NoopPublisher:
    async def publish(self, channel: str, message: object) -> None:
        return None


class _RecordingSender:
    """Sender que registra lo enviado, con fallos programables."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.texts: list[tuple[str, str]] = []
        self.images: list[tuple[str, str, str]] = []
        self._fail = fail

    async def send_text(self, to: str, body: str) -> None:
        if self._fail is not None:
            raise self._fail
        self.texts.append((to, body))

    async def send_image(self, to: str, link: str, caption: str = "") -> None:
        if self._fail is not None:
            raise self._fail
        self.images.append((to, link, caption))

    async def send_document(self, to: str, link: str, filename: str, caption: str = "") -> None:
        raise AssertionError("la entrega no manda documentos")


@pytest.fixture(autouse=True)
def _no_qr_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """No escribir PNGs reales en disco durante los tests."""
    monkeypatch.setattr(qr_image, "save_qr", lambda org_id, token: f"qr/{org_id}/{token}.png")


async def _seed(
    session_factory: SessionFactory,
    *,
    modality: str | None,
    links: list[tuple[str, str]] | None = None,
    stage_name: str = stages.PAYMENT_VALIDATED,
    lead_name: str | None = "Lead Test",
    services: int = 1,
    with_event: bool = True,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Devuelve (org_id, card_id) con la card lista para entregar."""
    org_id = uuid.uuid4()
    async with session_factory() as session:
        await seed_crm(session, org_id)
        if await session.get(Product, "cursos-mirko") is None:
            session.add(Product(slug="cursos-mirko", display_name="Cursos"))
            await session.flush()
        agent = Agent(
            organization_id=org_id,
            product_slug="cursos-mirko",
            display_name="Agente",
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
            funnel_stage=FunnelStage.HANDED_OFF,
            is_ai_active=False,
            full_name=lead_name,
        )
        session.add(conversation)
        await session.flush()
        stage = await BoardRepository(session).get_stage(org_id, stages.PIPELINE_HUMAN, stage_name)
        assert stage is not None
        card = Card(
            organization_id=org_id,
            conversation_id=conversation.id,
            stage_id=stage.id,
            title="Lead Test",
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
                price_amount=Decimal("650.00"),
            )
            session.add(service)
            await session.flush()
            for kind, url in links or []:
                session.add(
                    ServiceLink(organization_id=org_id, service_id=service.id, kind=kind, url=url)
                )
            session.add(
                CardService(
                    organization_id=org_id,
                    card_id=card.id,
                    service_id=service.id,
                    source="captured",
                )
            )
            # Anything that issues an entry needs an event to bind it to (#276): without
            # one the delivery does not complete, which is the case test_events covers.
            # Hybrid issues an entry too, so it needs one just like presencial.
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
        return org_id, card.id


async def _deliver(
    session_factory: SessionFactory, org_id: uuid.UUID, card_id: uuid.UUID, sender: _RecordingSender
) -> DeliveryOutcome:
    async with session_factory() as session:
        service = FulfillmentService(session=session, publisher=_NoopPublisher(), sender=sender)
        return await service.deliver(card_id, org_id)


async def _stage_name(session_factory: SessionFactory, card_id: uuid.UUID) -> str:
    async with session_factory() as session:
        card = await session.get(Card, card_id)
        assert card is not None
        stage = await session.get(Stage, card.stage_id)
        assert stage is not None
        return stage.name


async def _flags(session_factory: SessionFactory, card_id: uuid.UUID) -> list[str]:
    async with session_factory() as session:
        card = await session.get(Card, card_id)
        assert card is not None
        return card_flags.normalize(card.flags)


# --------------------------- caso 1: happy presencial ---------------------------


async def test_presencial_delivers_entry_with_location_and_closes(
    session_factory: SessionFactory,
) -> None:
    org_id, card_id = await _seed(
        session_factory,
        modality="presencial",
        links=[("maps", "https://maps.app.goo.gl/sede")],
    )
    sender = _RecordingSender()
    outcome = await _deliver(session_factory, org_id, card_id, sender)

    assert outcome.delivered is True
    assert outcome.closed is True
    assert len(sender.images) == 1  # la entrada, en un solo mensaje
    assert sender.texts == []
    to, _link, caption = sender.images[0]
    assert to == LEAD_WA_ID
    assert "maps.app.goo.gl/sede" in caption  # la ubicación viaja con la entrada
    # server#290: confirmación del pago + los datos clave del evento, en el mismo mensaje.
    assert caption.startswith(PAYMENT_CONFIRMED)
    assert "Fecha:" in caption and "Hora:" in caption
    assert "Modalidad: presencial" in caption
    assert "Lugar: Sede" in caption
    assert await _stage_name(session_factory, card_id) == stages.CLOSED
    assert await _flags(session_factory, card_id) == []

    async with session_factory() as session:
        entries = (await session.execute(select(QrEntry))).scalars().all()
        assert len(entries) == 1  # una entrada, con su token
        # El envío quedó espejado en el hilo para que el operador vea lo que recibió.
        mirrored = (await session.execute(select(AiChatHistory))).scalars().all()
        assert any(row.message.get("media_type") == "image" for row in mirrored)
        # El cierre creó el contacto (hook won).
        contacts = (await session.execute(select(Contact))).scalars().all()
        assert len(contacts) == 1


async def test_presencial_without_maps_still_delivers_and_warns(
    session_factory: SessionFactory,
) -> None:
    """La entrada es la entrega; la ubicación es complemento."""
    org_id, card_id = await _seed(session_factory, modality="presencial")
    sender = _RecordingSender()
    outcome = await _deliver(session_factory, org_id, card_id, sender)

    assert outcome.delivered is True
    assert len(sender.images) == 1
    assert await _stage_name(session_factory, card_id) == stages.CLOSED
    assert card_flags.MISSING_LINK in await _flags(session_factory, card_id)


# --------------------------- caso 2: happy virtual ---------------------------


async def test_virtual_delivers_links_and_closes_without_entry(
    session_factory: SessionFactory,
) -> None:
    org_id, card_id = await _seed(
        session_factory,
        modality="virtual",
        links=[
            ("whatsapp_group", "https://chat.whatsapp.com/grupo"),
            ("meeting", "https://meet.google.com/abc-defg-hij"),
        ],
    )
    sender = _RecordingSender()
    outcome = await _deliver(session_factory, org_id, card_id, sender)

    assert outcome.delivered is True
    assert outcome.closed is True
    assert sender.images == []  # sin entrada QR
    assert len(sender.texts) == 1
    _, body = sender.texts[0]
    assert "chat.whatsapp.com/grupo" in body
    assert "meet.google.com/abc-defg-hij" in body
    assert await _stage_name(session_factory, card_id) == stages.CLOSED

    async with session_factory() as session:
        assert (await session.execute(select(QrEntry))).scalars().all() == []
        mirrored = (await session.execute(select(AiChatHistory))).scalars().all()
        assert any("chat.whatsapp.com/grupo" in str(row.message.get("content")) for row in mirrored)


# --------------------------- caso 19: lead sin nombre ---------------------------


async def test_delivers_even_without_lead_name_and_flags_it(
    session_factory: SessionFactory,
) -> None:
    """Nunca se retiene una entrega ya pagada por un dato administrativo (#241)."""
    org_id, card_id = await _seed(
        session_factory,
        modality="presencial",
        links=[("maps", "https://maps.app/x")],
        lead_name=None,
    )
    sender = _RecordingSender()
    outcome = await _deliver(session_factory, org_id, card_id, sender)

    assert outcome.delivered is True  # se entregó
    assert outcome.closed is False  # pero no se cerró
    assert len(sender.images) == 1
    assert await _stage_name(session_factory, card_id) == stages.DELIVERED
    assert card_flags.NEEDS_NAME in await _flags(session_factory, card_id)
    async with session_factory() as session:
        assert (await session.execute(select(Contact))).scalars().all() == []


# --------------------------- casos 20 y 21-bis: falta config ---------------------------


async def test_virtual_without_links_goes_to_human_and_only_confirms_the_payment(
    session_factory: SessionFactory,
) -> None:
    org_id, card_id = await _seed(session_factory, modality="virtual")
    sender = _RecordingSender()
    outcome = await _deliver(session_factory, org_id, card_id, sender)

    assert outcome.delivered is False
    assert sender.images == []  # jamás un mensaje roto
    # Pero el pago sí está validado, y el lead lo sabe (server#290).
    assert sender.texts == [(LEAD_WA_ID, PAYMENT_CONFIRMED_PENDING)]
    assert await _stage_name(session_factory, card_id) == stages.PAYMENT_VALIDATED
    assert card_flags.MISSING_LINK in await _flags(session_factory, card_id)


async def test_service_without_modality_does_not_deliver(
    session_factory: SessionFactory,
) -> None:
    org_id, card_id = await _seed(session_factory, modality=None)
    sender = _RecordingSender()
    outcome = await _deliver(session_factory, org_id, card_id, sender)

    assert outcome.delivered is False
    assert sender.images == []
    assert sender.texts == [(LEAD_WA_ID, PAYMENT_CONFIRMED_PENDING)]
    assert await _stage_name(session_factory, card_id) == stages.PAYMENT_VALIDATED
    assert card_flags.NO_MODALITY in await _flags(session_factory, card_id)


async def test_blocked_delivery_confirms_the_payment_once_and_mirrors_it(
    session_factory: SessionFactory,
) -> None:
    """Dos intentos bloqueados (el operador carga el evento y se reintenta) no le dicen
    dos veces al lead que su pago está confirmado: el hilo lleva la marca."""
    org_id, card_id = await _seed(session_factory, modality="presencial", with_event=False)
    sender = _RecordingSender()

    await _deliver(session_factory, org_id, card_id, sender)
    await _deliver(session_factory, org_id, card_id, sender)

    assert sender.texts == [(LEAD_WA_ID, PAYMENT_CONFIRMED_PENDING)]
    assert card_flags.NO_EVENT in await _flags(session_factory, card_id)
    async with session_factory() as session:
        mirrored = (await session.execute(select(AiChatHistory))).scalars().all()
        notices = [row for row in mirrored if row.message.get("kind") == PAYMENT_CONFIRMED_KIND]
        assert len(notices) == 1
        assert notices[0].message["role"] == "assistant"  # el operador lo ve como del bot
        assert notices[0].message["content"] == PAYMENT_CONFIRMED_PENDING


async def test_failed_confirmation_notice_still_leaves_the_card_notice(
    session_factory: SessionFactory,
) -> None:
    """Meta caído o ventana cerrada: el aviso al lead es best-effort, el de la card no."""
    org_id, card_id = await _seed(session_factory, modality=None)
    sender = _RecordingSender(fail=OutsideWindowError("ventana cerrada"))
    outcome = await _deliver(session_factory, org_id, card_id, sender)

    assert outcome.delivered is False
    assert sender.texts == []
    assert card_flags.NO_MODALITY in await _flags(session_factory, card_id)
    async with session_factory() as session:
        mirrored = (await session.execute(select(AiChatHistory))).scalars().all()
        assert mirrored == []  # no se espeja lo que el lead no recibió


async def test_card_without_services_does_not_deliver(
    session_factory: SessionFactory,
) -> None:
    org_id, card_id = await _seed(session_factory, modality="presencial", services=0)
    sender = _RecordingSender()
    outcome = await _deliver(session_factory, org_id, card_id, sender)

    assert outcome.delivered is False
    assert card_flags.NO_MODALITY in await _flags(session_factory, card_id)


# --------------------------- caso 14: dos servicios ---------------------------


async def test_two_accepted_services_go_to_human(session_factory: SessionFactory) -> None:
    """Monto y entrega ambiguos: no se decide sola."""
    org_id, card_id = await _seed(session_factory, modality="presencial", services=2)
    sender = _RecordingSender()
    outcome = await _deliver(session_factory, org_id, card_id, sender)

    assert outcome.delivered is False
    assert sender.images == []
    assert card_flags.AMBIGUOUS_SERVICE in await _flags(session_factory, card_id)


# --------------------------- caso 30: ventana de 24h ---------------------------


async def test_outside_24h_window_leaves_delivery_pending(
    session_factory: SessionFactory,
) -> None:
    org_id, card_id = await _seed(
        session_factory, modality="virtual", links=[("whatsapp_group", "https://chat.w/x")]
    )
    sender = _RecordingSender(fail=OutsideWindowError("ventana cerrada"))
    outcome = await _deliver(session_factory, org_id, card_id, sender)

    assert outcome.delivered is False
    assert await _stage_name(session_factory, card_id) == stages.PAYMENT_VALIDATED
    assert card_flags.DELIVERY_PENDING in await _flags(session_factory, card_id)


async def test_retry_after_window_reopens_delivers_and_clears_the_flag(
    session_factory: SessionFactory,
) -> None:
    """El lead vuelve a escribir ⇒ la ventana se reabre ⇒ el reintento entrega."""
    org_id, card_id = await _seed(
        session_factory, modality="virtual", links=[("whatsapp_group", "https://chat.w/x")]
    )
    await _deliver(session_factory, org_id, card_id, _RecordingSender(fail=OutsideWindowError("x")))
    assert card_flags.DELIVERY_PENDING in await _flags(session_factory, card_id)

    sender = _RecordingSender()
    outcome = await _deliver(session_factory, org_id, card_id, sender)
    assert outcome.delivered is True
    assert len(sender.texts) == 1
    assert await _stage_name(session_factory, card_id) == stages.CLOSED
    assert card_flags.DELIVERY_PENDING not in await _flags(session_factory, card_id)


# --------------------------- caso 31: fallo de Meta ---------------------------


async def test_meta_failure_does_not_advance_the_stage(
    session_factory: SessionFactory,
) -> None:
    """Patrón #90: el stage avanza solo si el lead recibió algo."""
    org_id, card_id = await _seed(
        session_factory, modality="virtual", links=[("whatsapp_group", "https://chat.w/x")]
    )
    sender = _RecordingSender(fail=httpx.HTTPError("meta caído"))
    async with session_factory() as session:
        service = FulfillmentService(session=session, publisher=_NoopPublisher(), sender=sender)
        with pytest.raises(httpx.HTTPError):
            await service.deliver(card_id, org_id)
    assert await _stage_name(session_factory, card_id) == stages.PAYMENT_VALIDATED


async def test_meta_failure_on_entry_does_not_advance_the_stage(
    session_factory: SessionFactory,
) -> None:
    org_id, card_id = await _seed(
        session_factory, modality="presencial", links=[("maps", "https://maps.app/x")]
    )
    sender = _RecordingSender(fail=httpx.HTTPError("meta caído"))
    async with session_factory() as session:
        service = FulfillmentService(session=session, publisher=_NoopPublisher(), sender=sender)
        with pytest.raises(ExternalServiceError):
            await service.deliver(card_id, org_id)
    assert await _stage_name(session_factory, card_id) == stages.PAYMENT_VALIDATED


# --------------------------- idempotencia y precondición ---------------------------


async def test_delivering_twice_does_not_duplicate_anything(
    session_factory: SessionFactory,
) -> None:
    org_id, card_id = await _seed(
        session_factory, modality="presencial", links=[("maps", "https://maps.app/x")]
    )
    sender = _RecordingSender()
    await _deliver(session_factory, org_id, card_id, sender)
    outcome = await _deliver(session_factory, org_id, card_id, sender)

    # La segunda pasada no hace nada: la card ya no está en "Pago validado".
    assert outcome.delivered is False
    assert len(sender.images) == 1
    async with session_factory() as session:
        assert len((await session.execute(select(QrEntry))).scalars().all()) == 1


async def test_card_not_in_payment_validated_is_not_delivered(
    session_factory: SessionFactory,
) -> None:
    org_id, card_id = await _seed(
        session_factory,
        modality="presencial",
        links=[("maps", "https://maps.app/x")],
        stage_name=stages.PAYMENT_VALIDATION,
    )
    sender = _RecordingSender()
    outcome = await _deliver(session_factory, org_id, card_id, sender)
    assert outcome.delivered is False
    assert sender.images == []


async def test_presencial_without_an_event_does_not_deliver(
    session_factory: SessionFactory,
) -> None:
    """El gate de evento de #276 también aplica acá: una entrada sin fecha ni lugar no
    se puede validar en la puerta, así que no se emite."""
    org_id, card_id = await _seed(
        session_factory,
        modality="presencial",
        links=[("maps", "https://maps.app/x")],
        with_event=False,
    )
    sender = _RecordingSender()
    outcome = await _deliver(session_factory, org_id, card_id, sender)

    assert outcome.delivered is False
    assert sender.images == []
    assert card_flags.NO_EVENT in await _flags(session_factory, card_id)


async def test_pending_entry_does_not_block_itself_on_capacity(
    session_factory: SessionFactory,
) -> None:
    """Regresión: la entrada emitida-pero-no-enviada se contaba a sí misma contra el cupo.

    Secuencia real: el pago se valida fuera de la ventana de 24 h. La entrada ya quedó
    emitida (y commiteada) cuando el envío falló, así que el reintento volvía a evaluar el
    gate del evento y encontraba el cupo lleno — por su propia entrada, que reserva
    justamente el asiento que se le negaba. Peor: el aviso `capacity_full` reemplazaba a
    `delivery_pending`, y como ese es el único disparador del reintento, la entrega
    automática quedaba muerta. El lead pagó, tiene el asiento y no recibe nunca el QR.

    El camino manual (el botón del CRM) ya salteaba el gate cuando la entrada existía;
    esto le da al automático el mismo criterio.
    """
    org_id, card_id = await _seed(
        session_factory, modality="presencial", links=[("maps", "https://maps.app/x")]
    )
    async with session_factory() as session:
        event = (await session.execute(select(Event))).scalars().one()
        event.capacity = 1  # justo el asiento de este lead
        await session.commit()

    blocked = await _deliver(
        session_factory, org_id, card_id, _RecordingSender(fail=OutsideWindowError("ventana"))
    )
    assert blocked.delivered is False
    assert card_flags.DELIVERY_PENDING in await _flags(session_factory, card_id)
    async with session_factory() as session:
        assert (await session.execute(select(QrEntry))).scalars().one() is not None

    sender = _RecordingSender()
    retried = await _deliver(session_factory, org_id, card_id, sender)

    assert retried.delivered is True, "el reintento se bloqueó con su propia entrada"
    assert len(sender.images) == 1
    flags = await _flags(session_factory, card_id)
    assert card_flags.CAPACITY_FULL not in flags
    assert card_flags.DELIVERY_PENDING not in flags
    async with session_factory() as session:
        # Se reenvió la misma entrada, no se emitió otra.
        assert len((await session.execute(select(QrEntry))).scalars().all()) == 1


async def test_pending_entry_is_resent_even_if_its_event_passed(
    session_factory: SessionFactory,
) -> None:
    """La misma raíz sin tocar el cupo: el evento vence mientras la entrega está pendiente.

    Reevaluar el gate daba `no_event` y borraba `delivery_pending`. La entrada ya existe y
    guarda su fecha y lugar en el snapshot, así que reenviarla es lo correcto: el lead ya
    la tenía prometida.
    """
    org_id, card_id = await _seed(
        session_factory, modality="presencial", links=[("maps", "https://maps.app/x")]
    )

    await _deliver(
        session_factory, org_id, card_id, _RecordingSender(fail=OutsideWindowError("ventana"))
    )
    async with session_factory() as session:
        event = (await session.execute(select(Event))).scalars().one()
        event.starts_at = datetime.now(UTC) - timedelta(days=1)  # ya pasó
        await session.commit()

    sender = _RecordingSender()
    retried = await _deliver(session_factory, org_id, card_id, sender)

    assert retried.delivered is True
    assert len(sender.images) == 1
    assert card_flags.NO_EVENT not in await _flags(session_factory, card_id)


async def test_hybrid_delivers_entry_and_meeting_link_in_one_message(
    session_factory: SessionFactory,
) -> None:
    """The real shape of Mirko's course: one in-person module and three over Zoom.

    Neither `presencial` nor `virtual` delivers it whole — presencial ignores the meeting
    links, virtual issues no entry. This checks the lead gets both, in a single message,
    and that the event gate still applies because an entry is being issued.
    """
    org_id, card_id = await _seed(
        session_factory,
        modality="hibrido",
        links=[("maps", "https://maps.app/sede"), ("meeting", "https://zoom.us/j/777")],
    )
    sender = _RecordingSender()

    outcome = await _deliver(session_factory, org_id, card_id, sender)

    assert outcome.delivered is True
    # One message, and it is the QR image: the entry is what needs to exist at the door.
    assert len(sender.images) == 1
    assert sender.texts == []
    _to, _link, caption = sender.images[0]
    assert "módulo presencial" in caption
    assert "https://maps.app/sede" in caption
    assert "https://zoom.us/j/777" in caption
    assert card_flags.MISSING_LINK not in await _flags(session_factory, card_id)
    async with session_factory() as session:
        entry = (await session.execute(select(QrEntry))).scalars().one()
        # Linked to its event, like any presencial entry: the door validates against it.
        assert entry.event_id is not None


async def test_hybrid_without_an_event_does_not_deliver(
    session_factory: SessionFactory,
) -> None:
    """Hybrid issues an entry, so the event gate of #276 applies to it too."""
    org_id, card_id = await _seed(
        session_factory,
        modality="hibrido",
        links=[("meeting", "https://zoom.us/j/1")],
        with_event=False,
    )
    sender = _RecordingSender()

    outcome = await _deliver(session_factory, org_id, card_id, sender)

    assert outcome.delivered is False
    assert sender.images == []
    assert card_flags.NO_EVENT in await _flags(session_factory, card_id)


async def test_hybrid_without_the_meeting_link_still_delivers_and_warns(
    session_factory: SessionFactory,
) -> None:
    """Three of the four modules are over Zoom, but the entry is already paid for.

    Withholding it would leave the lead with nothing; the operator gets the notice and
    adds the link. Same asymmetry as a presencial course with no location.
    """
    org_id, card_id = await _seed(
        session_factory, modality="hibrido", links=[("maps", "https://maps.app/sede")]
    )
    sender = _RecordingSender()

    outcome = await _deliver(session_factory, org_id, card_id, sender)

    assert outcome.delivered is True
    assert len(sender.images) == 1
    assert card_flags.MISSING_LINK in outcome.flags
