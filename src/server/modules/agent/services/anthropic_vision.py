"""`VisionPort` sobre la Messages API de Anthropic.

Una sola llamada: el binario como bloque `image` o `document`, y la salida forzada por
una herramienta con el schema pedido. Se fuerza la herramienta en vez de pedir "devolvé
JSON" porque el proveedor valida la forma: no hay texto libre que parsear ni un modelo
que decida agregar una explicación antes del objeto.

Model ID pineado, como el resto del runtime (nunca un alias `-latest`).
"""

from __future__ import annotations

import base64
from typing import Any, cast

from anthropic import AsyncAnthropic
from anthropic.types import ToolUseBlock

from server.modules.agent.domain.vision_port import (
    SUPPORTED_DOCUMENT_MIMES,
    SUPPORTED_IMAGE_MIMES,
)

_TOOL_NAME = "registrar_datos"


class AnthropicVisionAdapter:
    """Implementa `VisionPort` sobre el SDK de Anthropic."""

    def __init__(self, *, api_key: str, model: str, max_tokens: int = 1024) -> None:
        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    async def extract(
        self,
        *,
        content: bytes,
        mime_type: str,
        instructions: str,
        schema: dict[str, object],
    ) -> dict[str, object]:
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=[
                cast(
                    Any,
                    {
                        "role": "user",
                        # El binario va ANTES del texto: es lo que el modelo tiene que
                        # mirar, y la instrucción se lee sobre eso.
                        "content": [
                            _media_block(content, mime_type),
                            {"type": "text", "text": instructions},
                        ],
                    },
                )
            ],
            tools=[
                cast(
                    Any,
                    {
                        "name": _TOOL_NAME,
                        "description": "Registra los datos leídos del documento.",
                        "input_schema": schema,
                    },
                )
            ],
            tool_choice=cast(Any, {"type": "tool", "name": _TOOL_NAME}),
        )
        for block in response.content:
            if isinstance(block, ToolUseBlock) and block.name == _TOOL_NAME:
                return dict(block.input) if isinstance(block.input, dict) else {}
        # El proveedor tenía la herramienta forzada, así que esto no debería pasar; si
        # pasa, "no se pudo leer nada" es la lectura correcta (deriva a un humano).
        return {}


def _media_block(content: bytes, mime_type: str) -> dict[str, object]:
    data = base64.standard_b64encode(content).decode("ascii")
    if mime_type in SUPPORTED_IMAGE_MIMES:
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": mime_type, "data": data},
        }
    if mime_type in SUPPORTED_DOCUMENT_MIMES:
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": mime_type, "data": data},
        }
    raise ValueError(f"mime type no soportado para visión: {mime_type}")
