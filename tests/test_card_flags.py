"""Avisos operativos de la card (`crm/domain/card_flags.py`) — lógica pura, sin DB."""

from __future__ import annotations

from server.modules.crm.domain import card_flags as flags


def test_add_is_idempotent_and_preserves_order() -> None:
    result = flags.add([], flags.NEEDS_NAME, flags.DELIVERY_PENDING)
    assert result == [flags.NEEDS_NAME, flags.DELIVERY_PENDING]
    assert flags.add(result, flags.NEEDS_NAME) == result


def test_remove_is_idempotent() -> None:
    start = [flags.NEEDS_NAME, flags.EXTRA_RECEIPT]
    assert flags.remove(start, flags.NEEDS_NAME) == [flags.EXTRA_RECEIPT]
    assert flags.remove([flags.EXTRA_RECEIPT], flags.NEEDS_NAME) == [flags.EXTRA_RECEIPT]


def test_unknown_flags_are_dropped_not_raised() -> None:
    """La columna es JSON: un código de otra versión no debe romper el tablero."""
    assert flags.normalize(["needs_name", "codigo_viejo", 42, None]) == [flags.NEEDS_NAME]
    assert flags.add([], "no_existe") == []


def test_normalize_tolerates_non_list_values() -> None:
    assert flags.normalize(None) == []
    assert flags.normalize("needs_name") == []
    assert flags.normalize({"needs_name": True}) == []


def test_normalize_deduplicates() -> None:
    assert flags.normalize([flags.NEEDS_NAME, flags.NEEDS_NAME]) == [flags.NEEDS_NAME]


def test_has() -> None:
    assert flags.has([flags.DELIVERY_PENDING], flags.DELIVERY_PENDING) is True
    assert flags.has([], flags.DELIVERY_PENDING) is False
    assert flags.has(None, flags.DELIVERY_PENDING) is False


def test_replace_delivery_flags_clears_previous_attempt_but_keeps_the_rest() -> None:
    """Un reintento limpia lo que dijo el intento anterior, pero no borra un aviso de
    otra naturaleza: el comprobante extra sigue necesitando revisión humana igual."""
    before = [flags.EXTRA_RECEIPT, flags.MISSING_LINK, flags.DELIVERY_PENDING]
    after = flags.replace_delivery_flags(before, flags.NEEDS_NAME)
    assert flags.EXTRA_RECEIPT in after
    assert flags.MISSING_LINK not in after
    assert flags.DELIVERY_PENDING not in after
    assert flags.NEEDS_NAME in after


def test_replace_delivery_flags_with_nothing_clears_them() -> None:
    """Entrega exitosa: se van todos los avisos del intento y no queda ninguno nuevo."""
    assert flags.replace_delivery_flags([flags.DELIVERY_PENDING, flags.NO_MODALITY]) == []


def test_merge_preserves_order_without_duplicates() -> None:
    merged = flags.merge([flags.NEEDS_NAME], [flags.NEEDS_NAME, flags.EXTRA_RECEIPT])
    assert merged == [flags.NEEDS_NAME, flags.EXTRA_RECEIPT]
