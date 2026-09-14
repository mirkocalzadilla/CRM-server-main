"""Puerto de visión: leer los datos de una imagen o un PDF.

Deliberadamente **separado** de `LLMPort` y no una generalización suya. `LLMPort` es el
contrato de la conversación: texto, herramientas, un historial que el loop y los
resúmenes comparten. Meter media ahí obligaría a tocar ese camino crítico para un uso
que no lo necesita. Este puerto hace una sola cosa: una llamada, un binario, un dict.

El resultado es **evidencia, no una decisión**: quien extrae no aprueba nada. Los
checks contra ese dict viven en `crm/domain/receipt_checks.py`, en código
determinístico. Es el mismo reparto que en el resto del agente — el modelo propone, el
código valida.
"""

from __future__ import annotations

from typing import Protocol

# Mime types que los proveedores aceptan para este uso. Un comprobante llega como foto
# de WhatsApp (jpeg/png/webp) o como PDF exportado por la app del banco.
SUPPORTED_IMAGE_MIMES: frozenset[str] = frozenset({"image/jpeg", "image/png", "image/webp"})
SUPPORTED_DOCUMENT_MIMES: frozenset[str] = frozenset({"application/pdf"})


def is_supported_mime(mime_type: str) -> bool:
    return mime_type in SUPPORTED_IMAGE_MIMES or mime_type in SUPPORTED_DOCUMENT_MIMES


class VisionPort(Protocol):
    """Una extracción estructurada sobre un binario. Implementado por los adapters."""

    async def extract(
        self,
        *,
        content: bytes,
        mime_type: str,
        instructions: str,
        schema: dict[str, object],
    ) -> dict[str, object]:
        """Devuelve los campos de `schema` leídos del binario.

        `instructions` describe qué buscar; `schema` es el JSON Schema que la respuesta
        debe cumplir (el proveedor lo fuerza, así que no hay que parsear texto libre).
        Un campo que no se puede leer viene ausente o `null` — eso es un resultado
        válido y significativo, no un error.

        Levanta una excepción si el proveedor falla o si el mime no está soportado: el
        caller trata eso como "no se pudo leer" y deriva a un humano.
        """
        ...
