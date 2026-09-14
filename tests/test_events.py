"""Eventos y entrada ligada al evento (server#276, CR5).

Cubre los casos **21** (presencial sin evento activo) y **22** (cupo lleno) de la matriz,
más el ABM, la resolución del evento vigente y la proyección al snapshot del agente.

El criterio: una entrada existe **para un evento concreto**. Sin fecha ni lugar no le
sirve al lead ni se puede validar en la puerta; y con el cupo lleno, emitirla sería
mandar a alguien con su QR válido a un lugar donde no hay asiento.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from server.modules.agent.domain.catalog_models import Service, ServiceLink
from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.modules.agent.domain.models import Agent, AgentInstance, Conversation, Product
from server.modules.core.services.tenant_service import TenantService
from server.modules.crm.domain import card_flags, stages
from server.modules.crm.domain.event_models import Event
from server.modules.crm.domain.models import Card, CardService, QrEntry, Stage
from server.modules.crm.repositories.board_repository import BoardRepository
from server.modules.crm.seed import seed_crm
from server.modules.crm.services import delivery_trigger, qr_image
from server.modules.crm.services.entry_service import EntryService
from server.modules.crm.services.event_service import EventService
from server.modules.crm.services.fulfillment_service import FulfillmentService

from .test_catalog import (
    API,
    STAFF_EMAIL,
    STAFF_PASSWORD,
    SessionFactory,
    _auth,
    _login,
    _operator_token,
    _seed_staff_member,
)

LEAD_WA_ID = "59170000333"
SOON = datetime(2026, 9, 10, 19, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_qr_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """No escribir PNGs reales: lo que se prueba acá es el evento, no la imagen."""
    monkeypatch.setattr(qr_image, "save_qr", lambda org_id, token: f"qr/{org_id}/{token}.png")


class _NoopPublisher:
    async def publish(self, channel: str, message: object) -> None:
        return None


class _RecordingSender:
    def __init__(self) -> None:
        self.images: list[tuple[str, str, str]] = []
        self.texts: list[tuple[str, str]] = []

    async def send_text(self, to: str, body: str) -> None:
        self.texts.append((to, body))

    async def send_image(self, to: str, link: str, caption: str = "") -> None:
        self.images.append((to, link, caption))

    async def send_document(self, to: str, link: str, filename: str, caption: str = "") -> None:
        raise AssertionError("la entrega no manda documentos")


async def _seed(
    session_factory: SessionFactory,
    *,
    modality: str | None = "presencial",
    service_maps: str | None = None,
    stage_name: str = stages.PAYMENT_VALIDATED,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Devuelve (org_id, service_id, card_id) en el tenant del operador."""
    async with session_factory() as session:
        tenant = await TenantService(session).get_by_slug("acme")
        org_id = tenant.id
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
            full_name="Lead Test",
        )
        session.add(conversation)
        await session.flush()
        service = Service(
            organization_id=org_id,
            agent_id=agent.id,
            slug="curso-presencial",
            nombre="Curso presencial",
            resumen="r",
            precio="650",
            moneda="BOB",
            flujo_cierre="pago_qr",
            modality=modality,
            price_amount=Decimal("650.00"),
        )
        session.add(service)
        await session.flush()
        if service_maps is not None:
            session.add(
                ServiceLink(
                    organization_id=org_id, service_id=service.id, kind="maps", url=service_maps
                )
            )
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
        session.add(
            CardService(
                organization_id=org_id,
                card_id=card.id,
                service_id=service.id,
                source="captured",
            )
        )
        await session.commit()
        return org_id, service.id, card.id


