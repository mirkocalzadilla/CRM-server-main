"""Circuito completo de la validación automática del comprobante (server#272, CR3).

Cubre de la matriz del handoff los casos **1** (aprobación que dispara la entrega), **3**
(monto distinto), **7** (mismo comprobante desde otro lead), **8** (misma imagen
reenviada), **9** (sin referencia), **10** (PDF), **11** (imagen ilegible), **13** (USD),
**14** (dos servicios) y **17** (un humano descalifica mientras el job corre).

El modelo de visión está stubbeado: acá se prueba **la decisión y sus consecuencias**,
que es lo determinístico. Que el modelo lea bien un comprobante boliviano de verdad es
la prueba con la API real (Capa B), no esto.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.agent.domain.catalog_models import Service
from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.modules.agent.domain.models import (
    Agent,
    AgentInstance,
    AiChatHistory,
    Conversation,
    Product,
)
from server.modules.crm.domain import card_flags, stages
from server.modules.crm.domain.models import Card, CardService, Stage
from server.modules.crm.domain.payment_models import PaymentReceipt, PaymentSettings
from server.modules.crm.domain.receipt_summary import NOTE_MARKER
from server.modules.crm.repositories.board_repository import BoardRepository
from server.modules.crm.seed import seed_crm
from server.modules.crm.services import receipt_media
from server.modules.crm.services.receipt_validation_service import (
    REVIEW_REPLY,
    ReceiptValidationService,
)

SessionFactory = async_sessionmaker[AsyncSession]
LEAD_WA_ID = "59170000777"
WAMID = "wamid.RECEIPT1"
TODAY = date(2026, 8, 23)
MIRKO = "Mirko Calzadilla"
JPEG = b"\xff\xd8\xff" + b"receipt-bytes" * 8
PDF = b"%PDF-1.4 receipt"


class _NoopPublisher:
    async def publish(self, channel: str, message: object) -> None:
        return None


class _RecordingPublisher:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    async def publish(self, channel: str, message: object) -> None:
        self.events.append((channel, message))


class _RecordingSender:
    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []

    async def send_text(self, to: str, body: str) -> None:
        self.texts.append((to, body))

    async def send_image(self, to: str, link: str, caption: str = "") -> None:
        raise AssertionError("la validación no manda imágenes")

    async def send_document(self, to: str, link: str, filename: str, caption: str = "") -> None:
        raise AssertionError("la validación no manda documentos")


class _StubVision:
    """Devuelve lo que se le programe, o falla si se le pide."""

    def __init__(self, result: dict[str, object] | None = None, *, fail: bool = False) -> None:
        self._result = result or {}
        self._fail = fail
        self.calls = 0

    async def extract(
        self,
        *,
        content: bytes,
        mime_type: str,
        instructions: str,
        schema: dict[str, object],
    ) -> dict[str, object]:
        self.calls += 1
        self.last_mime = mime_type
        if self._fail:
            raise RuntimeError("el proveedor de visión falló")
        return self._result


def _valid_extraction(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "amount": "650.00",
        "currency": "BOB",
        "paid_at": "2026-08-22",
        "beneficiary": "MIRKO CALZADILLA",
        "reference": "2P10019819",
        "bank": "BANCO GANADERO",
    }
    base.update(over)
    return base


@pytest.fixture
def media_root(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> object:
    """Redirige `media_root` a un directorio temporal del test."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        receipt_media,
        "get_settings",
        lambda: SimpleNamespace(media_root=str(tmp_path)),
    )
    return tmp_path


def _write_receipt(root: object, org_id: uuid.UUID, content: bytes, ext: str = ".jpg") -> str:
    import os

    org_dir = os.path.join(str(root), str(org_id))
    os.makedirs(org_dir, exist_ok=True)
    name = f"{WAMID}{ext}"
    with open(os.path.join(org_dir, name), "wb") as handle:
        handle.write(content)
    return f"{org_id}/{name}"


