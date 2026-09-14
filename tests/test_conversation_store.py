"""Tests de la conversión pura `ai_chat_histories.message` → `llm_port.Message`.

El adapter ligado a DB (`ConversationStoreAdapter.load/save_turn`) se valida en el
e2e contra Postgres real (enum/JSON no van en SQLite). Acá solo la función pura.
"""

from __future__ import annotations

from server.modules.agent.domain.llm_port import Message, Role
from server.modules.agent.services.conversation_store import (
    _trim_to_first_user,
    stored_message_to_llm,
    window_up_to_latest_user,
)


def _u(text: str) -> Message:
    return Message(role=Role.USER, text=text)


def _a(text: str) -> Message:
    return Message(role=Role.ASSISTANT, text=text)


def test_trim_drops_leading_assistant_turns() -> None:
    # Anthropic 400s if the array starts with assistant; the recency window can begin
    # on an assistant turn once history scrolls. The trim must start at the first user.
    window = (_a("buenas"), _u("precio?"), _a("480 Bs"), _u("dale"))
    assert _trim_to_first_user(window)[0].role is Role.USER
    assert _trim_to_first_user(window) == (_u("precio?"), _a("480 Bs"), _u("dale"))


def test_trim_keeps_window_starting_with_user() -> None:
    window = (_u("hola"), _a("buenas"))
    assert _trim_to_first_user(window) == window


def test_trim_empty_when_no_user_turn() -> None:
    assert _trim_to_first_user((_a("a"), _a("b"))) == ()
    assert _trim_to_first_user(()) == ()


def test_user_message_maps_to_user_role() -> None:
    msg = stored_message_to_llm({"role": "user", "content": "hola", "wamid": "x"})
    assert msg is not None
    assert msg.role is Role.USER
    assert msg.text == "hola"


def test_assistant_message_maps_to_assistant_role() -> None:
    msg = stored_message_to_llm({"role": "assistant", "content": "buenas", "channel": "whatsapp"})
    assert msg is not None
    assert msg.role is Role.ASSISTANT
    assert msg.text == "buenas"


def test_unknown_role_is_skipped() -> None:
    assert stored_message_to_llm({"role": "system", "content": "x"}) is None
    assert stored_message_to_llm({"content": "sin rol"}) is None


def test_missing_content_yields_empty_text() -> None:
    msg = stored_message_to_llm({"role": "user"})
    assert msg is not None
    assert msg.text == ""


def test_window_ends_at_latest_user_and_reports_its_order() -> None:
    ordered = [(519, _u("info")), (520, _a("hola")), (521, _u("cuando?"))]
    window, latest = window_up_to_latest_user(ordered)
    assert latest == 521
    assert window == (_u("info"), _a("hola"), _u("cuando?"))


def test_late_reply_outranking_a_newer_inbound_is_dropped() -> None:
    # #240 (el bug): la respuesta al 521 se guardó tarde y quedó con orden 523, por encima
    # del inbound 522 que llegó con el turno en vuelo. La ventana debe cortar en 522 (el
    # último turno del lead) y descartar la 523, para terminar en el mensaje sin responder.
    ordered = [
        (519, _u("info")),
        (520, _a("hola")),
        (521, _u("cuando inicia?")),
        (522, _u("cuanto cuesta?")),  # llegó con el turno anterior en vuelo
        (523, _a("dejame revisar...")),  # respuesta al 521, persistida tarde (orden mayor)
    ]
    window, latest = window_up_to_latest_user(ordered)
    assert latest == 522  # el inbound sin responder, no la respuesta 523
    assert window[-1] == _u("cuanto cuesta?")  # la ventana termina en el lead
    assert _a("dejame revisar...") not in window


def test_no_user_turn_yields_empty_window_and_no_order() -> None:
    window, latest = window_up_to_latest_user([(1, _a("a")), (2, _a("b"))])
    assert latest is None
    assert window == ()
