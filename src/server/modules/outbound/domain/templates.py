"""Registry of the Meta message templates the system sends.

Mirrors what was submitted to Meta (WhatsApp Manager). The body is kept locally to
render the text mirrored into the CRM thread; Meta renders its own copy on send.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PLACEHOLDER = re.compile(r"\{\{(\d+)\}\}")


@dataclass(frozen=True)
class TemplateSpec:
    name: str
    category: str  # UTILITY | MARKETING
    body: str
    header_image: bool = False
    language: str = "es"

    @property
    def variable_count(self) -> int:
        return len(set(_PLACEHOLDER.findall(self.body)))

    def render(self, variables: list[str]) -> str:
        return _PLACEHOLDER.sub(lambda m: variables[int(m.group(1)) - 1], self.body)


ENTRY_QR_READY = TemplateSpec(
    name="entry_qr_ready",
    category="UTILITY",
    header_image=True,
    body=(
        "Hola {{1}}! Acá tenés tu entrada para {{2}} del {{3}}. "
        "Mostrá este código QR en la puerta para ingresar. Nos vemos ahí!"
    ),
)

RECORDATORIO_EVENTO = TemplateSpec(
    name="recordatorio_evento",
    category="UTILITY",
    body=(
        "Hola {{1}}! Te recuerdo que {{2}} es el {{3}} a las {{4}} en {{5}}. "
        "Si tenés alguna duda, escribime por acá y te ayudo."
    ),
)

REACTIVACION_LEADS = TemplateSpec(
    name="reactivacion_leads",
    category="MARKETING",
    body=(
        "Hola {{1}}! Soy el asistente de Mirko. Hace un tiempo consultaste por {{2}} y "
        "quería contarte que {{3}}. Si te interesa, respondé este mensaje y te paso los "
        "detalles. Si preferís no recibir más mensajes, escribí BAJA."
    ),
)

TEMPLATES: dict[str, TemplateSpec] = {
    t.name: t for t in (ENTRY_QR_READY, RECORDATORIO_EVENTO, REACTIVACION_LEADS)
}


def build_components(
    spec: TemplateSpec, variables: list[str], header_image_url: str | None = None
) -> list[dict[str, object]]:
    """Meta `components` payload for a send. Variables map 1:1 to `{{n}}` order."""
    if len(variables) != spec.variable_count:
        raise ValueError(
            f"template {spec.name} expects {spec.variable_count} variables, got {len(variables)}"
        )
    components: list[dict[str, object]] = []
    if spec.header_image:
        if not header_image_url:
            raise ValueError(f"template {spec.name} requires a header image url")
        components.append(
            {
                "type": "header",
                "parameters": [{"type": "image", "image": {"link": header_image_url}}],
            }
        )
    if variables:
        components.append(
            {
                "type": "body",
                "parameters": [{"type": "text", "text": v} for v in variables],
            }
        )
    return components