async def _add_event(
    session_factory: SessionFactory,
    org_id: uuid.UUID,
    service_id: uuid.UUID,
    *,
    starts_at: datetime = SOON,
    capacity: int | None = None,
    status: str = "scheduled",
    maps_url: str | None = None,
    issued: int = 0,
) -> uuid.UUID:
    async with session_factory() as session:
        event = Event(
            organization_id=org_id,
            service_id=service_id,
            nombre="Edición septiembre",
            starts_at=starts_at,
            location="Av. Siempre Viva 742",
            maps_url=maps_url,
            capacity=capacity,
            status=status,
        )
        session.add(event)
        await session.flush()
        instance = (await session.execute(select(AgentInstance))).scalars().first()
        assert instance is not None
        stage = (await session.execute(select(Stage))).scalars().first()
        assert stage is not None
        for index in range(issued):
            # Entradas de OTRAS cards que ya ocuparon lugar. Cada card necesita su propia
            # conversación: `card.conversation_id` es UNIQUE (1 card por conversación).
            other_conversation = Conversation(
                instance_id=instance.id,
                organization_id=org_id,
                external_id=f"5917000{index:04d}",
                funnel_stage=FunnelStage.HANDED_OFF,
                is_ai_active=False,
            )
            session.add(other_conversation)
            await session.flush()
            other = Card(
                organization_id=org_id,
                conversation_id=other_conversation.id,
                stage_id=stage.id,
                title=f"Otro {index}",
            )
            session.add(other)
            await session.flush()
            session.add(
                QrEntry(
                    card_id=other.id,
                    token=str(uuid.uuid4()),
                    qr_ref="qr/x.png",
                    event_id=event.id,
                )
            )
        await session.commit()
        return event.id


async def _deliver(
    session_factory: SessionFactory, org_id: uuid.UUID, card_id: uuid.UUID, sender: _RecordingSender
) -> object:
    async with session_factory() as session:
        service = FulfillmentService(session=session, publisher=_NoopPublisher(), sender=sender)
        return await service.deliver(card_id, org_id)


async def _flags(session_factory: SessionFactory, card_id: uuid.UUID) -> list[str]:
    async with session_factory() as session:
        card = await session.get(Card, card_id)
        assert card is not None
        return card_flags.normalize(card.flags)


# --------------------------- caso 21: sin evento ---------------------------


