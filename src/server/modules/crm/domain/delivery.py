"""What a lead receives once their payment is validated.

Pure decision, no DB and no sending: given a service's modality, its links and the event
the entry binds to, returns a plan for the orchestrator to execute. Split out so every
combination — including the ones that fall back to a human — can be tested without
booting anything.

The three delivery shapes:

- **presencial** -> the QR entry plus the location, in a single message.
- **virtual** -> one message with every delivery link, labeled (WhatsApp group, meeting,
  and any extra material the operator loaded as `other`).
- **hibrido** -> both: the QR entry for the in-person part and the delivery links for the
  rest, in the same message. Mirko's only QR-paid service is a 4-module course where one
  module is in person and three are over Zoom, so this is the real shape, not an edge case.

Every message opens by confirming the payment and carries the facts the lead scrolls
back for — date, time, modality, place, map — so confirmation and access arrive together
(server#290).

What never ships on its own: no modality means we cannot know what to send, and a virtual
course with no ACCESS link (group or meeting) has nothing that opens the course — an
`other` link is complement, not access, so it never satisfies that gate alone. Those
return a blocked plan carrying the notice code; the orchestrator still confirms the
payment to the lead, and a human picks it up. The deliberate exception is a missing
location: the entry *is* the delivery, so it ships with a notice instead of withholding
something already paid for (hybrid: same for links).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple

from server.modules.crm.domain import card_flags
from server.shared.timezone import to_business_time

MODALITY_PRESENCIAL = "presencial"
MODALITY_VIRTUAL = "virtual"
MODALITY_HYBRID = "hibrido"

LINK_WHATSAPP_GROUP = "whatsapp_group"
LINK_MEETING = "meeting"
LINK_MAPS = "maps"
LINK_OTHER = "other"

# Lead-facing copy, in Spanish because the lead reads it. No opening marks (`¡`/`¿`):
# hard rule of the agent persona, with a deterministic backstop in `to_whatsapp_text`.
PAYMENT_CONFIRMED = "Tu pago está confirmado ✅"
ENTRY_CAPTION = (
    f"{PAYMENT_CONFIRMED} Acá va tu entrada 🙌 Te esperamos, no te olvides de mostrar "
    "este QR el día del evento"
)
VIRTUAL_INTRO = f"{PAYMENT_CONFIRMED} Ya estás dentro 🙌"
# Hybrid says "the in-person module" instead of "the event": the course has four dates and
# the QR only opens one of them, so "el día del evento" would be ambiguous.
HYBRID_ENTRY_CAPTION = (
    f"{PAYMENT_CONFIRMED} Acá va tu entrada 🙌 Mostrá este QR el día del módulo presencial"
)
HYBRID_VIRTUAL_INTRO = "Y para los módulos por Zoom"
# When the plan is blocked: the payment is validated all the same, and the lead must not
# be left guessing while a human completes the delivery.
PAYMENT_CONFIRMED_PENDING = (
    f"{PAYMENT_CONFIRMED} En un momento te mando los detalles y el acceso 🙌"
)
_MODALITY_LABELS: dict[str, str] = {
    MODALITY_PRESENCIAL: "presencial",
    MODALITY_VIRTUAL: "virtual",
    MODALITY_HYBRID: "presencial + virtual (Zoom)",
}
_LINK_LABELS: dict[str, str] = {
    LINK_WHATSAPP_GROUP: "Grupo de WhatsApp",
    LINK_MEETING: "Link de la clase",
    LINK_MAPS: "Ubicación",
    LINK_OTHER: "Link",
}
# Access kinds open the course; `other` links (study material, extras) ride along after
# them but never satisfy the access gates on their own (UAT 2026-08-31: a hybrid course
# with a meeting link plus a material link must deliver both).
_ACCESS_KINDS: tuple[str, ...] = (LINK_WHATSAPP_GROUP, LINK_MEETING)
_DELIVERY_KIND_ORDER: tuple[str, ...] = (*_ACCESS_KINDS, LINK_OTHER)


class LinkRef(NamedTuple):
    """A service delivery link, already resolved from the catalog."""

    kind: str
    url: str
    label: str | None = None


class EventInfo(NamedTuple):
    """When and where the entry gives access to, as the lead should read it."""

    starts_at: datetime
    location: str | None = None


@dataclass(frozen=True, slots=True)
class DeliveryPlan:
    """What to do with this card. `blocked_by` != None means nothing is sent."""

    needs_entry: bool = False
    text: str = ""
    entry_caption: str = ""
    blocked_by: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def is_blocked(self) -> bool:
        return self.blocked_by is not None


def plan_delivery(
    modality: str | None, links: list[LinkRef], *, event: EventInfo | None = None
) -> DeliveryPlan:
    """Delivery plan for a service, or a blocked plan carrying its notice."""
    if modality == MODALITY_PRESENCIAL:
        return _plan_presencial(links, event)
    if modality == MODALITY_VIRTUAL:
        return _plan_virtual(links, event)
    if modality == MODALITY_HYBRID:
        return _plan_hybrid(links, event)
    # No modality (or an unknown one, if someone wrote the DB by hand): do not guess.
    # Sending an entry to whoever bought something else is worse than sending nothing.
    return DeliveryPlan(blocked_by=card_flags.NO_MODALITY)


def _plan_presencial(links: list[LinkRef], event: EventInfo | None) -> DeliveryPlan:
    maps = _first(links, LINK_MAPS)
    caption = _with_details(ENTRY_CAPTION, MODALITY_PRESENCIAL, event, maps)
    # The entry is the delivery; the location is a complement. Nothing already paid for
    # is withheld over an administrative gap — it ships, with a notice.
    warnings: tuple[str, ...] = () if maps is not None else (card_flags.MISSING_LINK,)
    return DeliveryPlan(needs_entry=True, entry_caption=caption, warnings=warnings)


def _plan_virtual(links: list[LinkRef], event: EventInfo | None) -> DeliveryPlan:
    if not _has_access_link(links):
        # Nothing that opens the course: a message with only material (or nothing) says
        # "ya estás dentro" to someone who cannot get in. The access link is the delivery.
        return DeliveryPlan(blocked_by=card_flags.MISSING_LINK)
    intro = _with_details(VIRTUAL_INTRO, MODALITY_VIRTUAL, event, None)
    return DeliveryPlan(text="\n".join([intro, *_delivery_lines(links)]))


def _plan_hybrid(links: list[LinkRef], event: EventInfo | None) -> DeliveryPlan:
    """In person and virtual at once: the QR entry and the delivery links, together.

    Never blocked, because there is always the entry to send. A missing piece becomes a
    notice on the card, and the operator fills it in — same rule as a presencial course
    with no location. Material (`other`) links ride along with the entry, but a course
    with only material still warns: nothing loaded opens the virtual modules.
    """
    maps = _first(links, LINK_MAPS)
    caption = _with_details(HYBRID_ENTRY_CAPTION, MODALITY_HYBRID, event, maps)
    warnings: list[str] = [] if maps is not None else [card_flags.MISSING_LINK]
    lines = _delivery_lines(links)
    if lines:
        # One caption and not two messages: the lead scrolls back to a single message
        # on the day of each module. Criterion of #92, same as presencial.
        caption = "\n".join([caption, "", f"{HYBRID_VIRTUAL_INTRO}:", *lines])
    if not _has_access_link(links):
        warnings.append(card_flags.MISSING_LINK)
    return DeliveryPlan(
        needs_entry=True, entry_caption=caption, warnings=tuple(dict.fromkeys(warnings))
    )


def _with_details(intro: str, modality: str, event: EventInfo | None, maps: LinkRef | None) -> str:
    """The intro followed by the facts the lead scrolls back for, one per line."""
    lines = [intro]
    if event is not None:
        local = to_business_time(event.starts_at)
        lines.append(f"Fecha: {local:%d/%m/%Y}")
        lines.append(f"Hora: {local:%H:%M}")
    lines.append(f"Modalidad: {_MODALITY_LABELS[modality]}")
    if event is not None and event.location:
        lines.append(f"Lugar: {event.location}")
    if maps is not None:
        lines.append(f"Ubicación: {maps.url}")
    return "\n".join(lines)


def _delivery_lines(links: list[LinkRef]) -> list[str]:
    """Every deliverable link, labeled: grouped by kind in delivery order, and within a
    kind in the operator's catalog order (`ServiceLink.orden`, preserved upstream)."""
    ordered = [link for kind in _DELIVERY_KIND_ORDER for link in _of_kind(links, kind)]
    return [f"{_label_for(link)}: {link.url}" for link in ordered]


def _has_access_link(links: list[LinkRef]) -> bool:
    return any(_of_kind(links, kind) for kind in _ACCESS_KINDS)


def _of_kind(links: list[LinkRef], kind: str) -> list[LinkRef]:
    return [link for link in links if link.kind == kind and link.url.strip()]


def _first(links: list[LinkRef], kind: str) -> LinkRef | None:
    for link in links:
        if link.kind == kind and link.url.strip():
            return link
    return None


def _label_for(link: LinkRef) -> str:
    """The link's label: the one the operator typed, or its type's generic one."""
    label = (link.label or "").strip()
    return label or _LINK_LABELS.get(link.kind, "Link")
