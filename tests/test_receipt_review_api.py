"""Panel del comprobante y validación humana desde el CRM (server#272, CR3).

Cubre el caso **32** de la matriz (override con nota) y el contrato del panel: la
imagen, los datos extraídos y el semáforo de checks que ve el operador.

La regla que se está fijando: un comprobante que pasó los checks se valida con un
click; uno que no pasó **exige una nota**. Si una persona aprueba un pago que el sistema
no aprobaría, lo único que después explica esa decisión es lo que escribió.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import select

from server.modules.agent.domain.catalog_models import Service
from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.modules.agent.domain.models import Agent, AgentInstance, Conversation, Product
from server.modules.core.services.tenant_service import TenantService
from server.modules.crm.domain import card_flags, stages
from server.modules.crm.domain.models import Card, CardMove, CardService, Stage
from server.modules.crm.domain.payment_models import PaymentReceipt
from server.modules.crm.repositories.board_repository import BoardRepository
from server.modules.crm.seed import seed_crm

from .test_catalog import API, SessionFactory, _auth, _operator_token

LEAD_WA_ID = "59170000888"


async def _seed_card_with_receipt(
    session_factory: SessionFactory,
    *,
    verdict: str = "pass",
    flags: list[str] | None = None,
    stage_name: str = stages.PAYMENT_VALIDATION,
    approved_by: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Devuelve (card_id, receipt_id) en el tenant del operador ya registrado.

    `approved_by` simula un comprobante que ya se aprobó (`'system'` o un operador) y
    `stage_name` dónde quedó la card después: así se arman los casos "ya entregado".
    """
    async with session_factory() as session:
        tenant = await TenantService(session).get_by_slug("acme")
        org_id = tenant.id
        await seed_crm(session, org_id)
        if await session.get(Product, "cursos-mirko") is None:
            session.add(Product(slug="cursos-mirko", display_name="Cursos"))
            await session.flush()
        agent = (
            (await session.execute(select(Agent).where(Agent.organization_id == org_id)))
            .scalars()
            .first()
        )
        if agent is None:
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
        stage = await BoardRepository(session).get_stage(org_id, stages.PIPELINE_HUMAN, stage_name)
        assert stage is not None
        card = Card(
            organization_id=org_id,
            conversation_id=conversation.id,
            stage_id=stage.id,
            title="Lead Test",
            flags=flags or [],
        )
        session.add(card)
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
            modality="virtual",  # virtual sin links: la entrega no completa, no importa acá
            price_amount=Decimal("650.00"),
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
        receipt = PaymentReceipt(
            organization_id=org_id,
            card_id=card.id,
            wamid="wamid.REVIEW",
            media_path=f"{org_id}/wamid.REVIEW.jpg",
            image_sha256="sha-review",
            amount=Decimal("650.00") if verdict == "pass" else Decimal("900.00"),
            currency="BOB",
            beneficiary="MIRKO CALZADILLA",
            reference="2P10019819" if verdict == "pass" else None,
            verdict=verdict,
            checks=[
                {"code": "amount", "passed": verdict == "pass", "detail": "monto"},
                {"code": "beneficiary", "passed": True, "detail": "destinatario correcto"},
            ],
            # El JSON extraído siempre trae la referencia leída, incluso cuando el
            # veredicto falló y por eso la columna quedó en NULL (`describe_for_operator`).
            extracted={
                "amount": "650.00",
                "beneficiary": "MIRKO CALZADILLA",
                "reference": "2P10019819",
            },
            approved_at=datetime.now(UTC) if approved_by else None,
            approved_by=approved_by,
        )
        session.add(receipt)
        await session.commit()
        return card.id, receipt.id


async def _stage_name(session_factory: SessionFactory, card_id: uuid.UUID) -> str:
    async with session_factory() as session:
        card = await session.get(Card, card_id)
        assert card is not None
        stage = await session.get(Stage, card.stage_id)
        assert stage is not None
        return stage.name


# --------------------------- panel del comprobante ---------------------------