async def _seed(
    session_factory: SessionFactory,
    *,
    price: str | None = "650.00",
    currency: str = "BOB",
    modality: str | None = "presencial",
    services: int = 1,
    stage_name: str = stages.PAYMENT_VALIDATION,
    beneficiary: str | None = MIRKO,
    media_path: str | None = None,
    mime: str | None = "image/jpeg",
    wamid: str = WAMID,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Devuelve (org_id, conversation_id, card_id) con el comprobante ya ingerido."""
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
            full_name="Lead Test",
        )
        session.add(conversation)
        await session.flush()
        if beneficiary is not None:
            session.add(PaymentSettings(organization_id=org_id, expected_beneficiary=beneficiary))
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
                moneda=currency,
                flujo_cierre="pago_qr",
                modality=modality,
                price_amount=Decimal(price) if price is not None else None,
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
        # El turno del lead con el comprobante, tal como lo guarda el webhook.
        message: dict[str, object] = {
            "role": "user",
            "content": "[image: sin descripción]",
            "wamid": wamid,
            "channel": "whatsapp",
            "media_type": "image",
        }
        if media_path is not None:
            message["media_path"] = media_path
        if mime is not None:
            message["media_mime"] = mime
        session.add(
            AiChatHistory(
                agent_id=agent.id,
                organization_id=org_id,
                thread_id=str(conversation.id),
                session_id=LEAD_WA_ID,
                message=message,
            )
        )
        await session.commit()
        return org_id, conversation.id, card.id


async def _validate(
    session_factory: SessionFactory,
    org_id: uuid.UUID,
    conversation_id: uuid.UUID,
    vision: _StubVision,
    sender: _RecordingSender,
    *,
    wamid: str = WAMID,
    today: date = TODAY,
    publisher: _NoopPublisher | _RecordingPublisher | None = None,
) -> object:
    async with session_factory() as session:
        service = ReceiptValidationService(
            session=session,
            vision=vision,
            sender=sender,
            publisher=publisher or _NoopPublisher(),
            today=today,
        )
        return await service.validate(conversation_id, org_id, wamid)


async def _stage_name(session_factory: SessionFactory, card_id: uuid.UUID) -> str:
    async with session_factory() as session:
        card = await session.get(Card, card_id)
        assert card is not None
        stage = await session.get(Stage, card.stage_id)
        assert stage is not None
        return stage.name


async def _receipts(session_factory: SessionFactory) -> list[PaymentReceipt]:
    async with session_factory() as session:
        return list((await session.execute(select(PaymentReceipt))).scalars().all())


async def _flags(session_factory: SessionFactory, card_id: uuid.UUID) -> list[str]:
    async with session_factory() as session:
        card = await session.get(Card, card_id)
        assert card is not None
        return card_flags.normalize(card.flags)


# --------------------------- caso 1: aprobación ---------------------------


async def test_valid_receipt_is_approved_and_advances_the_card(
    session_factory: SessionFactory, media_root: object
) -> None:
    org_id, conversation_id, card_id = await _seed(session_factory)
    path = _write_receipt(media_root, org_id, JPEG)
    await _relink_media(session_factory, org_id, path)

    vision = _StubVision(_valid_extraction())
    sender = _RecordingSender()
    outcome = await _validate(session_factory, org_id, conversation_id, vision, sender)

    assert outcome.approved is True
    assert vision.calls == 1
    assert sender.texts == []  # una aprobación no manda el mensaje de revisión
    assert await _stage_name(session_factory, card_id) == stages.PAYMENT_VALIDATED
    receipts = await _receipts(session_factory)
    assert len(receipts) == 1
    assert receipts[0].verdict == "pass"
    assert receipts[0].amount == Decimal("650.00")
    assert receipts[0].reference == "2P10019819"
    # Queda registrado que lo aprobó el sistema: es lo que le dice al panel del CRM que
    # ya no hay nada que validar (server#292).
    assert receipts[0].approved_by == "system"
    assert receipts[0].approved_at is not None
    assert card_flags.RECEIPT_REVIEW not in await _flags(session_factory, card_id)


async def _relink_media(
    session_factory: SessionFactory, org_id: uuid.UUID, media_path: str
) -> None:
    """Apunta el turno ingerido al archivo recién escrito en el tmp_path."""
    async with session_factory() as session:
        rows = (await session.execute(select(AiChatHistory))).scalars().all()
        for row in rows:
            if row.message.get("wamid"):
                updated = dict(row.message)
                updated["media_path"] = media_path
                row.message = updated
        await session.commit()


# --------------------------- caso 3: monto distinto ---------------------------


async def test_wrong_amount_does_not_advance_and_notifies_neutrally(
    session_factory: SessionFactory, media_root: object
) -> None:
    org_id, conversation_id, card_id = await _seed(session_factory)
    await _relink_media(session_factory, org_id, _write_receipt(media_root, org_id, JPEG))

    vision = _StubVision(_valid_extraction(amount="600.00"))
    sender = _RecordingSender()
    outcome = await _validate(session_factory, org_id, conversation_id, vision, sender)

    assert outcome.approved is False
    assert await _stage_name(session_factory, card_id) == stages.PAYMENT_VALIDATION
    assert card_flags.RECEIPT_REVIEW in await _flags(session_factory, card_id)
    # Un solo mensaje, neutro, que no dice qué check falló.
    assert len(sender.texts) == 1
    _, body = sender.texts[0]
    assert body == REVIEW_REPLY
    assert "600" not in body and "monto" not in body.lower()

    receipts = await _receipts(session_factory)
    assert receipts[0].verdict == "fail"
    # Los datos leídos quedan guardados: el operador no tiene que descifrar la imagen.
    assert receipts[0].extracted["amount"] == "600.00"
    assert any(not check["passed"] for check in receipts[0].checks)


# --------------------------- caso 7 y 8: reuso ---------------------------


async def test_same_image_is_not_processed_twice(
    session_factory: SessionFactory, media_root: object
) -> None:
    """Caso 8: el mismo binario reenviado no re-dispara el job ni el mensaje."""
    org_id, conversation_id, _card_id = await _seed(session_factory)
    await _relink_media(session_factory, org_id, _write_receipt(media_root, org_id, JPEG))
    sender = _RecordingSender()
    await _validate(
        session_factory, org_id, conversation_id, _StubVision(_valid_extraction()), sender
    )

    # Llega de nuevo con otro wamid (WhatsApp da uno nuevo por reenvío) pero misma imagen.
    async with session_factory() as session:
        row = (await session.execute(select(AiChatHistory))).scalars().first()
        assert row is not None
        session.add(
            AiChatHistory(
                agent_id=row.agent_id,
                organization_id=org_id,
                thread_id=row.thread_id,
                session_id=row.session_id,
                message={**row.message, "wamid": "wamid.RESENT"},
            )
        )
        await session.commit()

    vision = _StubVision(_valid_extraction())
    outcome = await _validate(
        session_factory, org_id, conversation_id, vision, sender, wamid="wamid.RESENT"
    )
    assert outcome.processed is False
    assert vision.calls == 0  # no se gasta una llamada al modelo
    assert len(sender.texts) == 0  # ni un mensaje nuevo al lead
    assert len(await _receipts(session_factory)) == 1


async def test_reference_already_used_by_another_lead_fails(
    session_factory: SessionFactory, media_root: object
) -> None:
    """Caso 7: la misma transacción no puede pagar dos compras."""
    org_id, conversation_id, card_id = await _seed(session_factory)
    await _relink_media(session_factory, org_id, _write_receipt(media_root, org_id, JPEG))
    # Un comprobante previo ya usó esa transacción (otro lead la reenvió).
    async with session_factory() as session:
        card = await session.get(Card, card_id)
        assert card is not None
        session.add(
            PaymentReceipt(
                organization_id=org_id,
                card_id=card.id,
                wamid="wamid.OTRO",
                image_sha256="otro-sha",
                reference="2P10019819",
                verdict="pass",
                checks=[],
                extracted={},
            )
        )
        await session.commit()

    sender = _RecordingSender()
    outcome = await _validate(
        session_factory, org_id, conversation_id, _StubVision(_valid_extraction()), sender
    )
    assert outcome.approved is False
    assert await _stage_name(session_factory, card_id) == stages.PAYMENT_VALIDATION
    receipts = await _receipts(session_factory)
    new_receipt = next(r for r in receipts if r.wamid == WAMID)
    assert any(c["code"] == "reused" and not c["passed"] for c in new_receipt.checks)
    # La referencia del rechazado NO se guarda: si no, el unique bloquearía al operador
    # que después quiera aprobarlo a mano.
    assert new_receipt.reference is None


# --------------------------- casos 9, 11, 13, 14 ---------------------------


async def test_receipt_without_reference_is_not_approved(
    session_factory: SessionFactory, media_root: object
) -> None:
    org_id, conversation_id, card_id = await _seed(session_factory)
    await _relink_media(session_factory, org_id, _write_receipt(media_root, org_id, JPEG))
    outcome = await _validate(
        session_factory,
        org_id,
        conversation_id,
        _StubVision(_valid_extraction(reference=None)),
        _RecordingSender(),
    )
    assert outcome.approved is False
    assert await _stage_name(session_factory, card_id) == stages.PAYMENT_VALIDATION


async def test_unreadable_image_is_not_approved(
    session_factory: SessionFactory, media_root: object
) -> None:
    """Caso 11: el modelo no pudo leer nada (meme, foto borrosa)."""
    org_id, conversation_id, card_id = await _seed(session_factory)
    await _relink_media(session_factory, org_id, _write_receipt(media_root, org_id, JPEG))
    outcome = await _validate(
        session_factory, org_id, conversation_id, _StubVision({}), _RecordingSender()
    )
    assert outcome.approved is False
    assert await _stage_name(session_factory, card_id) == stages.PAYMENT_VALIDATION
    assert card_flags.RECEIPT_REVIEW in await _flags(session_factory, card_id)


async def test_vision_provider_failure_is_not_a_crash(
    session_factory: SessionFactory, media_root: object
) -> None:
    """Si el proveedor falla, es un comprobante que no se pudo leer — no un error 500."""
    org_id, conversation_id, _card_id = await _seed(session_factory)
    await _relink_media(session_factory, org_id, _write_receipt(media_root, org_id, JPEG))
    outcome = await _validate(
        session_factory,
        org_id,
        conversation_id,
        _StubVision(fail=True),
        _RecordingSender(),
    )
    assert outcome.processed is True
    assert outcome.approved is False


async def test_usd_service_is_not_approved(
    session_factory: SessionFactory, media_root: object
) -> None:
    """Caso 13: sin tasa de cambio en el sistema, el monto se confirma a mano."""
    org_id, conversation_id, _card_id = await _seed(session_factory, currency="USD")
    await _relink_media(session_factory, org_id, _write_receipt(media_root, org_id, JPEG))
    outcome = await _validate(
        session_factory,
        org_id,
        conversation_id,
        _StubVision(_valid_extraction()),
        _RecordingSender(),
    )
    assert outcome.approved is False


async def test_service_with_range_price_is_not_approved(
    session_factory: SessionFactory, media_root: object
) -> None:
    org_id, conversation_id, _card_id = await _seed(session_factory, price=None)
    await _relink_media(session_factory, org_id, _write_receipt(media_root, org_id, JPEG))
    outcome = await _validate(
        session_factory,
        org_id,
        conversation_id,
        _StubVision(_valid_extraction()),
        _RecordingSender(),
    )
    assert outcome.approved is False


async def test_two_accepted_services_is_not_approved(
    session_factory: SessionFactory, media_root: object
) -> None:
    """Caso 14: con dos servicios no hay un precio único contra el que comparar."""
    org_id, conversation_id, _card_id = await _seed(session_factory, services=2)
    await _relink_media(session_factory, org_id, _write_receipt(media_root, org_id, JPEG))
    outcome = await _validate(
        session_factory,
        org_id,
        conversation_id,
        _StubVision(_valid_extraction()),
        _RecordingSender(),
    )
    assert outcome.approved is False


async def test_unconfigured_beneficiary_is_not_approved(
    session_factory: SessionFactory, media_root: object
) -> None:
    """Falla segura del estado en que queda una organización recién migrada."""
    org_id, conversation_id, _card_id = await _seed(session_factory, beneficiary=None)
    await _relink_media(session_factory, org_id, _write_receipt(media_root, org_id, JPEG))
    outcome = await _validate(
        session_factory,
        org_id,
        conversation_id,
        _StubVision(_valid_extraction()),
        _RecordingSender(),
    )
    assert outcome.approved is False


# --------------------------- caso 10: PDF ---------------------------


async def test_pdf_receipt_goes_through_the_same_circuit(
    session_factory: SessionFactory, media_root: object
) -> None:
    org_id, conversation_id, card_id = await _seed(session_factory, mime="application/pdf")
    await _relink_media(
        session_factory, org_id, _write_receipt(media_root, org_id, PDF, ext=".pdf")
    )
    vision = _StubVision(_valid_extraction())
    outcome = await _validate(session_factory, org_id, conversation_id, vision, _RecordingSender())
    assert outcome.approved is True
    assert vision.last_mime == "application/pdf"  # detectado por magic bytes
    assert await _stage_name(session_factory, card_id) == stages.PAYMENT_VALIDATED


async def test_mime_is_detected_from_content_not_from_the_extension(
    session_factory: SessionFactory, media_root: object
) -> None:
    """Un JPEG guardado como `.bin` (mime desconocido) sigue siendo un JPEG."""
    org_id, conversation_id, _card_id = await _seed(session_factory, mime=None)
    await _relink_media(
        session_factory, org_id, _write_receipt(media_root, org_id, JPEG, ext=".bin")
    )
    vision = _StubVision(_valid_extraction())
    await _validate(session_factory, org_id, conversation_id, vision, _RecordingSender())
    assert vision.last_mime == "image/jpeg"


# --------------------------- caso 17: carrera con el humano ---------------------------


async def test_card_moved_by_a_human_meanwhile_is_not_touched(
    session_factory: SessionFactory, media_root: object
) -> None:
    """El job arrancó con la card esperando, pero un operador la descalificó: su
    decisión gana y no hay ni move ni entrega."""
    org_id, conversation_id, card_id = await _seed(session_factory)
    await _relink_media(session_factory, org_id, _write_receipt(media_root, org_id, JPEG))

    class _MovingVision(_StubVision):
        """Mueve la card a otro stage justo cuando el modelo "responde"."""

        async def extract(self, **kwargs: object) -> dict[str, object]:
            async with session_factory() as session:
                other = await BoardRepository(session).get_stage(
                    org_id, stages.PIPELINE_HUMAN, stages.HUMAN_INTAKE
                )
                assert other is not None
                card = await session.get(Card, card_id)
                assert card is not None
                card.stage_id = other.id
                await session.commit()
            return await super().extract(**kwargs)  # type: ignore[arg-type]

    outcome = await _validate(
        session_factory,
        org_id,
        conversation_id,
        _MovingVision(_valid_extraction()),
        _RecordingSender(),
    )
    assert outcome.approved is False
    assert outcome.reason is not None
    assert await _stage_name(session_factory, card_id) == stages.HUMAN_INTAKE
    # Sin move no hay aprobación: el comprobante en verde queda para que el operador lo
    # valide desde el panel, que es lo que el CRM le va a ofrecer.
    receipts = await _receipts(session_factory)
    assert receipts[0].verdict == "pass"
    assert receipts[0].approved_at is None
    assert receipts[0].approved_by is None


# --------------------------- idempotencia y precondición ---------------------------


async def test_already_processed_receipt_is_skipped(
    session_factory: SessionFactory, media_root: object
) -> None:
    org_id, conversation_id, _card_id = await _seed(session_factory)
    await _relink_media(session_factory, org_id, _write_receipt(media_root, org_id, JPEG))
    sender = _RecordingSender()
    await _validate(
        session_factory, org_id, conversation_id, _StubVision(_valid_extraction()), sender
    )

    vision = _StubVision(_valid_extraction())
    outcome = await _validate(session_factory, org_id, conversation_id, vision, sender)
    assert outcome.processed is False
    assert vision.calls == 0
    assert len(await _receipts(session_factory)) == 1


async def test_card_not_awaiting_validation_is_skipped(
    session_factory: SessionFactory, media_root: object
) -> None:
    org_id, conversation_id, _card_id = await _seed(session_factory, stage_name=stages.DELIVERED)
    await _relink_media(session_factory, org_id, _write_receipt(media_root, org_id, JPEG))
    vision = _StubVision(_valid_extraction())
    outcome = await _validate(session_factory, org_id, conversation_id, vision, _RecordingSender())
    assert outcome.processed is False
    assert vision.calls == 0


async def test_missing_file_is_a_failed_check_not_a_crash(
    session_factory: SessionFactory, media_root: object
) -> None:
    """El worker de dev sin volumen compartido: archivo no encontrado ⇒ humano."""
    org_id, conversation_id, card_id = await _seed(
        session_factory, media_path=f"{uuid.uuid4()}/no-existe.jpg"
    )
    vision = _StubVision(_valid_extraction())
    outcome = await _validate(session_factory, org_id, conversation_id, vision, _RecordingSender())
    assert outcome.processed is True
    assert outcome.approved is False
    assert vision.calls == 0  # no se llama al modelo sin archivo
    assert await _stage_name(session_factory, card_id) == stages.PAYMENT_VALIDATION
    assert card_flags.RECEIPT_REVIEW in await _flags(session_factory, card_id)


# --------------- la nota de revisión en el resumen IA (UAT 2026-08-31) ---------------

JPEG_2 = b"\xff\xd8\xff" + b"other-receipt" * 8


async def _summary(session_factory: SessionFactory, conversation_id: uuid.UUID) -> str | None:
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert conversation is not None
        return conversation.ai_summary


async def _set_summary(
    session_factory: SessionFactory, conversation_id: uuid.UUID, text: str
) -> None:
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert conversation is not None
        conversation.ai_summary = text
        await session.commit()


def _write_named(root: object, org_id: uuid.UUID, name: str, content: bytes) -> str:
    import os

    org_dir = os.path.join(str(root), str(org_id))
    os.makedirs(org_dir, exist_ok=True)
    with open(os.path.join(org_dir, name), "wb") as handle:
        handle.write(content)
    return f"{org_id}/{name}"


async def _ingest_second_receipt(
    session_factory: SessionFactory, wamid: str, media_path: str
) -> None:
    """Otro comprobante del mismo lead: copia el turno ingerido con otro wamid y archivo."""
    async with session_factory() as session:
        row = (await session.execute(select(AiChatHistory))).scalars().first()
        assert row is not None
        session.add(
            AiChatHistory(
                agent_id=row.agent_id,
                organization_id=row.organization_id,
                thread_id=row.thread_id,
                session_id=row.session_id,
                message={**row.message, "wamid": wamid, "media_path": media_path},
            )
        )
        await session.commit()


async def test_failed_validation_explains_itself_in_the_ai_summary(
    session_factory: SessionFactory, media_root: object
) -> None:
    """El operador lee primero el resumen: tiene que decir qué subsanar, no solo que
    llegó un comprobante. El detalle es el mismo que muestra el panel."""
    org_id, conversation_id, _card_id = await _seed(session_factory)
    await _relink_media(session_factory, org_id, _write_receipt(media_root, org_id, JPEG))
    await _set_summary(
        session_factory, conversation_id, "Quiere el curso híbrido; mandó su comprobante."
    )

    await _validate(
        session_factory,
        org_id,
        conversation_id,
        _StubVision(_valid_extraction(amount="600.00")),
        _RecordingSender(),
    )

    summary = await _summary(session_factory, conversation_id)
    assert summary is not None
    assert summary.startswith("Quiere el curso híbrido")  # el resumen del handoff sigue ahí
    assert NOTE_MARKER in summary
    assert "600" in summary and "650" in summary  # el detalle del check, no una paráfrasis
    assert "panel de pago" in summary


async def test_a_second_failure_replaces_the_note_instead_of_stacking(
    session_factory: SessionFactory, media_root: object
) -> None:
    org_id, conversation_id, _card_id = await _seed(session_factory)
    await _relink_media(session_factory, org_id, _write_receipt(media_root, org_id, JPEG))
    await _validate(
        session_factory,
        org_id,
        conversation_id,
        _StubVision(_valid_extraction(amount="600.00")),
        _RecordingSender(),
    )
    second = _write_named(media_root, org_id, "wamid.SEGUNDO.jpg", JPEG_2)
    await _ingest_second_receipt(session_factory, "wamid.SEGUNDO", second)

    await _validate(
        session_factory,
        org_id,
        conversation_id,
        _StubVision(_valid_extraction(amount="500.00")),
        _RecordingSender(),
        wamid="wamid.SEGUNDO",
    )

    summary = await _summary(session_factory, conversation_id)
    assert summary is not None
    assert summary.count(NOTE_MARKER) == 1
    assert "500" in summary and "600" not in summary


async def test_an_approval_removes_the_validation_note(
    session_factory: SessionFactory, media_root: object
) -> None:
    """Una nota vieja sobre un pago ya aprobado diría "subsaná algo" que ya no existe."""
    org_id, conversation_id, _card_id = await _seed(session_factory)
    await _relink_media(session_factory, org_id, _write_receipt(media_root, org_id, JPEG))
    await _set_summary(session_factory, conversation_id, "Resumen del handoff.")
    await _validate(
        session_factory,
        org_id,
        conversation_id,
        _StubVision(_valid_extraction(amount="600.00")),
        _RecordingSender(),
    )
    second = _write_named(media_root, org_id, "wamid.BUENO.jpg", JPEG_2)
    await _ingest_second_receipt(session_factory, "wamid.BUENO", second)

    outcome = await _validate(
        session_factory,
        org_id,
        conversation_id,
        _StubVision(_valid_extraction()),
        _RecordingSender(),
        wamid="wamid.BUENO",
    )

    assert outcome.approved is True
    assert await _summary(session_factory, conversation_id) == "Resumen del handoff."


async def test_validation_failure_publishes_receipt_needs_review(
    session_factory: SessionFactory, media_root: object
) -> None:
    """El front invalida board + card + panel al recibirlo; sin el evento lo mostraría
    igual el poll — esto solo lo adelanta."""
    org_id, conversation_id, card_id = await _seed(session_factory)
    await _relink_media(session_factory, org_id, _write_receipt(media_root, org_id, JPEG))
    publisher = _RecordingPublisher()

    await _validate(
        session_factory,
        org_id,
        conversation_id,
        _StubVision(_valid_extraction(amount="600.00")),
        _RecordingSender(),
        publisher=publisher,
    )

    payloads = [message for _channel, message in publisher.events]
    needs_review = [
        p for p in payloads if isinstance(p, dict) and p.get("type") == "receipt_needs_review"
    ]
    assert len(needs_review) == 1
    assert needs_review[0]["card_id"] == str(card_id)
    assert needs_review[0]["conversation_id"] == str(conversation_id)


# ----------- imagen reusada desde otra conversación (UAT 2026-08-31) -----------


async def _second_conversation_awaiting_payment(
    session_factory: SessionFactory,
    org_id: uuid.UUID,
    *,
    external_id: str,
    wamid: str,
    media_path: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Otra conversación de la misma org, con su card en "Por validar pago" y el turno
    del comprobante ya ingerido. Sin servicio aceptado: el camino del duplicado no
    llega a comparar precios."""
    async with session_factory() as session:
        agent = (
            (await session.execute(select(Agent).where(Agent.organization_id == org_id)))
            .scalars()
            .first()
        )
        assert agent is not None
        instance = (
            (await session.execute(select(AgentInstance).where(AgentInstance.agent_id == agent.id)))
            .scalars()
            .first()
        )
        assert instance is not None
        conversation = Conversation(
            instance_id=instance.id,
            organization_id=org_id,
            external_id=external_id,
            funnel_stage=FunnelStage.HANDED_OFF,
            is_ai_active=False,
            full_name="Otro Lead",
        )
        session.add(conversation)
        await session.flush()
        stage = await BoardRepository(session).get_stage(
            org_id, stages.PIPELINE_HUMAN, stages.PAYMENT_VALIDATION
        )
        assert stage is not None
        card = Card(
            organization_id=org_id,
            conversation_id=conversation.id,
            stage_id=stage.id,
            title="Otro Lead",
        )
        session.add(card)
        await session.flush()
        session.add(
            AiChatHistory(
                agent_id=agent.id,
                organization_id=org_id,
                thread_id=str(conversation.id),
                session_id=external_id,
                message={
                    "role": "user",
                    "content": "[image: sin descripción]",
                    "wamid": wamid,
                    "channel": "whatsapp",
                    "media_type": "image",
                    "media_path": media_path,
                    "media_mime": "image/jpeg",
                },
            )
        )
        await session.commit()
        return conversation.id, card.id


async def test_duplicate_image_from_another_conversation_leaves_a_trace(
    session_factory: SessionFactory, media_root: object
) -> None:
    """Caso 8 tenía la mitad buena (el mismo lead reenvía: silencio). La otra mitad —
    la misma captura desde OTRA conversación — se descartaba sin rastro: card esperando,
    panel vacío, nadie enterado. Tiene forma de fraude y queda para revisión."""
    org_id, conversation_id, _card_id = await _seed(session_factory)
    path = _write_receipt(media_root, org_id, JPEG)
    await _relink_media(session_factory, org_id, path)
    await _validate(
        session_factory,
        org_id,
        conversation_id,
        _StubVision(_valid_extraction()),
        _RecordingSender(),
    )

    other_conv, other_card = await _second_conversation_awaiting_payment(
        session_factory, org_id, external_id="59170000888", wamid="wamid.STOLEN", media_path=path
    )
    vision = _StubVision(_valid_extraction())
    sender = _RecordingSender()
    outcome = await _validate(
        session_factory, org_id, other_conv, vision, sender, wamid="wamid.STOLEN"
    )

    assert outcome.processed is True
    assert outcome.approved is False
    assert vision.calls == 0  # la imagen ya se conoce: no se gasta una llamada
    assert sender.texts == [(("59170000888"), REVIEW_REPLY)]
    assert card_flags.RECEIPT_REVIEW in await _flags(session_factory, other_card)
    receipts = await _receipts(session_factory)
    trace = next(r for r in receipts if r.wamid == "wamid.STOLEN")
    # El sha placeholder deja el unique anti-reuso en manos de la fila original.
    assert trace.image_sha256 == "reused:wamid.STOLEN"
    assert trace.verdict == "fail"
    assert any(c["code"] == "reused" and not c["passed"] for c in trace.checks)
    summary = await _summary(session_factory, other_conv)
    assert summary is not None and "otra conversación" in summary

    # Un segundo intento con la misma captura vuelve a dejar rastro y a avisar: cada
    # intento es evidencia, no ruido — la asimetría con el reenvío del mismo lead es
    # deliberada (ahí la fila con el sha real ya existe y responde por él).
    second = _write_named(media_root, org_id, "wamid.STOLEN2.jpg", JPEG)
    async with session_factory() as session:
        agent = (
            (await session.execute(select(Agent).where(Agent.organization_id == org_id)))
            .scalars()
            .first()
        )
        assert agent is not None
        session.add(
            AiChatHistory(
                agent_id=agent.id,
                organization_id=org_id,
                thread_id=str(other_conv),
                session_id="59170000888",
                message={
                    "role": "user",
                    "content": "[image: sin descripción]",
                    "wamid": "wamid.STOLEN2",
                    "channel": "whatsapp",
                    "media_type": "image",
                    "media_path": second,
                    "media_mime": "image/jpeg",
                },
            )
        )
        await session.commit()
    await _validate(session_factory, org_id, other_conv, vision, sender, wamid="wamid.STOLEN2")
    assert len(sender.texts) == 2
    assert len(await _receipts(session_factory)) == 3
