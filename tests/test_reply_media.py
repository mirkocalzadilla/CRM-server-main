"""Adjuntos y QR de pago del takeover (#251): validación/persistencia del archivo,
envío por WhatsApp (imagen vs documento) y espejo como mensaje humano con media.
Seed mínimo sobre SQLite; el canal de salida se stubea.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.agent.domain.models import (
    Agent,
    AgentInstance,
    AppChatHistory,
    Conversation,
    Product,
)
from server.modules.crm.domain.models import Card, Pipeline, Stage, StageStatus
from server.modules.crm.domain.payment_models import PaymentSettings
from server.modules.crm.services import media_store
from server.modules.crm.services.media_store import StoredMedia, store_outbound_media
from server.modules.crm.services.reply_service import ReplyService
from server.shared.exceptions import (
    ExternalServiceError,
    ValidationException,
)

LEAD_WA_ID = "59170000000"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 16
PDF_BYTES = b"%PDF-1.7 fake"


class _StubSender:
    """Registra envíos; opcionalmente falla (Meta caído)."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.images: list[tuple[str, str, str]] = []  # (to, link, caption)
        self.documents: list[tuple[str, str, str, str]] = []  # (to, link, filename, caption)

    async def send_text(self, to: str, body: str) -> None:
        raise AssertionError("media send must not fall back to text")

    async def send_image(self, to: str, link: str, caption: str = "") -> None:
        if self.fail:
            raise httpx.HTTPError("meta caído")
        self.images.append((to, link, caption))

    async def send_document(self, to: str, link: str, filename: str, caption: str = "") -> None:
        if self.fail:
            raise httpx.HTTPError("meta caído")
        self.documents.append((to, link, filename, caption))


async def _seed_card(
    session_factory: async_sessionmaker[AsyncSession], *, is_ai_active: bool = False
) -> tuple[uuid.UUID, uuid.UUID]:
    """Chain mínimo conversation+card. Devuelve (org_id, card_id)."""
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
            is_ai_active=is_ai_active,
        )
        session.add(conversation)
        pipeline = Pipeline(organization_id=org_id, kind="human", name="Gestión Humana", position=1)
        session.add(pipeline)
        await session.flush()
        stage = Stage(pipeline_id=pipeline.id, name="Nuevo", position=1, status_code="open")
        session.add(stage)
        await session.flush()
        card = Card(
            organization_id=org_id,
            conversation_id=conversation.id,
            stage_id=stage.id,
            title="Lead",
        )
        session.add(card)
        await session.commit()
        return org_id, card.id


def _service(session: AsyncSession, sender: _StubSender) -> ReplyService:
    return ReplyService(session=session, sender=sender)


async def _app_rows(session: AsyncSession) -> list[AppChatHistory]:
    result = await session.execute(select(AppChatHistory))
    return list(result.scalars().all())


