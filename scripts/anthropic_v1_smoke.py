"""Smoke SDK anthropic 1.x (server#288): 3 checks contra el API real.

1. `temperature` del agente viaja vía `extra_body` en un modelo que la acepta
   (Sonnet 4.6) — si el API la rechazara, esto falla.
2. Sin temperature → default del provider (extra_body ausente).
3. API key inválida → el adapter traduce a `LLMError(category='auth')` (valida la
   capa de traducción de excepciones de punta a punta).

Uso: `python scripts/anthropic_v1_smoke.py` (lee ANTHROPIC_API_KEY del .env).
"""

from __future__ import annotations

import asyncio

from server.config import get_settings
from server.modules.agent.domain.llm_port import LLMError, Message, Role
from server.modules.agent.services.anthropic_adapter import AnthropicAdapter


async def main() -> None:
    settings = get_settings()
    messages = [Message(role=Role.USER, text="Decí 'hola' y nada más.")]

    if settings.anthropic_api_key:
        adapter = AnthropicAdapter(
            api_key=settings.anthropic_api_key,
            model=settings.llm_model_sonnet,
            max_tokens=30,
        )
        with_temp = await adapter.complete(
            system="Sos un asistente escueto.", messages=messages, tools=[], temperature=0.3
        )
        print(
            f"1. temperature=0.3 (extra_body) -> {with_temp.text!r} (stop: {with_temp.stop_reason})"
        )

        without = await adapter.complete(
            system="Sos un asistente escueto.", messages=messages, tools=[]
        )
        print(f"2. temperature=None -> {without.text!r} (stop: {without.stop_reason})")
    else:
        print("1-2. SKIP: sin ANTHROPIC_API_KEY en el entorno (correr donde esté configurada)")

    broken = AnthropicAdapter(
        api_key="sk-ant-invalid-smoke", model=settings.llm_model_sonnet, max_tokens=30
    )
    try:
        await broken.complete(system="s", messages=messages, tools=[])
    except LLMError as exc:
        assert exc.category == LLMError.AUTH, f"esperaba 'auth', vino {exc.category!r}"
        assert exc.provider == "anthropic"
        print(f"3. api key inválida -> LLMError(category={exc.category!r}) OK")
    else:
        raise AssertionError("una key inválida tiene que levantar LLMError(auth)")


if __name__ == "__main__":
    asyncio.run(main())
