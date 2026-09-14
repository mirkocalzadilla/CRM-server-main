"""Servicios asignados a la oportunidad vía `card_service` (#132).

Cubre: asignación manual (source='assigned') resuelta contra el catálogo vivo,
reconciliación del set (agregar/quitar), preservación de los `captured` del bot,
card sin servicios, card inexistente (None), servicio fuera del catálogo (ValueError)
y aislamiento multi-tenant (no se puede asignar un servicio de otra org).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.agent.domain.agent_state import State
from server.modules.agent.domain.catalog_models import Service
from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.modules.agent.domain.models import Agent, Conversation, Product
from server.modules.crm.domain.models import Card, CardService, Pipeline, Stage
from server.modules.crm.services.board_service import BoardService
from server.modules.crm.services.service_capture_service import ServiceCaptureService


class _NoopPublisher:
    async def publish(self, channel: str, payload: dict[str, object]) -> None:
        return None


def _board(session: AsyncSession) -> BoardService:
    return BoardService(session=session, publisher=_NoopPublisher())  # type: ignore[arg-type]


async def _seed_card(
    session_factory: async_sessionmaker[AsyncSession], org_id: uuid.UUID
) -> uuid.UUID:
    async with session_factory() as session:
        pipeline = Pipeline(organization_id=org_id, kind="ia", name="Gestión IA", position=0)
        session.add(pipeline)
        await session.flush()
        stage = Stage(pipeline_id=pipeline.id, name="Enganchando", position=0, status_code="open")
        session.add(stage)
        await session.flush()
        conv = Conversation(
            instance_id=uuid.uuid4(), organization_id=org_id, external_id=str(uuid.uuid4())
        )
        session.add(conv)
        await session.flush()
        card = Card(
            organization_id=org_id, conversation_id=conv.id, stage_id=stage.id, title="Lead"
        )
        session.add(card)
        await session.commit()
        return card.id


async def _seed_services(
    session_factory: async_sessionmaker[AsyncSession], org_id: uuid.UUID, slugs: list[str]
) -> list[uuid.UUID]:
    """Un agente + N servicios en `org_id`. Devuelve los service_id en orden."""
    async with session_factory() as session:
        product_slug = f"prod-{org_id.hex[:8]}"
        if await session.get(Product, product_slug) is None:
            session.add(Product(slug=product_slug, display_name=product_slug))
            await session.flush()
        agent = Agent(
            organization_id=org_id,
            product_slug=product_slug,
            display_name="Agente",
            system_prompt="x",
            model="claude-haiku-4-5-20251001",
            tools=[],
            config={},
        )
        session.add(agent)
        await session.flush()
        ids: list[uuid.UUID] = []
        for slug in slugs:
            service = Service(
                organization_id=org_id,
                agent_id=agent.id,
                slug=slug,
                nombre=f"Servicio {slug}",
                resumen="resumen",
                precio="650",
                moneda="BOB",
                flujo_cierre="pago_qr",
            )
            session.add(service)
            await session.flush()
            ids.append(service.id)
        await session.commit()
        return ids


async def _set_services(
    session_factory: async_sessionmaker[AsyncSession],
    card_id: uuid.UUID,
    org_id: uuid.UUID,
    service_ids: list[uuid.UUID],
) -> list[uuid.UUID] | None:
    async with session_factory() as session:
        result = await _board(session).set_card_services(card_id, org_id, service_ids)
    return None if result is None else [s.service_id for s in result]


async def _detail_services(
    session_factory: async_sessionmaker[AsyncSession], card_id: uuid.UUID, org_id: uuid.UUID
) -> list[tuple[uuid.UUID, str]]:
    async with session_factory() as session:
        detail = await _board(session).get_card_detail(card_id, org_id)
    assert detail is not None
    return [(s.service_id, s.source) for s in detail.services]


async def test_card_without_services_is_empty(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    card_id = await _seed_card(session_factory, org_id)
    assert await _detail_services(session_factory, card_id, org_id) == []


async def test_assign_services_shows_in_detail(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    card_id = await _seed_card(session_factory, org_id)
    a, b = await _seed_services(session_factory, org_id, ["uno", "dos"])

    returned = await _set_services(session_factory, card_id, org_id, [a, b])
    assert returned is not None and set(returned) == {a, b}

    async with session_factory() as session:
        detail = await _board(session).get_card_detail(card_id, org_id)
    assert detail is not None
    assert {s.service_id for s in detail.services} == {a, b}
    uno = next(s for s in detail.services if s.service_id == a)
    assert uno.nombre == "Servicio uno" and uno.precio == "650" and uno.moneda == "BOB"
    assert all(s.source == "assigned" for s in detail.services)


async def test_set_reconciles_assigned(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    card_id = await _seed_card(session_factory, org_id)
    a, b, c = await _seed_services(session_factory, org_id, ["a", "b", "c"])

    await _set_services(session_factory, card_id, org_id, [a, b])
    # Re-set a [a, c]: agrega c, quita b, mantiene a (idempotente).
    await _set_services(session_factory, card_id, org_id, [a, c])
    assert {sid for sid, _ in await _detail_services(session_factory, card_id, org_id)} == {a, c}

    # Set vacío deja la card sin servicios asignados.
    await _set_services(session_factory, card_id, org_id, [])
    assert await _detail_services(session_factory, card_id, org_id) == []


async def test_assigned_set_preserves_captured(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    card_id = await _seed_card(session_factory, org_id)
    a, b = await _seed_services(session_factory, org_id, ["a", "b"])

    # El bot capturó `a` (source='captured', simulado: #133 aún no existe).
    async with session_factory() as session:
        session.add(
            CardService(organization_id=org_id, card_id=card_id, service_id=a, source="captured")
        )
        await session.commit()

    # Asignar manualmente solo `b` NO borra el capturado `a`.
    await _set_services(session_factory, card_id, org_id, [b])
    services = dict(await _detail_services(session_factory, card_id, org_id))
    assert services == {a: "captured", b: "assigned"}


async def test_unknown_service_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    card_id = await _seed_card(session_factory, org_id)
    async with session_factory() as session:
        with pytest.raises(ValueError, match="catálogo"):
            await _board(session).set_card_services(card_id, org_id, [uuid.uuid4()])


async def test_unknown_card_returns_none(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    assert await _set_services(session_factory, uuid.uuid4(), uuid.uuid4(), []) is None


async def test_services_are_tenant_scoped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    card_b = await _seed_card(session_factory, org_b)
    (service_a,) = await _seed_services(session_factory, org_a, ["solo-a"])

    # Asignar un servicio de org_a a una card de org_b → rechazado (no es de su catálogo).
    async with session_factory() as session:
        with pytest.raises(ValueError, match="catálogo"):
            await _board(session).set_card_services(card_b, org_b, [service_a])


# ---------- Captura automática por el bot (source='captured', #133) ----------


async def _seed_for_capture(
    session_factory: async_sessionmaker[AsyncSession], org_id: uuid.UUID, slug: str
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """org + agente + servicio (`slug`) + conversación + card. Devuelve
    (card_id, conversation_id, agent_id, service_id)."""
    async with session_factory() as session:
        product_slug = f"prod-{org_id.hex[:8]}"
        session.add(Product(slug=product_slug, display_name=product_slug))
        await session.flush()
        agent = Agent(
            organization_id=org_id,
            product_slug=product_slug,
            display_name="Agente",
            system_prompt="x",
            model="claude-haiku-4-5-20251001",
            tools=[],
            config={},
        )
        session.add(agent)
        await session.flush()
        service = Service(
            organization_id=org_id,
            agent_id=agent.id,
            slug=slug,
            nombre="Curso",
            resumen="r",
            precio="650",
            moneda="BOB",
            flujo_cierre="pago_qr",
        )
        session.add(service)
        await session.flush()
        pipeline = Pipeline(organization_id=org_id, kind="ia", name="IA", position=0)
        session.add(pipeline)
        await session.flush()
        stage = Stage(pipeline_id=pipeline.id, name="Eng", position=0, status_code="open")
        session.add(stage)
        await session.flush()
        conv = Conversation(
            instance_id=uuid.uuid4(), organization_id=org_id, external_id=str(uuid.uuid4())
        )
        session.add(conv)
        await session.flush()
        card = Card(
            organization_id=org_id, conversation_id=conv.id, stage_id=stage.id, title="Lead"
        )
        session.add(card)
        await session.commit()
        return card.id, conv.id, agent.id, service.id


def _state(org_id: uuid.UUID, conversation_id: uuid.UUID, agent_id: uuid.UUID) -> State:
    return State(
        tenant_id=org_id,
        conversation_id=conversation_id,
        agent_id=agent_id,
        external_id="59170000000",
        funnel_stage=FunnelStage.ENGAGING,
        is_ai_active=True,
        system_prompt="x",
        config={},
    )


async def _capture(
    session_factory: async_sessionmaker[AsyncSession], state: State, slugs: tuple[str, ...]
) -> None:
    async with session_factory() as session:
        await ServiceCaptureService(session=session).on_captured(state, slugs)


async def test_capture_stamps_service(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    card_id, conv_id, agent_id, service_id = await _seed_for_capture(
        session_factory, org_id, "curso-edicion"
    )

    await _capture(session_factory, _state(org_id, conv_id, agent_id), ("curso-edicion",))

    assert await _detail_services(session_factory, card_id, org_id) == [(service_id, "captured")]


async def test_capture_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    card_id, conv_id, agent_id, service_id = await _seed_for_capture(
        session_factory, org_id, "curso-edicion"
    )
    state = _state(org_id, conv_id, agent_id)

    await _capture(session_factory, state, ("curso-edicion",))
    await _capture(session_factory, state, ("curso-edicion", "curso-edicion"))

    assert await _detail_services(session_factory, card_id, org_id) == [(service_id, "captured")]


async def test_capture_unknown_slug_is_noop(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    card_id, conv_id, agent_id, _service_id = await _seed_for_capture(
        session_factory, org_id, "curso-edicion"
    )

    await _capture(session_factory, _state(org_id, conv_id, agent_id), ("no-existe",))

    assert await _detail_services(session_factory, card_id, org_id) == []


async def test_capture_does_not_override_assigned(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    card_id, conv_id, agent_id, service_id = await _seed_for_capture(
        session_factory, org_id, "curso-edicion"
    )
    # Ya estaba asignado a mano; la captura del bot no lo duplica ni cambia su source.
    await _set_services(session_factory, card_id, org_id, [service_id])

    await _capture(session_factory, _state(org_id, conv_id, agent_id), ("curso-edicion",))

    assert await _detail_services(session_factory, card_id, org_id) == [(service_id, "assigned")]


async def _set_stage(
    session_factory: async_sessionmaker[AsyncSession],
    conversation_id: uuid.UUID,
    stage: FunnelStage,
) -> None:
    async with session_factory() as session:
        conv = await session.get(Conversation, conversation_id)
        assert conv is not None
        conv.funnel_stage = stage
        await session.commit()


def _ratings(board: object) -> list[str]:
    return [card.rating for p in board.pipelines for s in p.stages for card in s.cards]  # type: ignore[attr-defined]


async def test_handed_off_with_accepted_service_is_hot(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # #206: un lead derivado a Gestión Humana que YA aceptó un servicio es una compra en
    # curso → hot (no medium). Regresión del bug de temperatura del cierre consultivo.
    org_id = uuid.uuid4()
    _card_id, conv_id, agent_id, _service_id = await _seed_for_capture(
        session_factory, org_id, "curso-edicion"
    )
    await _set_stage(session_factory, conv_id, FunnelStage.HANDED_OFF)
    await _capture(session_factory, _state(org_id, conv_id, agent_id), ("curso-edicion",))

    async with session_factory() as session:
        board = await _board(session).get_board(org_id)
    assert _ratings(board) == ["hot"]


async def test_handed_off_without_service_stays_medium(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Un handoff SIN servicio aceptado (pidió humano, reclamo, servicio desconocido) sigue
    # medium — la temperatura hot es solo para el lead que aceptó algo (#206).
    org_id = uuid.uuid4()
    _card_id, conv_id, _agent_id, _service_id = await _seed_for_capture(
        session_factory, org_id, "curso-edicion"
    )
    await _set_stage(session_factory, conv_id, FunnelStage.HANDED_OFF)

    async with session_factory() as session:
        board = await _board(session).get_board(org_id)
    assert _ratings(board) == ["medium"]
