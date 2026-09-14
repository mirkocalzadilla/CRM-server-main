"""Delivery plan (`crm/domain/delivery.py`) — pure logic, no DB and no sending.

Covers the three delivery shapes and, above all, the cases that do NOT ship on their own:
the criterion is that a lead never receives a half-written message, and that a delivery
already paid for is not withheld over an administrative gap.
"""

from __future__ import annotations

from datetime import UTC, datetime

from server.modules.crm.domain import card_flags
from server.modules.crm.domain.delivery import (
    ENTRY_CAPTION,
    HYBRID_ENTRY_CAPTION,
    HYBRID_VIRTUAL_INTRO,
    LINK_MAPS,
    LINK_MEETING,
    LINK_OTHER,
    LINK_WHATSAPP_GROUP,
    MODALITY_HYBRID,
    MODALITY_PRESENCIAL,
    MODALITY_VIRTUAL,
    PAYMENT_CONFIRMED,
    PAYMENT_CONFIRMED_PENDING,
    VIRTUAL_INTRO,
    EventInfo,
    LinkRef,
    plan_delivery,
)


def test_presencial_with_maps_sends_entry_and_location_in_one_message() -> None:
    plan = plan_delivery(MODALITY_PRESENCIAL, [LinkRef(LINK_MAPS, "https://maps.app.goo.gl/abc")])
    assert plan.is_blocked is False
    assert plan.needs_entry is True
    assert "https://maps.app.goo.gl/abc" in plan.entry_caption
    assert ENTRY_CAPTION in plan.entry_caption
    assert plan.warnings == ()
    assert plan.text == ""  # la entrada viaja con su caption, sin texto aparte


def test_presencial_without_maps_still_delivers_and_warns() -> None:
    """La entrada es la entrega; la ubicación es complemento. No se retiene algo ya
    pagado por un link que falta — se entrega y se avisa."""
    plan = plan_delivery(MODALITY_PRESENCIAL, [])
    assert plan.is_blocked is False
    assert plan.needs_entry is True
    assert plan.entry_caption.startswith(ENTRY_CAPTION)
    assert "Ubicación:" not in plan.entry_caption
    assert plan.warnings == (card_flags.MISSING_LINK,)


def test_presencial_ignores_links_of_other_kinds() -> None:
    plan = plan_delivery(
        MODALITY_PRESENCIAL, [LinkRef(LINK_WHATSAPP_GROUP, "https://chat.whatsapp.com/x")]
    )
    assert plan.needs_entry is True
    assert "whatsapp.com" not in plan.entry_caption
    assert plan.warnings == (card_flags.MISSING_LINK,)


def test_virtual_sends_group_and_meeting_in_order() -> None:
    plan = plan_delivery(
        MODALITY_VIRTUAL,
        [
            LinkRef(LINK_MEETING, "https://meet.google.com/xyz"),
            LinkRef(LINK_WHATSAPP_GROUP, "https://chat.whatsapp.com/abc"),
        ],
    )
    assert plan.is_blocked is False
    assert plan.needs_entry is False
    lines = plan.text.splitlines()
    assert "Grupo de WhatsApp: https://chat.whatsapp.com/abc" in lines
    assert "Link de la clase: https://meet.google.com/xyz" in lines
    # El grupo va antes que la reunión, independientemente del orden de carga.
    assert lines.index("Grupo de WhatsApp: https://chat.whatsapp.com/abc") < lines.index(
        "Link de la clase: https://meet.google.com/xyz"
    )


def test_virtual_with_only_one_link_is_enough() -> None:
    plan = plan_delivery(MODALITY_VIRTUAL, [LinkRef(LINK_WHATSAPP_GROUP, "https://chat.w/x")])
    assert plan.is_blocked is False
    assert "https://chat.w/x" in plan.text


def test_virtual_uses_operator_label_when_present() -> None:
    plan = plan_delivery(
        MODALITY_VIRTUAL,
        [LinkRef(LINK_WHATSAPP_GROUP, "https://chat.w/x", "Grupo del curso de agosto")],
    )
    assert "Grupo del curso de agosto: https://chat.w/x" in plan.text