async def test_get_receipt_returns_data_and_traffic_light(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    card_id, receipt_id = await _seed_card_with_receipt(session_factory)

    response = await client.get(f"{API}/crm/cards/{card_id}/receipt", headers=_auth(token))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(receipt_id)
    assert body["verdict"] == "pass"
    assert body["extracted"]["beneficiary"] == "MIRKO CALZADILLA"
    assert {check["code"] for check in body["checks"]} == {"amount", "beneficiary"}
    assert body["image_url"] is not None and "/media/" in body["image_url"]
    # Nadie lo aprobó todavía: el panel ofrece validarlo.
    assert body["approved_at"] is None
    assert body["approved_by"] is None


async def test_get_receipt_is_null_when_the_lead_sent_none(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    response = await client.get(f"{API}/crm/cards/{uuid.uuid4()}/receipt", headers=_auth(token))
    assert response.status_code == 200
    assert response.json() is None


# --------------------------- validación en 1 click ---------------------------


async def test_validate_moves_the_card_in_one_click(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    card_id, _ = await _seed_card_with_receipt(session_factory, flags=[card_flags.RECEIPT_REVIEW])

    response = await client.post(
        f"{API}/crm/cards/{card_id}/receipt/validate", headers=_auth(token)
    )
    assert response.status_code == 200, response.text
    assert await _stage_name(session_factory, card_id) == stages.PAYMENT_VALIDATED
    # Queda registrado quién aprobó: el operador, no el sistema (server#292).
    body = response.json()
    assert body["approved_at"] is not None
    assert body["approved_by"] != "system"
    uuid.UUID(body["approved_by"])
    async with session_factory() as session:
        card = await session.get(Card, card_id)
        assert card is not None
        # El aviso de "revisá este comprobante" se va: ya lo revisó una persona.
        assert card_flags.RECEIPT_REVIEW not in card_flags.normalize(card.flags)


async def test_validate_refuses_a_failed_receipt_without_a_note(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """El click de un solo paso es para lo que pasó los checks. Lo que no pasó exige
    que alguien escriba por qué lo aprueba."""
    token = await _operator_token(client, session_factory)
    card_id, _ = await _seed_card_with_receipt(session_factory, verdict="fail")

    response = await client.post(
        f"{API}/crm/cards/{card_id}/receipt/validate", headers=_auth(token)
    )
    assert response.status_code == 400, response.text
    assert "nota" in response.json()["detail"].lower()
    assert await _stage_name(session_factory, card_id) == stages.PAYMENT_VALIDATION


async def test_validate_is_refused_once_the_card_was_delivered(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Sobre una card ya entregada, "validar" retrocedía la card a "Pago validado" y le
    mandaba la entrega al lead por segunda vez (UAT del 30/08). Ahora es un 400 y no se
    toca nada — ni con nota."""
    token = await _operator_token(client, session_factory)
    card_id, receipt_id = await _seed_card_with_receipt(
        session_factory, stage_name=stages.CLOSED, approved_by="system"
    )

    validate = await client.post(
        f"{API}/crm/cards/{card_id}/receipt/validate", headers=_auth(token)
    )
    assert validate.status_code == 400, validate.text
    assert "ya está validado" in validate.json()["detail"]
    override = await client.post(
        f"{API}/crm/cards/{card_id}/receipt/override",
        json={"note": "Lo valido igual para probar."},
        headers=_auth(token),
    )
    assert override.status_code == 400, override.text

    assert await _stage_name(session_factory, card_id) == stages.CLOSED
    async with session_factory() as session:
        receipt = await session.get(PaymentReceipt, receipt_id)
        assert receipt is not None
        assert receipt.approved_by == "system"
        assert receipt.human_note is None


async def test_validate_on_pago_validado_retries_delivery_and_keeps_the_first_approval(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """El sistema aprobó pero la entrega quedó trabada (virtual sin links): el click del
    operador la reintenta, y la aprobación sigue siendo la del sistema."""
    token = await _operator_token(client, session_factory)
    card_id, _ = await _seed_card_with_receipt(
        session_factory, stage_name=stages.PAYMENT_VALIDATED, approved_by="system"
    )

    response = await client.post(
        f"{API}/crm/cards/{card_id}/receipt/validate", headers=_auth(token)
    )
    assert response.status_code == 200, response.text
    assert response.json()["approved_by"] == "system"
    assert await _stage_name(session_factory, card_id) == stages.PAYMENT_VALIDATED
    async with session_factory() as session:
        card = await session.get(Card, card_id)
        assert card is not None
        assert card_flags.MISSING_LINK in card_flags.normalize(card.flags)


# --------------------------- caso 32: override con nota ---------------------------


async def test_override_with_note_validates_and_audits(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    card_id, receipt_id = await _seed_card_with_receipt(session_factory, verdict="fail")

    response = await client.post(
        f"{API}/crm/cards/{card_id}/receipt/override",
        json={"note": "Pagó 900 porque incluye el material impreso, hablado por teléfono."},
        headers=_auth(token),
    )
    assert response.status_code == 200, response.text
    assert await _stage_name(session_factory, card_id) == stages.PAYMENT_VALIDATED

    async with session_factory() as session:
        receipt = await session.get(PaymentReceipt, receipt_id)
        assert receipt is not None
        assert receipt.human_note is not None and "material impreso" in receipt.human_note
        # El override también es una aprobación, y con nombre propio.
        assert receipt.approved_at is not None
        assert receipt.approved_by not in (None, "system")
        # La nota también queda como motivo del movimiento: el historial de la card
        # explica por sí solo por qué avanzó (patrón de #253).
        moves = (
            (await session.execute(select(CardMove).where(CardMove.card_id == card_id)))
            .scalars()
            .all()
        )
        assert any(move.reason and "material impreso" in move.reason for move in moves)


async def test_override_rejects_an_empty_note(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    card_id, _ = await _seed_card_with_receipt(session_factory, verdict="fail")
    for note in ("", "   ", "ok"):
        response = await client.post(
            f"{API}/crm/cards/{card_id}/receipt/override",
            json={"note": note},
            headers=_auth(token),
        )
        assert response.status_code == 422, f"{note!r}: {response.text}"
    assert await _stage_name(session_factory, card_id) == stages.PAYMENT_VALIDATION


async def test_override_normalizes_whitespace_in_the_note(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    card_id, receipt_id = await _seed_card_with_receipt(session_factory, verdict="fail")
    response = await client.post(
        f"{API}/crm/cards/{card_id}/receipt/override",
        json={"note": "  sobrepago   acordado  "},
        headers=_auth(token),
    )
    assert response.status_code == 200, response.text
    async with session_factory() as session:
        receipt = await session.get(PaymentReceipt, receipt_id)
        assert receipt is not None
        assert receipt.human_note == "sobrepago acordado"


async def test_validate_unknown_card_is_404(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    response = await client.post(
        f"{API}/crm/cards/{uuid.uuid4()}/receipt/validate", headers=_auth(token)
    )
    assert response.status_code == 404


async def test_human_override_occupies_the_anti_reuse_unique(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Regresión: una transferencia aprobada a mano quedaba reusable para siempre.

    La referencia se guarda al validar automáticamente, pero un comprobante que falló los
    checks la deja en NULL a propósito — para no bloquear al operador que después quiera
    aprobarlo. Nadie la escribía cuando ese operador efectivamente lo aprobaba, así que la
    misma transferencia podía **auto-aprobar** una segunda entrega más adelante: los seis
    checks en verde, sin pasar por ojos humanos.

    El sha de la imagen no cubre ese caso: es igualdad exacta de bytes, y una captura
    reenviada por WhatsApp se recomprime.
    """
    token = await _operator_token(client, session_factory)
    card_id, receipt_id = await _seed_card_with_receipt(session_factory, verdict="fail")

    async with session_factory() as session:
        before = await session.get(PaymentReceipt, receipt_id)
        assert before is not None
        assert before.reference is None  # el estado que hace posible el reuso

    response = await client.post(
        f"{API}/crm/cards/{card_id}/receipt/override",
        json={"note": "pagó desde la cuenta del hermano, verificado por teléfono"},
        headers=_auth(token),
    )
    assert response.status_code == 200, response.text

    async with session_factory() as session:
        after = await session.get(PaymentReceipt, receipt_id)
        assert after is not None
        assert after.reference == "2P10019819"


async def test_override_does_not_steal_a_reference_from_another_card(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Si la transacción ya validó otro pago, el unique no se toca.

    El operador está aprobando algo que el sistema considera reuso; se respeta su decisión
    (ya escribió la nota), pero pisar el unique rompería la fila que lo tiene legítimamente.
    """
    token = await _operator_token(client, session_factory)
    card_id, receipt_id = await _seed_card_with_receipt(session_factory, verdict="fail")
    async with session_factory() as session:
        card = await session.get(Card, card_id)
        assert card is not None
        # El comprobante que ya ocupa el unique es de OTRA card: es lo que hace del caso
        # un reuso y no un segundo intento de la misma persona.
        instance = (await session.execute(select(AgentInstance))).scalars().first()
        assert instance is not None
        other_conv = Conversation(
            instance_id=instance.id,
            organization_id=card.organization_id,
            external_id="59170000889",
            funnel_stage=FunnelStage.HANDED_OFF,
            is_ai_active=False,
            full_name="Otro Lead",
        )
        session.add(other_conv)
        await session.flush()
        other_card = Card(
            organization_id=card.organization_id,
            conversation_id=other_conv.id,
            stage_id=card.stage_id,
            title="Otro Lead",
        )
        session.add(other_card)
        await session.flush()
        other = PaymentReceipt(
            organization_id=card.organization_id,
            card_id=other_card.id,
            wamid="wamid.OTRA",
            image_sha256="sha-otra",
            amount=Decimal("650.00"),
            currency="BOB",
            reference="2P10019819",  # la misma transacción, ya ocupada
            verdict="pass",
            checks=[],
            extracted={},
        )
        session.add(other)
        await session.commit()
        other_id = other.id

    response = await client.post(
        f"{API}/crm/cards/{card_id}/receipt/override",
        json={"note": "aprobado a mano igual"},
        headers=_auth(token),
    )
    assert response.status_code == 200, response.text

    async with session_factory() as session:
        overridden = await session.get(PaymentReceipt, receipt_id)
        assert overridden is not None
        assert overridden.reference is None
        assert overridden.human_note == "aprobado a mano igual"
        untouched = await session.get(PaymentReceipt, other_id)
        assert untouched is not None
        assert untouched.reference == "2P10019819"