async def test_send_image_delivers_and_mirrors_media(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id, card_id = await _seed_card(session_factory)
    sender = _StubSender()
    media = StoredMedia(media_type="image", url="https://media.test/crm/x.jpg", filename="x.jpg")
    async with session_factory() as session:
        message = await _service(session, sender).send_human_media(
            card_id, media, "mirá esto", uuid.uuid4(), org_id
        )
    assert sender.images == [(LEAD_WA_ID, "https://media.test/crm/x.jpg", "mirá esto")]
    assert message.sender == "human"
    assert message.type == "image"
    assert message.media_url == "https://media.test/crm/x.jpg"
    async with session_factory() as session:
        rows = await _app_rows(session)
        assert len(rows) == 1
        assert rows[0].media_type == "image"
        assert rows[0].media_url == "https://media.test/crm/x.jpg"
        assert rows[0].message == "mirá esto"


async def test_send_document_uses_filename(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id, card_id = await _seed_card(session_factory)
    sender = _StubSender()
    media = StoredMedia(
        media_type="document", url="https://media.test/crm/c.pdf", filename="contrato.pdf"
    )
    async with session_factory() as session:
        message = await _service(session, sender).send_human_media(
            card_id, media, "", uuid.uuid4(), org_id
        )
    assert sender.documents == [(LEAD_WA_ID, "https://media.test/crm/c.pdf", "contrato.pdf", "")]
    assert message.type == "document"


async def test_send_payment_qr_uses_global_url_without_org_config(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sin config de pagos propia, el QR manual sigue siendo el global (server#268)."""
    from server.modules.crm.services import payment_settings_service as settings_module

    monkeypatch.setattr(
        settings_module,
        "get_settings",
        lambda: SimpleNamespace(payment_qr_url="https://qr.test/pago.jpg"),
    )
    org_id, card_id = await _seed_card(session_factory)
    sender = _StubSender()
    async with session_factory() as session:
        message = await _service(session, sender).send_payment_qr(card_id, uuid.uuid4(), org_id)
    assert sender.images == [(LEAD_WA_ID, "https://qr.test/pago.jpg", "")]
    assert message.type == "image"
    assert message.media_url == "https://qr.test/pago.jpg"


async def test_send_payment_qr_prefers_org_configured_url(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Con QR propio cargado, el envío manual usa el de la organización (server#268)."""
    org_id, card_id = await _seed_card(session_factory)
    async with session_factory() as session:
        session.add(
            PaymentSettings(
                organization_id=org_id, payment_qr_url="https://qr.test/mirko-propio.png"
            )
        )
        await session.commit()
    sender = _StubSender()
    async with session_factory() as session:
        message = await _service(session, sender).send_payment_qr(card_id, uuid.uuid4(), org_id)
    assert sender.images == [(LEAD_WA_ID, "https://qr.test/mirko-propio.png", "")]
    assert message.media_url == "https://qr.test/mirko-propio.png"


async def test_media_requires_takeover(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id, card_id = await _seed_card(session_factory, is_ai_active=True)
    media = StoredMedia(media_type="image", url="https://media.test/x.jpg", filename="x.jpg")
    async with session_factory() as session:
        with pytest.raises(ValidationException):
            await _service(session, _StubSender()).send_human_media(
                card_id, media, "", uuid.uuid4(), org_id
            )


async def test_failed_send_does_not_mirror(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id, card_id = await _seed_card(session_factory)
    media = StoredMedia(media_type="image", url="https://media.test/x.jpg", filename="x.jpg")
    async with session_factory() as session:
        with pytest.raises(ExternalServiceError):
            await _service(session, _StubSender(fail=True)).send_human_media(
                card_id, media, "", uuid.uuid4(), org_id
            )
    async with session_factory() as session:
        assert await _app_rows(session) == []


# ---------- store_outbound_media (validación + persistencia en disco) ----------


@pytest.fixture
def _media_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(
        media_store,
        "get_settings",
        lambda: SimpleNamespace(media_root=str(tmp_path), media_base_url="https://media.test"),
    )
    return tmp_path


def test_store_image_writes_file_and_builds_url(_media_settings: Path) -> None:
    org_id = uuid.uuid4()
    stored = store_outbound_media(
        organization_id=org_id, content_type="image/png", filename="Foto ñoña.png", data=PNG_BYTES
    )
    assert stored.media_type == "image"
    assert stored.url.startswith(f"https://media.test/media/crm/{org_id}/")
    assert stored.url.endswith(".png")
    saved = list((_media_settings / "crm" / str(org_id)).iterdir())
    assert len(saved) == 1
    assert saved[0].read_bytes() == PNG_BYTES


def test_store_pdf_is_document_with_sanitized_name(_media_settings: Path) -> None:
    stored = store_outbound_media(
        organization_id=uuid.uuid4(),
        content_type="application/pdf",
        filename="Contrato Anual.pdf",
        data=PDF_BYTES,
    )
    assert stored.media_type == "document"
    assert stored.filename == "Contrato-Anual.pdf"


def test_store_rejects_unknown_mime(_media_settings: Path) -> None:
    with pytest.raises(ValidationException):
        store_outbound_media(
            organization_id=uuid.uuid4(),
            content_type="video/mp4",
            filename="v.mp4",
            data=b"0000",
        )


def test_store_rejects_magic_bytes_mismatch(_media_settings: Path) -> None:
    # Declara PNG pero el contenido no lo es → rechazado (no confiar en el MIME).
    with pytest.raises(ValidationException):
        store_outbound_media(
            organization_id=uuid.uuid4(),
            content_type="image/png",
            filename="x.png",
            data=b"not a png",
        )


def test_store_rejects_oversize(_media_settings: Path) -> None:
    big = PNG_BYTES + b"0" * media_store.MATERIAL_MAX_BYTES
    with pytest.raises(ValidationException):
        store_outbound_media(
            organization_id=uuid.uuid4(), content_type="image/png", filename="x.png", data=big
        )