def test_virtual_without_links_goes_to_a_human() -> None:
    """Jamás un mensaje vacío ni un "None" al lead."""
    plan = plan_delivery(MODALITY_VIRTUAL, [])
    assert plan.is_blocked is True
    assert plan.blocked_by == card_flags.MISSING_LINK
    assert plan.text == ""


def test_virtual_ignores_maps_only() -> None:
    plan = plan_delivery(MODALITY_VIRTUAL, [LinkRef(LINK_MAPS, "https://maps.app/x")])
    assert plan.is_blocked is True
    assert plan.blocked_by == card_flags.MISSING_LINK


def test_blank_url_counts_as_missing() -> None:
    plan = plan_delivery(MODALITY_VIRTUAL, [LinkRef(LINK_WHATSAPP_GROUP, "   ")])
    assert plan.is_blocked is True


def test_no_modality_blocks_delivery() -> None:
    """Default seguro: sin modalidad no se entrega nada, aunque haya links cargados."""
    plan = plan_delivery(None, [LinkRef(LINK_WHATSAPP_GROUP, "https://chat.w/x")])
    assert plan.is_blocked is True
    assert plan.blocked_by == card_flags.NO_MODALITY
    assert plan.needs_entry is False


def test_unknown_modality_is_treated_as_none() -> None:
    """Si alguien escribió un valor a mano en la DB, no se adivina."""
    plan = plan_delivery("híbrido", [LinkRef(LINK_MAPS, "https://maps.app/x")])
    assert plan.blocked_by == card_flags.NO_MODALITY


def test_no_opening_punctuation_in_lead_facing_copy() -> None:
    """Regla dura de la persona: sin `¡` ni `¿` en lo que ve el lead."""
    plan = plan_delivery(MODALITY_VIRTUAL, [LinkRef(LINK_WHATSAPP_GROUP, "https://chat.w/x")])
    for text in (plan.text, plan.entry_caption, ENTRY_CAPTION):
        assert "¡" not in text
        assert "¿" not in text


# --------------------------- hybrid: in person AND virtual ---------------------------
#
# Mirko's only QR-paid service is a 4-module course: one module in person, three over
# Zoom, one payment. Neither `presencial` nor `virtual` delivers that course whole, which
# is why this modality exists.


def test_hybrid_sends_entry_location_and_meeting_links_in_one_message() -> None:
    plan = plan_delivery(
        MODALITY_HYBRID,
        [
            LinkRef(LINK_MAPS, "https://maps.app.goo.gl/abc"),
            LinkRef(LINK_MEETING, "https://zoom.us/j/123"),
        ],
    )

    assert plan.is_blocked is False
    # The QR ships: the in-person module needs something to show at the door.
    assert plan.needs_entry is True
    assert HYBRID_ENTRY_CAPTION in plan.entry_caption
    # And the location and the Zoom link travel with it, in the same message.
    assert "https://maps.app.goo.gl/abc" in plan.entry_caption
    assert "https://zoom.us/j/123" in plan.entry_caption
    assert HYBRID_VIRTUAL_INTRO in plan.entry_caption
    assert plan.text == ""
    assert plan.warnings == ()


def test_hybrid_does_not_say_the_day_of_the_event() -> None:
    """The course has four dates and the QR opens one: "el día del evento" would mislead."""
    plan = plan_delivery(MODALITY_HYBRID, [LinkRef(LINK_MEETING, "https://zoom.us/j/1")])

    assert "el día del evento" not in plan.entry_caption
    assert "módulo presencial" in plan.entry_caption
    # And it is not the presencial copy either.
    assert ENTRY_CAPTION not in plan.entry_caption


def test_hybrid_without_meeting_links_still_delivers_the_entry() -> None:
    """Same rule as presencial without a location: what is paid for is not withheld."""
    plan = plan_delivery(MODALITY_HYBRID, [LinkRef(LINK_MAPS, "https://maps.app/x")])

    assert plan.is_blocked is False
    assert plan.needs_entry is True
    assert "https://maps.app/x" in plan.entry_caption
    assert HYBRID_VIRTUAL_INTRO not in plan.entry_caption
    assert plan.warnings == (card_flags.MISSING_LINK,)