async def test_presencial_without_an_event_does_not_deliver(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Una entrada sin fecha ni lugar no le sirve al lead ni se puede validar."""
    await _operator_token(client, session_factory)
    org_id, _service_id, card_id = await _seed(session_factory)
    sender = _RecordingSender()

    outcome = await _deliver(session_factory, org_id, card_id, sender)

    assert outcome.delivered is False
    assert sender.images == []  # no se emitió nada
    assert card_flags.NO_EVENT in await _flags(session_factory, card_id)
    async with session_factory() as session:
        assert (await session.execute(select(QrEntry))).scalars().all() == []


async def test_past_event_does_not_count_as_available(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Emitir una entrada para algo que ya pasó es peor que no emitirla: el lead se
    enteraría en la puerta."""
    await _operator_token(client, session_factory)
    org_id, service_id, card_id = await _seed(session_factory)
    await _add_event(
        session_factory, org_id, service_id, starts_at=datetime.now(UTC) - timedelta(days=1)
    )

    outcome = await _deliver(session_factory, org_id, card_id, _RecordingSender())
    assert outcome.delivered is False
    assert card_flags.NO_EVENT in await _flags(session_factory, card_id)


async def test_closed_event_does_not_count_as_available(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    await _operator_token(client, session_factory)
    org_id, service_id, card_id = await _seed(session_factory)
    await _add_event(session_factory, org_id, service_id, status="closed")

    outcome = await _deliver(session_factory, org_id, card_id, _RecordingSender())
    assert outcome.delivered is False
    assert card_flags.NO_EVENT in await _flags(session_factory, card_id)


# --------------------------- caso 22: cupo lleno ---------------------------


async def test_full_capacity_does_not_deliver(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Nunca una entrada de más en silencio: en la puerta habría alguien con su QR
    válido y sin asiento."""
    await _operator_token(client, session_factory)
    org_id, service_id, card_id = await _seed(session_factory)
    await _add_event(session_factory, org_id, service_id, capacity=2, issued=2)

    outcome = await _deliver(session_factory, org_id, card_id, _RecordingSender())

    assert outcome.delivered is False
    assert card_flags.CAPACITY_FULL in await _flags(session_factory, card_id)


async def test_capacity_with_room_left_delivers(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    await _operator_token(client, session_factory)
    org_id, service_id, card_id = await _seed(session_factory)
    await _add_event(session_factory, org_id, service_id, capacity=3, issued=2)

    outcome = await _deliver(session_factory, org_id, card_id, _RecordingSender())
    assert outcome.delivered is True


async def test_revoked_entries_free_their_seat(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """El pago detrás de una entrada revocada no se pudo confirmar: seguir reservándole
    cupo dejaría un asiento que nadie puede usar."""
    await _operator_token(client, session_factory)
    org_id, service_id, card_id = await _seed(session_factory)
    event_id = await _add_event(session_factory, org_id, service_id, capacity=2, issued=2)
    async with session_factory() as session:
        entry = (
            (await session.execute(select(QrEntry).where(QrEntry.event_id == event_id)))
            .scalars()
            .first()
        )
        assert entry is not None
        entry.revoked_at = datetime.now(UTC)
        await session.commit()

    outcome = await _deliver(session_factory, org_id, card_id, _RecordingSender())
    assert outcome.delivered is True


async def test_no_capacity_limit_never_blocks(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    await _operator_token(client, session_factory)
    org_id, service_id, card_id = await _seed(session_factory)
    await _add_event(session_factory, org_id, service_id, capacity=None, issued=50)

    outcome = await _deliver(session_factory, org_id, card_id, _RecordingSender())
    assert outcome.delivered is True


# --------------------------- entrada ligada al evento ---------------------------


async def test_entry_is_linked_to_the_event_with_a_snapshot(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    await _operator_token(client, session_factory)
    org_id, service_id, card_id = await _seed(session_factory)
    event_id = await _add_event(session_factory, org_id, service_id, status="active")

    await _deliver(session_factory, org_id, card_id, _RecordingSender())

    async with session_factory() as session:
        entry = (
            await session.execute(select(QrEntry).where(QrEntry.card_id == card_id))
        ).scalar_one()
        assert entry.event_id == event_id
        # El snapshot deja la fecha y el lugar legibles en la puerta aunque después
        # alguien edite o borre el evento.
        assert entry.event_snapshot["nombre"] == "Edición septiembre"
        assert entry.event_snapshot["location"] == "Av. Siempre Viva 742"


async def test_deleting_the_event_keeps_the_entry_as_legacy(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """El lead ya tiene el QR: borrar el evento no puede borrarle la entrada.

    Nota del entorno: la FK es `ON DELETE SET NULL`, pero SQLite no aplica FKs por
    defecto, así que acá se verifica lo que sí se puede — que la entrada **sobrevive** al
    borrado y que su snapshot sigue legible, que es lo que necesita el escáner. Que el
    `event_id` quede en NULL se verifica contra Postgres al aplicar la migración.
    """
    await _operator_token(client, session_factory)
    org_id, service_id, card_id = await _seed(session_factory)
    event_id = await _add_event(session_factory, org_id, service_id, status="active")
    await _deliver(session_factory, org_id, card_id, _RecordingSender())

    async with session_factory() as session:
        await EventService(session).delete_event(event_id, org_id)

    async with session_factory() as session:
        assert await session.get(Event, event_id) is None  # el evento se borró
        entry = (
            await session.execute(select(QrEntry).where(QrEntry.card_id == card_id))
        ).scalar_one()
        # La entrada sobrevive y sigue siendo legible en la puerta.
        assert entry.event_snapshot["nombre"] == "Edición septiembre"
        assert entry.event_snapshot["location"] == "Av. Siempre Viva 742"


async def test_event_maps_url_overrides_the_service_one(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """El curso puede ser el mismo y la sede cambiar de una edición a otra."""
    await _operator_token(client, session_factory)
    org_id, service_id, card_id = await _seed(
        session_factory, service_maps="https://maps.app/sede-vieja"
    )
    await _add_event(
        session_factory,
        org_id,
        service_id,
        status="active",
        maps_url="https://maps.app/sede-nueva",
    )
    sender = _RecordingSender()

    await _deliver(session_factory, org_id, card_id, sender)

    assert len(sender.images) == 1
    _, _link, caption = sender.images[0]
    assert "sede-nueva" in caption
    assert "sede-vieja" not in caption


async def test_service_maps_is_used_when_the_event_has_none(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    await _operator_token(client, session_factory)
    org_id, service_id, card_id = await _seed(
        session_factory, service_maps="https://maps.app/sede-del-servicio"
    )
    await _add_event(session_factory, org_id, service_id, status="active", maps_url=None)
    sender = _RecordingSender()

    await _deliver(session_factory, org_id, card_id, sender)
    assert "sede-del-servicio" in sender.images[0][2]


async def test_virtual_service_ignores_events(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Un curso virtual entrega links: no necesita evento ni lo mira."""
    await _operator_token(client, session_factory)
    org_id, service_id, card_id = await _seed(session_factory, modality="virtual")
    async with session_factory() as session:
        session.add(
            ServiceLink(
                organization_id=org_id,
                service_id=service_id,
                kind="whatsapp_group",
                url="https://chat.whatsapp.com/x",
            )
        )
        await session.commit()

    sender = _RecordingSender()
    outcome = await _deliver(session_factory, org_id, card_id, sender)

    assert outcome.delivered is True
    assert sender.images == []
    assert len(sender.texts) == 1
    async with session_factory() as session:
        assert (await session.execute(select(QrEntry))).scalars().all() == []


# --------------------------- ABM ---------------------------


async def test_event_abm(client: AsyncClient, session_factory: SessionFactory) -> None:
    token = await _operator_token(client, session_factory)
    _org, service_id, _card = await _seed(session_factory)

    created = await client.post(
        f"{API}/crm/agenda",
        json={
            "service_id": str(service_id),
            "nombre": "Edición octubre",
            "starts_at": "2026-10-05T19:00:00+00:00",
            "location": "Sede centro",
            "maps_url": "https://maps.app/centro",
            "capacity": 20,
        },
        headers=_auth(token),
    )
    assert created.status_code == 201, created.text
    event_id = created.json()["id"]
    assert created.json()["issued"] == 0  # el cupo arranca vacío

    listed = await client.get(f"{API}/crm/agenda", headers=_auth(token))
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    updated = await client.put(
        f"{API}/crm/agenda/{event_id}",
        json={"status": "active", "capacity": 25},
        headers=_auth(token),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["status"] == "active"
    assert updated.json()["capacity"] == 25
    assert updated.json()["nombre"] == "Edición octubre"  # no se pisa lo no enviado

    deleted = await client.delete(f"{API}/crm/agenda/{event_id}", headers=_auth(token))
    assert deleted.status_code == 204
    assert (await client.get(f"{API}/crm/agenda", headers=_auth(token))).json() == []


async def _flag_card(session_factory: SessionFactory, card_id: uuid.UUID, *flags: str) -> None:
    async with session_factory() as session:
        card = await session.get(Card, card_id)
        assert card is not None
        card.flags = list(flags)
        await session.commit()


def _inject_sender(monkeypatch: pytest.MonkeyPatch, sender: _RecordingSender) -> None:
    """El disparador arma el fulfillment con el sender real de WhatsApp; acá se
    intercepta la fábrica para registrar los envíos sin tocar Meta."""

    def build(**kwargs: object) -> FulfillmentService:
        return FulfillmentService(sender=sender, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(delivery_trigger, "FulfillmentService", build)


async def test_creating_the_missing_event_delivers_the_cards_waiting_for_it(
    client: AsyncClient, session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """server#290: la card quedó en "Pago validado" con `no_event`; cargar la fecha es lo
    que faltaba, así que la entrega sale en el mismo request sin apretar nada más."""
    token = await _operator_token(client, session_factory)
    _org, service_id, card_id = await _seed(session_factory)
    await _flag_card(session_factory, card_id, card_flags.NO_EVENT)
    sender = _RecordingSender()
    _inject_sender(monkeypatch, sender)

    created = await client.post(
        f"{API}/crm/agenda",
        json={
            "service_id": str(service_id),
            "nombre": "Edición octubre",
            "starts_at": "2026-10-05T19:00:00+00:00",
            "location": "Sede centro",
        },
        headers=_auth(token),
    )

    assert created.status_code == 201, created.text
    assert len(sender.images) == 1
    caption = sender.images[0][2]
    assert "Fecha: 05/10/2026" in caption and "Hora: 15:00" in caption  # 19:00 UTC → La Paz
    assert "Lugar: Sede centro" in caption
    assert card_flags.NO_EVENT not in await _flags(session_factory, card_id)
    async with session_factory() as session:
        card = await session.get(Card, card_id)
        assert card is not None
        stage = await session.get(Stage, card.stage_id)
        assert stage is not None and stage.name == stages.CLOSED


async def test_creating_an_event_leaves_cards_without_a_notice_alone(
    client: AsyncClient, session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Una card en "Pago validado" sin aviso no estaba esperando la fecha: no se toca."""
    token = await _operator_token(client, session_factory)
    _org, service_id, _card_id = await _seed(session_factory)
    sender = _RecordingSender()
    _inject_sender(monkeypatch, sender)

    created = await client.post(
        f"{API}/crm/agenda",
        json={"service_id": str(service_id), "nombre": "Edición", "starts_at": SOON.isoformat()},
        headers=_auth(token),
    )

    assert created.status_code == 201, created.text
    assert sender.images == [] and sender.texts == []


async def test_event_of_unknown_service_is_rejected(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    await _seed(session_factory)
    response = await client.post(
        f"{API}/crm/agenda",
        json={
            "service_id": str(uuid.uuid4()),
            "nombre": "Fantasma",
            "starts_at": "2026-10-05T19:00:00+00:00",
        },
        headers=_auth(token),
    )
    assert response.status_code == 422, response.text


async def test_invalid_event_status_is_rejected(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    _org, service_id, _card = await _seed(session_factory)
    response = await client.post(
        f"{API}/crm/agenda",
        json={
            "service_id": str(service_id),
            "nombre": "X",
            "starts_at": "2026-10-05T19:00:00+00:00",
            "status": "cancelado",
        },
        headers=_auth(token),
    )
    assert response.status_code == 422


async def test_event_maps_url_must_be_http(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    _org, service_id, _card = await _seed(session_factory)
    response = await client.post(
        f"{API}/crm/agenda",
        json={
            "service_id": str(service_id),
            "nombre": "X",
            "starts_at": "2026-10-05T19:00:00+00:00",
            "maps_url": "maps.app/sin-esquema",
        },
        headers=_auth(token),
    )
    assert response.status_code == 422


async def test_unknown_event_update_is_404(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    response = await client.put(
        f"{API}/crm/agenda/{uuid.uuid4()}", json={"status": "active"}, headers=_auth(token)
    )
    assert response.status_code == 404


# --------------------------- snapshot del agente ---------------------------


async def test_upcoming_events_reach_the_agent_snapshot(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """El bot tiene que poder contestar "cuándo es" y "dónde queda" (#201)."""
    from server.modules.agent.services.catalog_snapshot_service import CatalogSnapshotService

    token = await _operator_token(client, session_factory)
    org_id, service_id, _card = await _seed(session_factory)
    await _add_event(session_factory, org_id, service_id, status="active")
    assert token  # el seed necesita el tenant del operador

    async with session_factory() as session:
        await CatalogSnapshotService(session).sync_for_tenant(org_id)

    async with session_factory() as session:
        agent = (await session.execute(select(Agent))).scalars().first()
        assert agent is not None
        events = agent.config.get("events")
        assert isinstance(events, list) and len(events) == 1
        entry = events[0]
        assert isinstance(entry, dict)
        assert entry["service_slug"] == "curso-presencial"
        assert entry["lugar"] == "Av. Siempre Viva 742"
        assert "fecha" in entry
        # El cupo NO viaja: un "quedan 2 lugares" que el modelo repita mal es peor que
        # no decirlo.
        assert "capacity" not in entry and "cupo" not in entry


async def test_past_and_closed_events_do_not_reach_the_snapshot(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Un evento que ya pasó en el contexto solo sirve para que el bot ofrezca una
    fecha que no existe."""
    from server.modules.agent.services.catalog_snapshot_service import CatalogSnapshotService

    await _operator_token(client, session_factory)
    org_id, service_id, _card = await _seed(session_factory)
    await _add_event(
        session_factory, org_id, service_id, starts_at=datetime.now(UTC) - timedelta(days=2)
    )
    await _add_event(session_factory, org_id, service_id, status="closed")

    async with session_factory() as session:
        await CatalogSnapshotService(session).sync_for_tenant(org_id)

    async with session_factory() as session:
        agent = (await session.execute(select(Agent))).scalars().first()
        assert agent is not None
        assert agent.config.get("events") == []


# --------------------------- entrada manual ---------------------------


async def test_manual_entry_generation_also_requires_an_event(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """El botón del CRM no puede saltear el gate: una entrada sin evento no sirve
    igual, la haya pedido el sistema o una persona."""
    from server.shared.exceptions import ValidationException

    await _operator_token(client, session_factory)
    org_id, _service_id, card_id = await _seed(session_factory)

    async with session_factory() as session:
        svc = EntryService(session=session, publisher=_NoopPublisher(), sender=_RecordingSender())
        with pytest.raises(ValidationException):
            await svc.generate_entry(card_id, uuid.uuid4(), org_id)


async def test_staff_can_list_events_but_not_change_them(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """RBAC asimétrico, y es lo que hace usable al escáner.

    Quien atiende la puerta es `staff` y necesita **elegir el evento** que está
    controlando: sin eso el escáner nunca manda `event_id` y el rechazo por "esa entrada es
    de otra fecha" queda inalcanzable desde la única pantalla que lo usa. Crear o editar un
    evento, en cambio, es configuración del negocio y sigue cerrado.
    """
    await _operator_token(client, session_factory)
    await _seed_staff_member(session_factory)
    staff = _auth(await _login(client, STAFF_EMAIL, STAFF_PASSWORD))
    _org, service_id, _card = await _seed(session_factory)

    listed = await client.get(f"{API}/crm/agenda", headers=staff)
    created = await client.post(
        f"{API}/crm/agenda",
        json={
            "service_id": str(service_id),
            "nombre": "Edición nueva",
            "starts_at": (datetime.now(UTC) + timedelta(days=5)).isoformat(),
        },
        headers=staff,
    )

    assert listed.status_code == 200, listed.text
    assert created.status_code == 403


async def test_creating_an_event_makes_it_visible_to_the_bot(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Regresión: el ABM no re-proyectaba el snapshot, así que el bot no veía la agenda.

    Los eventos viajan al contexto del agente a propósito — la fecha de un curso es lo
    primero que pregunta un lead, antes de pagar. Pero el snapshot solo se reconstruía al
    tocar el **catálogo**: cargar los eventos (que es un paso del checklist de deploy) no
    los hacía visibles hasta que alguien editara algún servicio, y el bot habría dicho que
    no tiene fechas el primer día de uso.
    """
    token = await _operator_token(client, session_factory)
    _org, service_id, _card = await _seed(session_factory)

    response = await client.post(
        f"{API}/crm/agenda",
        json={
            "service_id": str(service_id),
            "nombre": "Edición octubre",
            "starts_at": (datetime.now(UTC) + timedelta(days=20)).isoformat(),
            "location": "Sede sur",
        },
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text

    async with session_factory() as session:
        agent = (await session.execute(select(Agent))).scalars().one()
        projected = agent.config.get("events") or []
        assert [e["nombre"] for e in projected] == ["Edición octubre"]


async def test_deleting_an_event_removes_it_from_the_bot_context(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """La otra dirección: un evento borrado no puede seguir ofreciéndose al lead."""
    token = await _operator_token(client, session_factory)
    org_id, service_id, _card = await _seed(session_factory)
    event_id = await _add_event(session_factory, org_id, service_id)
    # El alta por repositorio no proyecta: se fuerza la proyección con un alta por API.
    await client.post(
        f"{API}/crm/agenda",
        json={
            "service_id": str(service_id),
            "nombre": "Edición a borrar",
            "starts_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        },
        headers=_auth(token),
    )

    deleted = await client.delete(f"{API}/crm/agenda/{event_id}", headers=_auth(token))

    assert deleted.status_code in (200, 204), deleted.text
    async with session_factory() as session:
        agent = (await session.execute(select(Agent))).scalars().one()
        names = [e["nombre"] for e in (agent.config.get("events") or [])]
        assert "Edición septiembre" not in names
        assert "Edición a borrar" in names
