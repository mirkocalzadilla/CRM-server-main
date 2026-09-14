"""`VisionPort` sobre Chat Completions de OpenAI (runtime alterno).

Misma idea que el adapter de Anthropic, con las dos diferencias del proveedor: la
imagen viaja como `image_url` con un data URI, y un PDF no entra por ese camino — va
como `file` con su base64. La salida se fuerza con una función, por el mismo motivo que
allá: que el proveedor garantice la forma en vez de parsear texto libre.
"""

from __future__ import annotations

import base64
import json
from typing import Any, cast

from openai import AsyncOpenAI

from server.modules.agent.domain.vision_port import (
    SUPPORTED_DOCUMENT_MIMES,
    SUPPORTED_IMAGE_MIMES,
)

_TOOL_NAME = "registrar_datos"


class OpenAIVisionAdapter:
    """Implementa `VisionPort` sobre el SDK de OpenAI."""

    def __init__(self, *, api_key: str, model: str, max_tokens: int = 1024) -> None:
        self._client = AsyncOpenAI(api_key=api_key)
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
        response = await self._client.chat.completions.create(
            model=self._model,
            max_completion_tokens=self._max_tokens,
            messages=[
                cast(
                    Any,
                    {
                        "role": "user",
                        "content": [
                            _media_part(content, mime_type),
                            {"type": "text", "text": instructions},
                        ],
                    },
                )
            ],
            tools=[
                cast(
                    Any,
                    {
                        "type": "function",
                        "function": {
                            "name": _TOOL_NAME,
                            "description": "Registra los datos leídos del documento.",
                            "parameters": schema,
                        },
                    },
                )
            ],
            tool_choice=cast(Any, {"type": "function", "function": {"name": _TOOL_NAME}}),
        )
        for call in response.choices[0].message.tool_calls or []:
            arguments = getattr(getattr(call, "function", None), "arguments", None)
            if arguments:
                return _parse(arguments)
        return {}


def _media_part(content: bytes, mime_type: str) -> dict[str, object]:
    data = base64.standard_b64encode(content).decode("ascii")
    if mime_type in SUPPORTED_IMAGE_MIMES:
        return {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{data}"}}
    if mime_type in SUPPORTED_DOCUMENT_MIMES:
        return {
            "type": "file",
            "file": {"filename": "comprobante.pdf", "file_data": f"data:{mime_type};base64,{data}"},
        }
    raise ValueError(f"mime type no soportado para visión: {mime_type}")


def _parse(raw: str) -> dict[str, object]:
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return {str(key): value for key, value in parsed.items()} if isinstance(parsed, dict) else {}