def test_hybrid_without_location_still_delivers_the_entry() -> None:
    plan = plan_delivery(MODALITY_HYBRID, [LinkRef(LINK_WHATSAPP_GROUP, "https://chat.w/x")])

    assert plan.needs_entry is True
    assert "https://chat.w/x" in plan.entry_caption
    assert plan.warnings == (card_flags.MISSING_LINK,)


def test_hybrid_with_nothing_loaded_delivers_the_entry_with_a_single_notice() -> None:
    """Two things missing, one notice: the card shows a problem, not the same one twice."""
    plan = plan_delivery(MODALITY_HYBRID, [])

    assert plan.is_blocked is False
    assert plan.needs_entry is True
    assert plan.entry_caption.startswith(HYBRID_ENTRY_CAPTION)
    assert HYBRID_VIRTUAL_INTRO not in plan.entry_caption
    assert plan.warnings == (card_flags.MISSING_LINK,)


def test_hybrid_delivers_both_meeting_links_when_both_exist() -> None:
    plan = plan_delivery(
        MODALITY_HYBRID,
        [
            LinkRef(LINK_WHATSAPP_GROUP, "https://chat.w/g"),
            LinkRef(LINK_MEETING, "https://zoom.us/j/9"),
        ],
    )

    assert "https://chat.w/g" in plan.entry_caption
    assert "https://zoom.us/j/9" in plan.entry_caption


def test_hybrid_delivers_the_material_link_labeled_after_the_class_link() -> None:
    """UAT 2026-08-31: a hybrid course with a Meet link plus a material link loaded as
    `other` must deliver both — the material used to be dropped silently."""
    plan = plan_delivery(
        MODALITY_HYBRID,
        [
            LinkRef(LINK_OTHER, "https://drive.google.com/mat", "Materia del estudio"),
            LinkRef(LINK_MEETING, "https://meet.google.com/xyz"),
            LinkRef(LINK_MAPS, "https://maps.app/sede"),
        ],
    )

    assert plan.is_blocked is False
    assert "Link de la clase: https://meet.google.com/xyz" in plan.entry_caption
    assert "Materia del estudio: https://drive.google.com/mat" in plan.entry_caption
    # Access first, material after, whatever the load order was.
    assert plan.entry_caption.index("https://meet.google.com/xyz") < plan.entry_caption.index(
        "https://drive.google.com/mat"
    )
    assert plan.warnings == ()


def test_two_links_of_the_same_kind_both_ship_in_catalog_order() -> None:
    plan = plan_delivery(
        MODALITY_VIRTUAL,
        [
            LinkRef(LINK_MEETING, "https://zoom.us/j/modulo2"),
            LinkRef(LINK_MEETING, "https://zoom.us/j/modulo3"),
        ],
    )

    lines = plan.text.splitlines()
    assert "Link de la clase: https://zoom.us/j/modulo2" in lines
    assert "Link de la clase: https://zoom.us/j/modulo3" in lines
    assert lines.index("Link de la clase: https://zoom.us/j/modulo2") < lines.index(
        "Link de la clase: https://zoom.us/j/modulo3"
    )


def test_virtual_with_only_a_material_link_still_blocks() -> None:
    """An `other` link is complement, not access: it never opens the course, so a
    virtual course with only material has nothing that lets the lead in."""
    plan = plan_delivery(
        MODALITY_VIRTUAL, [LinkRef(LINK_OTHER, "https://drive.google.com/mat", "Material")]
    )

    assert plan.is_blocked is True
    assert plan.blocked_by == card_flags.MISSING_LINK


def test_hybrid_with_only_a_material_link_delivers_it_and_warns() -> None:
    """The entry always ships; the material rides along, and the card still warns that
    nothing loaded opens the virtual modules."""
    plan = plan_delivery(
        MODALITY_HYBRID, [LinkRef(LINK_OTHER, "https://drive.google.com/mat", "Material")]
    )

    assert plan.needs_entry is True
    assert "Material: https://drive.google.com/mat" in plan.entry_caption
    assert plan.warnings == (card_flags.MISSING_LINK,)


