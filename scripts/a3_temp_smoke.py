"""Smoke A3: prueba que `temperature` viaja por el adapter hasta el LLM real.

Una llamada mínima con temperature=0.3 (y otra sin, para el default del
provider). Si el SDK rechazara el parámetro, esto falla con 4xx. Uso:
`python scripts/a3_temp_smoke.py` (lee OPENAI_API_KEY del .env).
"""

from __future__ import annotations

import asyncio

from server.config import get_settings
from server.modules.agent.domain.llm_port import Message, Role
from server.modules.agent.services.openai_adapter import OpenAIAdapter


async def main() -> None:
    settings = get_settings()
    adapter = OpenAIAdapter(
        api_key=settings.openai_api_key,
        model=settings.llm_openai_model_loop,
        max_tokens=30,
    )
    messages = [Message(role=Role.USER, text="Decí 'hola' y nada más.")]
    with_temp = await adapter.complete(
        system="Sos un asistente escueto.", messages=messages, tools=[], temperature=0.3
    )
    print(f"temperature=0.3 -> {with_temp.text!r} (stop: {with_temp.stop_reason})")
    without = await adapter.complete(
        system="Sos un asistente escueto.", messages=messages, tools=[]
    )
    print(f"temperature=None -> {without.text!r} (stop: {without.stop_reason})")


if __name__ == "__main__":
    asyncio.run(main())
