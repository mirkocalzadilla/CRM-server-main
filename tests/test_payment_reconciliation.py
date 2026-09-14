"""Conciliación de pagos auto-validados (server#274, CR4).

Cubre los casos **23** (confirmación humana) y **24** (rechazo → entrada revocada + card
a `lost`) de la matriz del handoff.

La razón de ser de esta capa: mientras no haya pasarela bancaria, un comprobante es una
imagen y una imagen se puede editar. El sistema aprueba y entrega para que el lead no
espere, pero alguien tiene que cotejar contra el banco — y si no entró, hay que poder
revocar lo que ya se entregó.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import select

from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.modules.agent.domain.models import Agent, AgentInstance, Conversation, Product
from server.modules.core.services.tenant_service import TenantService
from server.modules.crm.domain import card_flags, stages
from server.modules.crm.domain.models import Card, CardMove, QrEntry, Stage
from server.modules.crm.domain.payment_models import PaymentReceipt
from server.modules.crm.repositories.board_repository import BoardRepository
from server.modules.crm.seed import seed_crm
from server.shared.timezone import business_today

from .test_catalog import API, SessionFactory, _auth, _operator_token

LEAD_WA_ID = "59170000444"


async def _seed(
    session_factory: SessionFactory,
    *,
    verdict: str = "pass",
    human_note: str | None = None,
    with_entry: bool = True,
    stage_name: str = stages.DELIVERED,
    flags: list[str] | None = None,
    created_at: datetime | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Devuelve (org_id, card_id, receipt_id) en el tenant del operador."""
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
        stage = await BoardRepository(session).get_stage(org_id, stages.PIPELINE_HUMAN, stage_name)
        assert stage is not None
        card = Card(
            organization_id=org_id,
            conversation_id=conversation.id,
            stage_id=stage.id,
            title="Lead Test",
            flags=flags if flags is not None else [card_flags.PAYMENT_UNCONFIRMED],
        )
        session.add(card)
        await session.flush()
        if with_entry:
            session.add(QrEntry(card_id=card.id, token=str(uuid.uuid4()), qr_ref="qr/x.png"))
        receipt = PaymentReceipt(
            organization_id=org_id,
            card_id=card.id,
            wamid=f"wamid.{uuid.uuid4()}",
            image_sha256=str(uuid.uuid4()),
            amount=Decimal("650.00"),
            currency="BOB",
            beneficiary="MIRKO CALZADILLA",
            reference=f"2P{uuid.uuid4().hex[:8]}",
            bank="BANCO GANADERO",
            verdict=verdict,
            checks=[{"code": "amount", "passed": True, "detail": "monto correcto"}],
            extracted={"amount": "650.00"},
            human_note=human_note,
        )
        if created_at is not None:
            receipt.created_at = created_at
        session.add(receipt)
        await session.commit()
        return org_id, card.id, receipt.id


async def _stage_status(session_factory: SessionFactory, card_id: uuid.UUID) -> str:
    async with session_factory() as session:
        card = await session.get(Card, card_id)
        assert card is not None
        stage = await session.get(Stage, card.stage_id)
        assert stage is not None
        return stage.status_code


async def _flags(session_factory: SessionFactory, card_id: uuid.UUID) -> list[str]:
    async with session_factory() as session:
        card = await session.get(Card, card_id)
        assert card is not None
        return card_flags.normalize(card.flags)


# --------------------------- la cola ---------------------------


