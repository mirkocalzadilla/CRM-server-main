"""Control de acceso en la puerta (server#278, CR6, cierra #202).

Cubre los casos **25** (redeem válido), **26** (doble redeem, secuencial y concurrente),
**27** (entrada de otro evento), **28** (QR de pago escaneado por error) y **29** (entrada
legacy) de la matriz del handoff.

Lo que estos tests protegen: que **de dos escaneos del mismo QR gane exactamente uno**, y
que cada rechazo diga por qué. En la puerta hay una fila esperando y quien atiende
necesita saber qué hacer con la persona que tiene enfrente — "ya la usaron a las 19:40",
"esa entrada es del sábado" y "eso es el QR de pago" llevan a tres conversaciones
distintas, y un "inválido" genérico no sirve para ninguna.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import select

from server.modules.agent.domain.catalog_models import Service
from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.modules.agent.domain.models import Agent, AgentInstance, Conversation, Product
from server.modules.core.services.tenant_service import TenantService
from server.modules.crm.domain import stages
from server.modules.crm.domain.event_models import Event
from server.modules.crm.domain.models import Card, CardService, QrEntry
from server.modules.crm.domain.redemption import RedeemStatus
from server.modules.crm.repositories.board_repository import BoardRepository
from server.modules.crm.seed import seed_crm
from server.modules.crm.services.redemption_service import RedemptionService

from .test_catalog import API, SessionFactory, _auth, _operator_token

# El payload real del QR de pago de un banco boliviano: base64 de 256 bytes + "|" + hash.
PAYMENT_QR = (
    "VoAJzu2Axa763DIr5SMt62rpM+ihUKUSTHzyca1QR3Mgo0hxO6+kMTG0gYfAqTvO4J2pFF9ismAdc9O/XB7Pchkg"
    "Y3LZNFrEybZpUm5dcRX2dKbNeJdpEJoobhdv7l6wFsAO4agAcD1bo+VJ0qJfUjhkMdC1R1A3sMRf1BAqLw0Ru+Ok"
    "wyMj0przgBA7DmUuYg8CyK2N6LZ3KJB4jWCKMECDsBofnGu2XnV7rImIJJSbr+Ypk/Y9eIQyPYfS2siit0QZFo2u"
    "7c/3R+1laG9S2MDuhKMVGdoGdRzPn9HMEotNLlZYvVHHGVanRKnQnlKGO9mby+B7PjMdcFTp3CYi2A==|772E2FC6B3BAA5262DA3FE4B"
)


async def _seed(
    session_factory: SessionFactory,
    *,
    with_event: bool = True,
    revoked: bool = False,
    used: bool = False,
    lead_name: str | None = "Ana Quispe",
) -> tuple[uuid.UUID, str, uuid.UUID | None]:
    """Devuelve (org_id, token, event_id) con una entrada emitida."""
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
            external_id="59170000222",
            funnel_stage=FunnelStage.HANDED_OFF,
            is_ai_active=False,
            full_name=lead_name,
        )
        session.add(conversation)
        await session.flush()
        service = Service(
            organization_id=org_id,
            agent_id=agent.id,
            slug="curso-presencial",
            nombre="Curso de edición",
            resumen="r",
            precio="650 Bs",
            moneda="BOB",
            flujo_cierre="pago_qr",
            modality="presencial",
            price_amount=Decimal("650.00"),
        )
        session.add(service)
        await session.flush()
        stage = await BoardRepository(session).get_stage(
            org_id, stages.PIPELINE_HUMAN, stages.DELIVERED
        )
        assert stage is not None
        card = Card(
            organization_id=org_id,
            conversation_id=conversation.id,
            stage_id=stage.id,
            title="Lead",
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
        event_id: uuid.UUID | None = None
        snapshot: dict[str, object] = {}
        if with_event:
            event = Event(
                organization_id=org_id,
                service_id=service.id,
                nombre="Edición septiembre",
                starts_at=datetime.now(UTC) + timedelta(days=3),
                location="Sede centro",
                status="active",
            )
            session.add(event)
            await session.flush()
            event_id = event.id
            snapshot = {"nombre": event.nombre, "location": event.location}
        token = str(uuid.uuid4())
        session.add(
            QrEntry(
                card_id=card.id,
                token=token,
                qr_ref="qr/x.png",
                event_id=event_id,
                event_snapshot=snapshot,
                revoked_at=datetime.now(UTC) if revoked else None,
                revoked_reason="pago no confirmado" if revoked else None,
                used_at=datetime.now(UTC) - timedelta(minutes=20) if used else None,
            )
        )
        await session.commit()
        return org_id, token, event_id


async def _redeem(
    client: AsyncClient, token: str, auth: str, event_id: uuid.UUID | None = None
) -> dict[str, object]:
    body: dict[str, object] = {"token": token}
    if event_id is not None:
        body["event_id"] = str(event_id)
    response = await client.post(f"{API}/crm/entries/redeem", json=body, headers=_auth(auth))
    assert response.status_code == 200, response.text
    result: dict[str, object] = response.json()
    return result


# --------------------------- caso 25: redeem válido ---------------------------


async def test_valid_entry_is_admitted_with_the_attendee_data(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token_auth = await _operator_token(client, session_factory)
    _org, token, event_id = await _seed(session_factory)

    body = await _redeem(client, token, token_auth, event_id)

    assert body["status"] == RedeemStatus.OK
    assert body["admitted"] is True
    # Lo que se muestra en la pantalla sale de la base, no del QR: el token es opaco.
    assert body["lead_name"] == "Ana Quispe"
    assert body["service_name"] == "Curso de edición"
    assert body["amount"] == "650 Bs"
    assert body["event_name"] == "Edición septiembre"

    async with session_factory() as session:
        entry = (await session.execute(select(QrEntry))).scalars().one()
        assert entry.used_at is not None
        assert entry.used_by is not None  # queda quién la escaneó


async def test_entry_without_event_id_in_the_request_still_works(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Sin `event_id` se valida la entrada, pero no se puede rechazar por otra fecha."""
    token_auth = await _operator_token(client, session_factory)
    _org, token, _event = await _seed(session_factory)
    body = await _redeem(client, token, token_auth)
    assert body["admitted"] is True


