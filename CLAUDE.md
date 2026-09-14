# CLAUDE.md — Backend Conventions

Guidance for Claude Code (and any dev) working on the backend. **Read before touching code.** Source of truth for the design: [`docs/`](docs/).

## Architecture

- **Framework:** FastAPI
- **Database:** PostgreSQL (asyncpg) + SQLAlchemy 2.0 (async) + Alembic. Docker image: **`pgvector/pgvector:pg16`** (the `agents` schema uses pgvector).
- **Structure:** Modular by bounded contexts (`core`, `agent`). **Repository → Service → API Router. Never bypass layers.**

## Paradigm / invariants — NO romper sin actualizar el diseño primero

Reglas de arquitectura del agente. Si un cambio las toca, **primero se actualiza el doc de diseño y se acuerda** (ver "Antes de commitear"). Detalle en [`docs/`](docs/).

- **Modelo de datos = esquema `agents`** ([docs/reference/agents.schema.sql](docs/reference/agents.schema.sql)): `product → agent_template → agent → agent_version → agent_instance` + `kb_chunk` + `handoff_event` + `ai_chat_histories`/`app_chat_histories` + `blacklist`. Multi-dominio, versionado, RAG. Reescribe el módulo `agent` plano actual.
- **Orquestación = hand-coded** (router determinístico + FSM + loop `tool_use` propio). **NO LangGraph** en el MVP (es destino de escala). **NO LangChain.**
- **Runtime LLM = adapter propio detrás de `LLMPort`** (contrato neutro), **NO `langchain-anthropic`**. **Multi-provider seleccionable por config** (`LLM_PROVIDER`): **Anthropic es el default** (Messages API; Sonnet 4.6 en el loop = conversación con el lead, Haiku 4.5 para los resúmenes = handoff + intra-etapa; prompt caching), **OpenAI alterno** (Chat Completions; sin prompt caching). **Model IDs siempre pineados** (nunca alias `-latest`/`-mini` suelto). La selección de provider vive en un único lugar (`llm_factory.build_llm`); el loop y los handlers nunca tocan un SDK directo.
- **Tools = `ToolDefinition` dataclass con contrato formal** (NO `dict[str, Callable]`).
- **Funnel = state machine validada en código** (`validate_transition`): el LLM propone, el código valida.
- **Ruteo determinístico primero; IA solo cuando hace falta generar** (restricción dura de costo).
- **Multi-tenant:** filtrá por `tenant_id` en cada query; **nunca** metas `tenant_id` en el contexto del LLM.
- **Contratos de integración entre piezas:** [docs/SPECS_MVP.md](docs/SPECS_MVP.md). Comportamiento del agente: [docs/FLUJO_AGENTE.md](docs/FLUJO_AGENTE.md).

## Clean Code Rules

- **Clean code siempre.** Ruff (lint/format) + MyPy estricto, sin `Any`. Tipado estricto en todo.
- **Tamaño:** archivos <200 líneas, funciones <50 líneas.
- **Pruebas end-to-end / por módulo antes de dar por terminado un cambio — no romper lo existente.** Hasta que una pieza esté integrada: pruebas locales en consola con la estructura correcta (ver [docs/SPECS_MVP.md](docs/SPECS_MVP.md) §"Regla de pruebas").
- **Idioma:** código en **inglés**; **comentarios mínimos, solo cuando sean necesarios, en inglés**; **documentación (`docs/`, README) en español**.

## Commands

- `docker compose up -d` — Postgres (pgvector) + Redis (+ backend).
- `pip install -e ".[dev]"` — dependencias dev.
- `alembic upgrade head` / `alembic revision --autogenerate -m "msg"` — migraciones.
- `uvicorn server.main:app --reload` — dev server.
- `ruff check .` · `mypy` · `pytest` — lint / tipos / tests.
- **Runbook (levantar/probar) + estado del proyecto:** [docs/ESTADO_Y_RUNBOOK.md](docs/ESTADO_Y_RUNBOOK.md).

## Antes de commitear

Usá **`/close`**: muestra el diff, lo verifica contra `docs/` + estas reglas, registra la entrada directamente en `BITACORA.md` (arriba de todo; los conflictos entre PRs se auto-resuelven vía `merge=union` en `.gitattributes`), corre los tests y arma el commit. **No se commitea contra el diseño** — si el cambio desvía del paradigma, se actualiza el doc primero.

## Project Structure

- `src/server/` — código (main.py, config.py, `shared/`, `modules/<core|agent>/{domain,repositories,services,api}`).
- `tests/` — pytest. `migrations/` — Alembic.
- `docs/` — diseño (DESIGN_*, BENCHMARK, FLUJO_AGENTE, SPECS_MVP, ESTADO_Y_RUNBOOK) + `reference/` (esquema agents) + `archive/` (handoffs históricos).
- `BITACORA.md` — registro de cambios.