def test_other_link_without_label_gets_the_generic_one() -> None:
    plan = plan_delivery(
        MODALITY_HYBRID,
        [LinkRef(LINK_MEETING, "https://zoom.us/j/1"), LinkRef(LINK_OTHER, "https://x.com/m")],
    )

    assert "Link: https://x.com/m" in plan.entry_caption


def test_hybrid_lead_copy_has_no_opening_marks() -> None:
    """Hard rule of the agent persona, checked on the copy this modality introduces."""
    plan = plan_delivery(
        MODALITY_HYBRID,
        [LinkRef(LINK_MAPS, "https://maps.app/x"), LinkRef(LINK_MEETING, "https://zoom.us/j/1")],
    )

    assert "¡" not in plan.entry_caption
    assert "¿" not in plan.entry_caption


# --------------------------- confirmation + key facts (server#290) ---------------------------
#
# The lead asked "¿y ahora?" after paying: every message opens by confirming the payment
# and carries date, time, modality, place and map — what they scroll back for.

_EVENT = EventInfo(
    starts_at=datetime(2026, 9, 10, 23, 30, tzinfo=UTC),  # 19:30 in La Paz
    location="Av. Siempre Viva 742",
)


def test_every_message_opens_by_confirming_the_payment() -> None:
    for text in (ENTRY_CAPTION, HYBRID_ENTRY_CAPTION, VIRTUAL_INTRO, PAYMENT_CONFIRMED_PENDING):
        assert text.startswith(PAYMENT_CONFIRMED)
        assert "¡" not in text and "¿" not in text


def test_presencial_caption_lists_date_time_modality_place_and_map_in_business_time() -> None:
    plan = plan_delivery(
        MODALITY_PRESENCIAL, [LinkRef(LINK_MAPS, "https://maps.app/sede")], event=_EVENT
    )

    lines = plan.entry_caption.splitlines()
    assert lines[0] == ENTRY_CAPTION
    assert lines[1:] == [
        "Fecha: 10/09/2026",
        "Hora: 19:30",
        "Modalidad: presencial",
        "Lugar: Av. Siempre Viva 742",
        "Ubicación: https://maps.app/sede",
    ]
    # The map is printed once: the details block replaced the old location suffix.
    assert plan.entry_caption.count("https://maps.app/sede") == 1


def test_naive_event_time_is_read_as_utc_not_process_time() -> None:
    """SQLite hands back naive datetimes; the hour must still be the lead's clock."""
    plan = plan_delivery(
        MODALITY_PRESENCIAL, [], event=EventInfo(starts_at=datetime(2026, 9, 11, 1, 0))
    )

    assert "Fecha: 10/09/2026" in plan.entry_caption  # 01:00 UTC is still the 10th in La Paz
    assert "Hora: 21:00" in plan.entry_caption


def test_virtual_message_states_the_modality_after_the_confirmation() -> None:
    plan = plan_delivery(MODALITY_VIRTUAL, [LinkRef(LINK_MEETING, "https://zoom.us/j/1")])

    lines = plan.text.splitlines()
    assert lines[0] == VIRTUAL_INTRO
    assert lines[1] == "Modalidad: virtual"
    assert lines[2] == "Link de la clase: https://zoom.us/j/1"
    assert "Fecha:" not in plan.text  # no event for a virtual course


def test_hybrid_caption_puts_the_facts_before_the_zoom_links() -> None:
    plan = plan_delivery(
        MODALITY_HYBRID,
        [LinkRef(LINK_MAPS, "https://maps.app/x"), LinkRef(LINK_MEETING, "https://zoom.us/j/1")],
        event=_EVENT,
    )

    caption = plan.entry_caption
    assert caption.index("Fecha: 10/09/2026") < caption.index(HYBRID_VIRTUAL_INTRO)
    assert "Modalidad: presencial + virtual (Zoom)" in caption
    assert "Lugar: Av. Siempre Viva 742" in caption
    assert caption.count("https://maps.app/x") == 1


def test_event_without_location_prints_no_empty_place_line() -> None:
    plan = plan_delivery(
        MODALITY_PRESENCIAL, [], event=EventInfo(starts_at=_EVENT.starts_at, location=None)
    )

    assert "Lugar:" not in plan.entry_caption
    assert "Fecha: 10/09/2026" in plan.entry_caption