# --------------------------- caso 26: doble redeem ---------------------------


async def test_second_scan_is_rejected_with_the_time_of_the_first(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token_auth = await _operator_token(client, session_factory)
    _org, token, event_id = await _seed(session_factory)

    first = await _redeem(client, token, token_auth, event_id)
    assert first["admitted"] is True

    second = await _redeem(client, token, token_auth, event_id)
    assert second["admitted"] is False
    assert second["status"] == RedeemStatus.ALREADY_USED
    assert second["used_at"] is not None
    assert "ya se usó" in str(second["detail"])
    # También muestra de quién era: es lo que resuelve la discusión en la puerta.
    assert second["lead_name"] == "Ana Quispe"


async def test_concurrent_scans_admit_exactly_one(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """El caso que motiva el update atómico: alguien reenvía su QR y dos personas lo
    muestran a la vez en filas distintas."""
    await _operator_token(client, session_factory)  # crea el tenant del seed
    org_id, token, event_id = await _seed(session_factory)

    async def scan() -> bool:
        async with session_factory() as session:
            service = RedemptionService(session=session)
            result = await service.redeem(token, org_id, uuid.uuid4(), event_id)
            return result.admitted

    results = await asyncio.gather(*(scan() for _ in range(5)))

    assert sum(1 for admitted in results if admitted) == 1, results
    async with session_factory() as session:
        entry = (await session.execute(select(QrEntry))).scalars().one()
        assert entry.used_at is not None


async def test_already_used_entry_is_rejected_before_consuming(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token_auth = await _operator_token(client, session_factory)
    _org, token, event_id = await _seed(session_factory, used=True)
    async with session_factory() as session:
        original = (await session.execute(select(QrEntry))).scalars().one().used_at

    body = await _redeem(client, token, token_auth, event_id)

    assert body["status"] == RedeemStatus.ALREADY_USED
    async with session_factory() as session:
        entry = (await session.execute(select(QrEntry))).scalars().one()
        assert entry.used_at == original  # no se re-marca con la hora nueva


# --------------------------- caso 27: otro evento ---------------------------


async def test_entry_from_another_event_is_rejected_by_name(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token_auth = await _operator_token(client, session_factory)
    _org, token, _event_id = await _seed(session_factory)
    other_event = uuid.uuid4()

    body = await _redeem(client, token, token_auth, other_event)

    assert body["admitted"] is False
    assert body["status"] == RedeemStatus.WRONG_EVENT
    # Nombra el evento al que SÍ pertenece: sin eso, quien atiende no sabe qué decirle.
    assert "Edición septiembre" in str(body["detail"])
    async with session_factory() as session:
        entry = (await session.execute(select(QrEntry))).scalars().one()
        assert entry.used_at is None  # no se consume una entrada que no es de acá


# --------------------------- caso 28: QR de pago ---------------------------


async def test_payment_qr_gets_its_own_message(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """El error más probable en la puerta: el lead abre la conversación y muestra la
    primera imagen, que es el QR con el que pagó."""
    token_auth = await _operator_token(client, session_factory)
    await _seed(session_factory)

    body = await _redeem(client, PAYMENT_QR, token_auth)

    assert body["admitted"] is False
    assert body["status"] == RedeemStatus.PAYMENT_QR
    assert "QR de pago" in str(body["detail"])
    assert "pedile la entrada" in str(body["detail"])


async def test_random_text_is_not_found(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token_auth = await _operator_token(client, session_factory)
    await _seed(session_factory)
    body = await _redeem(client, "hola", token_auth)
    assert body["status"] == RedeemStatus.NOT_FOUND


async def test_unknown_but_well_formed_token_is_not_found(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token_auth = await _operator_token(client, session_factory)
    await _seed(session_factory)
    body = await _redeem(client, str(uuid.uuid4()), token_auth)
    assert body["status"] == RedeemStatus.NOT_FOUND


# --------------------------- caso 29: legacy ---------------------------


async def test_legacy_entry_is_admitted_with_a_warning(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Emitida antes de que los eventos existieran: es legítima, así que pasa — pero
    quien atiende tiene que saber que hay que verificarla a mano."""
    token_auth = await _operator_token(client, session_factory)
    _org, token, _event = await _seed(session_factory, with_event=False)

    body = await _redeem(client, token, token_auth, uuid.uuid4())

    assert body["admitted"] is True
    assert body["status"] == RedeemStatus.LEGACY
    assert "verificá" in str(body["detail"])
    async with session_factory() as session:
        entry = (await session.execute(select(QrEntry))).scalars().one()
        assert entry.used_at is not None  # se consume igual


# --------------------------- revocada y aislamiento ---------------------------


async def test_revoked_entry_is_rejected(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """La entrada de un pago que no se pudo confirmar: acá es donde la revocación de
    #274 tiene efecto real."""
    token_auth = await _operator_token(client, session_factory)
    _org, token, event_id = await _seed(session_factory, revoked=True)

    body = await _redeem(client, token, token_auth, event_id)

    assert body["admitted"] is False
    assert body["status"] == RedeemStatus.REVOKED
    assert "no se pudo confirmar" in str(body["detail"])
    async with session_factory() as session:
        entry = (await session.execute(select(QrEntry))).scalars().one()
        assert entry.used_at is None


async def test_entry_of_another_tenant_cannot_be_redeemed(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """El tenant se valida por JOIN a la card (`qr_entry` no tiene organization_id)."""
    await _operator_token(client, session_factory)
    _org, token, event_id = await _seed(session_factory)
    other_org = uuid.uuid4()

    async with session_factory() as session:
        result = await RedemptionService(session=session).redeem(
            token, other_org, uuid.uuid4(), event_id
        )
    assert result.admitted is False
    assert result.status == RedeemStatus.NOT_FOUND
    async with session_factory() as session:
        entry = (await session.execute(select(QrEntry))).scalars().one()
        assert entry.used_at is None


async def test_redeem_requires_authentication(client: AsyncClient) -> None:
    response = await client.post(f"{API}/crm/entries/redeem", json={"token": str(uuid.uuid4())})
    assert response.status_code == 401


# --------------------------- lista de asistencia ---------------------------


async def test_attendance_list_shows_who_entered(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """El fallback sin cámara: un teléfono sin permiso o un QR arrugado no puede dejar
    afuera a alguien que pagó."""
    token_auth = await _operator_token(client, session_factory)
    _org, token, event_id = await _seed(session_factory)
    assert event_id is not None

    before = await client.get(f"{API}/crm/entries/attendance/{event_id}", headers=_auth(token_auth))
    assert before.status_code == 200, before.text
    assert before.json()["total"] == 1
    assert before.json()["checked_in"] == 0
    assert before.json()["attendees"][0]["lead_name"] == "Ana Quispe"

    await _redeem(client, token, token_auth, event_id)

    after = await client.get(f"{API}/crm/entries/attendance/{event_id}", headers=_auth(token_auth))
    assert after.json()["checked_in"] == 1
    assert after.json()["attendees"][0]["used_at"] is not None


async def test_attendance_includes_revoked_entries(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Quien atiende necesita poder decir "tu entrada fue anulada", no "no apareces"."""
    token_auth = await _operator_token(client, session_factory)
    _org, _token, event_id = await _seed(session_factory, revoked=True)
    assert event_id is not None

    response = await client.get(
        f"{API}/crm/entries/attendance/{event_id}", headers=_auth(token_auth)
    )
    body = response.json()
    assert len(body["attendees"]) == 1
    assert body["attendees"][0]["revoked_at"] is not None
    assert body["total"] == 0  # una revocada no cuenta como entrada vigente


async def test_attendance_of_another_tenant_is_empty(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token_auth = await _operator_token(client, session_factory)
    await _seed(session_factory)
    response = await client.get(
        f"{API}/crm/entries/attendance/{uuid.uuid4()}", headers=_auth(token_auth)
    )
    assert response.status_code == 200
    assert response.json()["attendees"] == []


# --------------------------- clasificación del token ---------------------------


async def test_token_classification_is_pure_and_specific() -> None:
    """Guard de la heurística: un uuid4 no puede confundirse con un QR de pago ni al
    revés, que es lo que hace posible el mensaje específico."""
    from server.modules.crm.domain.redemption import looks_like_entry_token, looks_like_payment_qr

    assert looks_like_entry_token(str(uuid.uuid4())) is True
    assert looks_like_entry_token(PAYMENT_QR) is False
    assert looks_like_entry_token("hola") is False
    assert looks_like_payment_qr(PAYMENT_QR) is True
    assert looks_like_payment_qr(str(uuid.uuid4())) is False
    assert looks_like_payment_qr("hola") is False


async def test_the_time_of_the_first_use_is_bolivian_not_utc(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Regresión: la hora del "ya se usó" salía en UTC, cuatro horas adelantada.

    El contenedor corre en UTC (la imagen no define `TZ`) y el helper formateaba con la
    zona del proceso, así que una entrada usada a las 19:40 se informaba como "23:40" —
    una hora que todavía no llegó. Es exactamente el dato con el que quien atiende resuelve
    la discusión de un QR reenviado: con la hora corrida, el titular legítimo dice que no
    la usó y el sistema parece darle la razón.
    """
    token_auth = await _operator_token(client, session_factory)
    _org, token, event_id = await _seed(session_factory)
    # 19:40 en Bolivia = 23:40 UTC del mismo día.
    used_at = datetime(2026, 8, 22, 23, 40, tzinfo=UTC)
    async with session_factory() as session:
        entry = (await session.execute(select(QrEntry).where(QrEntry.token == token))).scalar_one()
        entry.used_at = used_at
        await session.commit()

    body = await _redeem(client, token, token_auth, event_id)

    assert body["status"] == RedeemStatus.ALREADY_USED
    assert "19:40" in str(body["detail"]), body["detail"]