async def test_auto_approved_payment_shows_in_the_queue(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    _org, card_id, receipt_id = await _seed(session_factory)

    response = await client.get(f"{API}/crm/payments/pending", headers=_auth(token))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["id"] == str(receipt_id)
    assert item["card_id"] == str(card_id)
    assert item["amount"] == "650.00"
    assert item["reference"] is not None  # lo que se coteja contra el extracto


async def test_manually_validated_payment_is_not_in_the_queue(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Ya lo miró una persona: ponerlo en la cola sería pedir el mismo trabajo dos veces."""
    token = await _operator_token(client, session_factory)
    await _seed(session_factory, human_note="sobrepago acordado por teléfono")

    response = await client.get(f"{API}/crm/payments/pending", headers=_auth(token))
    assert response.json()["total"] == 0


async def test_failed_receipt_is_not_in_the_queue(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Un comprobante rechazado por los checks nunca se aprobó: no hay nada que conciliar."""
    token = await _operator_token(client, session_factory)
    await _seed(session_factory, verdict="fail")

    response = await client.get(f"{API}/crm/payments/pending", headers=_auth(token))
    assert response.json()["total"] == 0


# --------------------------- caso 23: confirmar ---------------------------


async def test_confirm_seals_the_payment_and_clears_the_notice(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    _org, card_id, receipt_id = await _seed(session_factory)

    response = await client.post(f"{API}/crm/payments/{receipt_id}/confirm", headers=_auth(token))
    assert response.status_code == 200, response.text

    async with session_factory() as session:
        receipt = await session.get(PaymentReceipt, receipt_id)
        assert receipt is not None
        assert receipt.human_confirmed_at is not None
        assert receipt.human_by is not None  # queda auditado quién
    assert card_flags.PAYMENT_UNCONFIRMED not in await _flags(session_factory, card_id)
    # Sale de la cola.
    assert (await client.get(f"{API}/crm/payments/pending", headers=_auth(token))).json()[
        "total"
    ] == 0


async def test_confirming_twice_is_harmless(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    _org, _card_id, receipt_id = await _seed(session_factory)

    first = await client.post(f"{API}/crm/payments/{receipt_id}/confirm", headers=_auth(token))
    assert first.status_code == 200
    async with session_factory() as session:
        receipt = await session.get(PaymentReceipt, receipt_id)
        assert receipt is not None
        sealed_at = receipt.human_confirmed_at

    second = await client.post(f"{API}/crm/payments/{receipt_id}/confirm", headers=_auth(token))
    assert second.status_code == 200
    async with session_factory() as session:
        receipt = await session.get(PaymentReceipt, receipt_id)
        assert receipt is not None
        assert receipt.human_confirmed_at == sealed_at  # no se re-sella


async def test_confirm_unknown_receipt_is_404(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    response = await client.post(f"{API}/crm/payments/{uuid.uuid4()}/confirm", headers=_auth(token))
    assert response.status_code == 404


# --------------------------- caso 24: rechazar ---------------------------


async def test_reject_revokes_the_entry_and_loses_the_opportunity(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    _org, card_id, receipt_id = await _seed(session_factory)

    response = await client.post(
        f"{API}/crm/payments/{receipt_id}/reject",
        json={"note": "No figura en el extracto del banco, el comprobante estaba editado."},
        headers=_auth(token),
    )
    assert response.status_code == 200, response.text

    async with session_factory() as session:
        receipt = await session.get(PaymentReceipt, receipt_id)
        assert receipt is not None
        assert receipt.human_rejected_at is not None
        assert receipt.human_note is not None and "extracto" in receipt.human_note
        # La entrada queda anulada, no borrada: el lead ya la tiene en el teléfono y lo
        # único que se puede hacer es que el escáner la rechace en la puerta.
        entry = (
            await session.execute(select(QrEntry).where(QrEntry.card_id == card_id))
        ).scalar_one()
        assert entry.revoked_at is not None
        assert entry.revoked_reason is not None
        # La oportunidad se cierra como perdida, con el motivo en el historial.
        moves = (
            (await session.execute(select(CardMove).where(CardMove.card_id == card_id)))
            .scalars()
            .all()
        )
        assert any(move.reason and "extracto" in move.reason for move in moves)

    assert await _stage_status(session_factory, card_id) == "lost"
    assert card_flags.PAYMENT_UNCONFIRMED not in await _flags(session_factory, card_id)


async def test_reject_without_a_note_is_refused(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Revocar algo ya entregado sin dejar el motivo escrito no es una opción."""
    token = await _operator_token(client, session_factory)
    _org, card_id, receipt_id = await _seed(session_factory)

    for note in ("", "   ", "no"):
        response = await client.post(
            f"{API}/crm/payments/{receipt_id}/reject",
            json={"note": note},
            headers=_auth(token),
        )
        assert response.status_code == 422, f"{note!r}: {response.text}"

    async with session_factory() as session:
        entry = (
            await session.execute(select(QrEntry).where(QrEntry.card_id == card_id))
        ).scalar_one()
        assert entry.revoked_at is None  # nada se revocó


async def test_reject_works_without_an_entry(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Un curso virtual no genera entrada: el rechazo igual cierra la oportunidad."""
    token = await _operator_token(client, session_factory)
    _org, card_id, receipt_id = await _seed(session_factory, with_entry=False)

    response = await client.post(
        f"{API}/crm/payments/{receipt_id}/reject",
        json={"note": "El pago nunca entró."},
        headers=_auth(token),
    )
    assert response.status_code == 200, response.text
    assert await _stage_status(session_factory, card_id) == "lost"


async def test_rejecting_twice_is_harmless(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    _org, _card_id, receipt_id = await _seed(session_factory)
    payload = {"note": "No figura en el extracto."}

    first = await client.post(
        f"{API}/crm/payments/{receipt_id}/reject", json=payload, headers=_auth(token)
    )
    assert first.status_code == 200
    async with session_factory() as session:
        receipt = await session.get(PaymentReceipt, receipt_id)
        assert receipt is not None
        rejected_at = receipt.human_rejected_at

    second = await client.post(
        f"{API}/crm/payments/{receipt_id}/reject", json=payload, headers=_auth(token)
    )
    assert second.status_code == 200
    async with session_factory() as session:
        receipt = await session.get(PaymentReceipt, receipt_id)
        assert receipt is not None
        assert receipt.human_rejected_at == rejected_at


# --------------------------- export ---------------------------


async def test_export_returns_the_days_auto_approved_payments(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    token = await _operator_token(client, session_factory)
    await _seed(session_factory)

    response = await client.get(f"{API}/crm/payments/export", headers=_auth(token))
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    lines = response.text.strip().splitlines()
    assert lines[0].startswith("fecha_registro,monto,moneda")
    assert len(lines) == 2
    assert "650.00" in lines[1]
    assert "por confirmar" in lines[1]


async def test_export_of_another_day_is_empty(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """El export es por día: el de ayer no trae lo de hoy."""
    token = await _operator_token(client, session_factory)
    await _seed(session_factory)
    # "Ayer" del negocio, no de UTC: entre las 20:00 y las 24:00 de Bolivia el "ayer" UTC
    # es el hoy local, y el export traía el comprobante recién sembrado (misma raíz que #281).
    yesterday = (business_today() - timedelta(days=1)).isoformat()

    response = await client.get(f"{API}/crm/payments/export?day={yesterday}", headers=_auth(token))
    assert response.status_code == 200
    assert len(response.text.strip().splitlines()) == 1  # solo el encabezado


async def test_rejecting_a_card_already_in_lost_still_revokes_the_entry(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Regresión: el rechazo se perdía entero si la card ya estaba en "Perdido".

    `move_card` commitea sólo cuando el stage cambia, así que este camino salía del
    servicio sin persistir nada y la sesión del request se cerraba con rollback: la
    entrada quedaba sin revocar y el operador veía un 200. Es el peor caso posible porque
    la revocación tiene un único escritor y ninguna reconciliación posterior — lo que no
    se graba acá no se graba nunca, y el escáner admite la entrada en la puerta.

    Se llega arrastrando la card a "Perdido" antes de ir a la cola, o rechazando un
    segundo comprobante de la misma card.
    """
    token = await _operator_token(client, session_factory)
    _org, card_id, receipt_id = await _seed(session_factory, stage_name=stages.HUMAN_LOST)

    response = await client.post(
        f"{API}/crm/payments/{receipt_id}/reject",
        json={"note": "el banco no muestra la transferencia"},
        headers=_auth(token),
    )
    assert response.status_code == 200, response.text

    async with session_factory() as session:
        entry = (
            await session.execute(select(QrEntry).where(QrEntry.card_id == card_id))
        ).scalar_one()
        assert entry.revoked_at is not None, "la entrada quedó sin revocar: rollback silencioso"
        assert entry.revoked_reason == "el banco no muestra la transferencia"
        receipt = await session.get(PaymentReceipt, receipt_id)
        assert receipt is not None
        assert receipt.human_rejected_at is not None
        assert receipt.human_note == "el banco no muestra la transferencia"


async def test_a_rejected_payment_leaves_the_pending_queue_even_from_lost(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """La otra mitad del mismo bug: sin commit, el comprobante volvía a la cola siempre."""
    token = await _operator_token(client, session_factory)
    _org, _card_id, receipt_id = await _seed(session_factory, stage_name=stages.HUMAN_LOST)

    await client.post(
        f"{API}/crm/payments/{receipt_id}/reject",
        json={"note": "no entró"},
        headers=_auth(token),
    )
    pending = await client.get(f"{API}/crm/payments/pending", headers=_auth(token))

    assert pending.status_code == 200, pending.text
    ids = [item["id"] for item in pending.json()["items"]]
    assert str(receipt_id) not in ids


async def test_export_uses_bolivian_day_boundaries(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """Regresión: el CSV del día usaba límites UTC y perdía las últimas cuatro horas.

    Un pago recibido a las 21:00 de Bolivia son las 01:00 UTC del día siguiente, así que
    caía en el CSV de mañana y faltaba en el del día que se estaba cotejando contra el
    extracto — que es un extracto bancario boliviano.
    """
    token = await _operator_token(client, session_factory)
    # 2026-08-22 21:00 en Bolivia = 2026-08-23 01:00 UTC.
    await _seed(session_factory, created_at=datetime(2026, 8, 23, 1, 0, tzinfo=UTC))

    same_day = await client.get(f"{API}/crm/payments/export?day=2026-08-22", headers=_auth(token))
    next_day = await client.get(f"{API}/crm/payments/export?day=2026-08-23", headers=_auth(token))

    assert same_day.status_code == 200, same_day.text
    assert len(same_day.text.strip().splitlines()) == 2, "el pago de las 21:00 no salió en su día"
    assert len(next_day.text.strip().splitlines()) == 1  # solo el encabezado


async def test_export_without_a_day_includes_a_payment_received_now(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    """El "hoy" por defecto del export es el día boliviano.

    Con el día UTC, abrir el export después de las 20:00 locales pedía el día siguiente y
    devolvía un archivo vacío justo cuando se estaba cerrando la jornada.
    """
    token = await _operator_token(client, session_factory)
    await _seed(session_factory)

    response = await client.get(f"{API}/crm/payments/export", headers=_auth(token))

    assert response.status_code == 200, response.text
    assert len(response.text.strip().splitlines()) == 2, "el pago de hoy no salió en el export"
