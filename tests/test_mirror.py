"""Test del merge puro del hilo espejo (`build_thread`). Sin DB."""

from __future__ import annotations

from datetime import UTC, datetime

from server.modules.agent.domain.models import AiChatHistory, AppChatHistory
from server.modules.crm.services.mirror import build_thread

BASE_URL = "https://media.example.com"


def _ai(role: str, content: str, minute: int, **extra: object) -> AiChatHistory:
    row = AiChatHistory(
        agent_id=None,
        organization_id=None,
        thread_id="t",
        session_id="s",
        message={"role": role, "content": content, **extra},
    )
    row.created_at = datetime(2026, 6, 6, 12, minute, tzinfo=UTC)
    return row


def _app(
    message: str, minute: int, media_type: str | None = None, media_url: str | None = None
) -> AppChatHistory:
    row = AppChatHistory(
        agent_id=None,
        organization_id=None,
        session_id="s",
        sender="Mirko",
        message=message,
        media_type=media_type,
        media_url=media_url,
    )
    row.message_time = datetime(2026, 6, 6, 12, minute, tzinfo=UTC)
    return row


def test_merges_three_sources_in_time_order() -> None:
    ai_rows = [_ai("user", "hola", 0), _ai("assistant", "buenas!", 1)]
    app_rows = [_app("te ayudo yo", 2)]

    thread = build_thread(ai_rows, app_rows, BASE_URL)

    assert [(m.sender, m.text) for m in thread] == [
        ("lead", "hola"),
        ("agent", "buenas!"),
        ("human", "te ayudo yo"),
    ]


def test_interleaves_by_timestamp() -> None:
    # app llega ENTRE los dos turnos de IA → debe quedar en el medio.
    ai_rows = [_ai("user", "primero", 0), _ai("assistant", "tercero", 2)]
    app_rows = [_app("segundo", 1)]

    thread = build_thread(ai_rows, app_rows, BASE_URL)

    assert [m.text for m in thread] == ["primero", "segundo", "tercero"]


def test_role_maps_to_sender() -> None:
    thread = build_thread([_ai("user", "x", 0), _ai("assistant", "y", 1)], [], BASE_URL)
    assert thread[0].sender == "lead"
    assert thread[1].sender == "agent"


def test_plain_text_has_no_media() -> None:
    thread = build_thread([_ai("user", "hola", 0)], [], BASE_URL)
    assert thread[0].type == "text"
    assert thread[0].media_url is None


def test_image_message_carries_media_url() -> None:
    row = _ai(
        "user",
        "[image: una foto]",
        0,
        media_type="image",
        media_path="org-1/wamid-1.jpg",
    )
    thread = build_thread([row], [], BASE_URL)
    assert thread[0].type == "image"
    assert thread[0].media_url == "https://media.example.com/media/org-1/wamid-1.jpg"
    assert thread[0].text == "[image: una foto]"


def test_document_message_carries_media_url() -> None:
    row = _ai(
        "user",
        "[document: contrato.pdf]",
        0,
        media_type="document",
        media_path="org-1/wamid-2.pdf",
    )
    thread = build_thread([row], [], BASE_URL)
    assert thread[0].type == "document"
    assert thread[0].media_url == "https://media.example.com/media/org-1/wamid-2.pdf"


def test_media_type_without_path_falls_back_to_text() -> None:
    row = _ai("user", "[image: sin path]", 0, media_type="image")
    thread = build_thread([row], [], BASE_URL)
    assert thread[0].type == "text"
    assert thread[0].media_url is None


def test_outbound_absolute_media_url_used_as_is() -> None:
    # #175: la media saliente (materiales del catálogo / QR) trae una URL absoluta;
    # el mirror la usa tal cual, sin anteponer la base del mount `/media`.
    row = _ai(
        "assistant",
        "Te paso la info del curso 🙌",
        0,
        media_type="document",
        media_url="https://cdn.catalog.test/curso-edicion.pdf",
    )
    thread = build_thread([row], [], BASE_URL)
    assert thread[0].sender == "agent"
    assert thread[0].type == "document"
    assert thread[0].media_url == "https://cdn.catalog.test/curso-edicion.pdf"


def test_human_media_message_carries_media_url() -> None:
    # #251: adjunto/QR enviado por el humano en takeover — URL absoluta, se usa tal cual.
    row = _app("te paso el QR", 0, media_type="image", media_url="https://qr.test/pago.jpg")
    thread = build_thread([], [row], BASE_URL)
    assert thread[0].sender == "human"
    assert thread[0].type == "image"
    assert thread[0].media_url == "https://qr.test/pago.jpg"


def test_human_media_without_url_falls_back_to_text() -> None:
    row = _app("hola", 0, media_type="image")
    thread = build_thread([], [row], BASE_URL)
    assert thread[0].type == "text"
    assert thread[0].media_url is None


def test_system_row_maps_to_error_event() -> None:
    # server#288: la fila `role: system` (el agente falló el turno) se proyecta como
    # evento de error del hilo, con la categoría como código para que el CRM la traduzca.
    row = _ai(
        "system",
        "El agente IA no pudo responder este mensaje.",
        1,
        kind="agent_error",
        category="provider",
        detail="Internal server error",
    )
    thread = build_thread([_ai("user", "hola", 0), row], [], BASE_URL)
    assert thread[1].sender == "system"
    assert thread[1].type == "error"
    assert thread[1].category == "provider"
    assert thread[1].text == "El agente IA no pudo responder este mensaje."
    assert thread[1].media_url is None


def test_system_row_without_category_still_renders() -> None:
    row = _ai("system", "El agente IA no pudo responder este mensaje.", 0, kind="agent_error")
    thread = build_thread([row], [], BASE_URL)
    assert thread[0].sender == "system"
    assert thread[0].type == "error"
    assert thread[0].category is None


def test_absolute_media_url_takes_precedence_over_path() -> None:
    row = _ai(
        "assistant",
        "QR de pago",
        0,
        media_type="image",
        media_url="https://qr.test/pago.jpg",
        media_path="org-1/ignored.jpg",
    )
    thread = build_thread([row], [], BASE_URL)
    assert thread[0].media_url == "https://qr.test/pago.jpg"
