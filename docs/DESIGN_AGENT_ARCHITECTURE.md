# Diseño — Arquitectura técnica del agente (datos + orquestación + runtime)

> **Fecha:** 2026-06-05 · **Estado:** propuesta para implementar (shape acordado con Natalia)
> **Complementa:** [DESIGN_AGENT_SALES_FLOW.md](DESIGN_AGENT_SALES_FLOW.md) (modelo de producto / negocio); la decisión de runtime+tools está registrada en CLAUDE.md (invariantes) / BITACORA. Este doc resuelve **modelo de datos + orquestación + ruteo + costo**.
> **Fuente de datos:** esquema `agents` de noxis en [reference/agents.schema.sql](reference/agents.schema.sql) (copia del KB).

---

## 1. Principios

| Principio | Implicación |
|---|---|
| **Purpose-bound** (política Meta, vigente 15-ene-2026) | El agente está acotado al dominio (venta de cursos / servicios de Mirko). No es chatbot general. Guardrails en código, no solo en prompt. |
| **Óptimo en costo** | El pago mensual del cliente es bajo (y lo será los primeros meses). **IA solo cuando hace falta generar.** La mayoría de los turnos se resuelven sin LLM (§7). |
| **Determinístico primero** | El ruteo de flujo es por reglas (gratis); el LLM-clasificador es solo *fallback*. |
| **Simple, funcional, escalable** | MVP para Mirko (1 tenant, 1 número), pero el modelo de datos y la orquestación soportan multi-rubro sin reescritura (§4, §13). |
| **El LLM propone, el código valida** | Las transiciones del funnel y los guardrails los impone el código; el prompt refuerza. |

## 2. Las tres dimensiones ortogonales (no mezclar)

El error de diseño más común es confundir estas tres. Son independientes y viven en lugares distintos:

| Dimensión | Pregunta que responde | Vida | Dónde vive |
|---|---|---|---|
| **Config por dominio** | ¿Quién es el agente y qué vende? | estática por tenant | esquema `agents`: `product`/`agent_template`/`agent`/`agent_version` (§4) |
| **Intención del turno** (router) | ¿De qué trata ESTE mensaje? | efímera, por turno | router determinístico; se recalcula cada turno (§7) |
| **Etapa del lead** (funnel) | ¿Qué tan avanzado está el lead? | persistente | `funnel_stage` en la conversación + FSM validada (§9) |

> El router mira el **turno**; el funnel mira el **lead**; la config define **quién es el agente**. Ej.: un mensaje de intención "derivación" dispara la transición de funnel `qualified → handed_off`, que el código valida.

## 3. Stack y decisiones lockeadas

| Capa | Decisión | Origen |
|---|---|---|
| Backend | FastAPI async + PostgreSQL (asyncpg + SQLAlchemy 2.0) + Redis + Alembic | stack existente |
| Modelo de datos del agente | **Adoptar el esquema `agents`** (por-dominio, versionado, RAG) — reescribe el módulo plano actual | decisión 2026-06-05 |
| Orquestación | **Hand-coded en FastAPI** (router determinístico + FSM + loop propio) para el MVP, con costuras que dejan barata la migración futura a **LangGraph** al escalar (§5) | decisión 2026-06-05 (confirma benchmark §4) |
| Runtime LLM | **Multi-provider vía adapter propio detrás de `LLMPort`** (NO langchain-anthropic), loop en `AgentService`. Provider seleccionable por config (`LLM_PROVIDER`); **Anthropic es el default**, OpenAI es alterno | benchmark Enfoque A + §5; multi-provider 2026-06-06 |
| Modelos | **Default (Anthropic):** Sonnet 4.6 en el loop (conversación con el lead), Haiku 4.5 para los resúmenes (handoff + intra-etapa), prompt caching. **Alterno (OpenAI):** modelos pineados por config, sin prompt caching (no-op) | benchmark §5; 2026-06-06 · rol↔modelo invertido 2026-07-24 (experiencia del cliente > costo; el resumen es interno) |
| Tools | **`ToolDefinition` dataclass con contrato formal** (NO `dict[str,Callable]`) | benchmark make-or-break |
| Ruteo | Determinístico primero; LLM-clasificador solo fallback | decisión costo 2026-06-05 |

## 4. Modelo de datos — esquema `agents`

Se adopta el esquema limpio multi-dominio de noxis. Aterriza como modelos SQLAlchemy async en el módulo `agent`.

| Tabla | Rol | Notas de adopción |
|---|---|---|
| `product` | Catálogo de **rubros** (slug, `domain`, `domain_db_name`) | Para Mirko: cursos y servicios son catálogo del agente, **no** dos productos (un solo número → un agente; ver §10) |
| `agent_template` | **Plantilla por rubro** (system_prompt, default_tools, default_config) | Semilla reutilizable; el 2º cliente parte de aquí |
| `agent` | **Instancia por organización** (UNIQUE `organization_id, product_slug`; `tools`/`config` jsonb, `current_version_id`) | El agente de Mirko |
| `agent_version` | **Versionado atómico** de prompt+tools+config | Iterar sin perder historial; rollback; clave para evals |
| `agent_instance` | **Canal** (`whatsapp_number` UNIQUE, `handoff_whatsapp`) | Mirko = 1 instancia, 1 número |
| `kb_chunk` | **RAG por instancia** (`embedding vector(1536)`, HNSW cosine) | FAQ / conocimiento de cursos y servicios |
| `handoff_event` | **Handoff de 1ª clase** (reason, context, `resolved_at`) | Auditable; alimenta el inbox |
| `ai_chat_histories` / `app_chat_histories` | Mensajes IA (jsonb, `thread_id`+`message_order`) vs humano (`sender`) | Separación IA/humano = base de inbox + takeover. **Fuente de verdad** de la conversación |
| `blacklist` | Pausa de IA por `organization_id, numero, estado` | Switch IA-on/off / takeover |

**Lo que hay que AGREGAR al esquema** (ni `agents` ni feeling lo tienen):
- `funnel_stage` (enum) + `is_ai_active` (bool) a nivel conversación/thread — la etapa del lead (§9).
- Log de intención del turno — en `ai_chat_histories.message`/metadata (no necesita tabla nueva).

**Reescritura y coordinación:** el módulo `agent` actual de Christian ([models.py](../src/server/modules/agent/domain/models.py): `Agent`/`Conversation`/`Message` planos) se reemplaza por estos modelos. **Toca código de Christian → coordinar el boundary** antes de migrar. El webhook + sender Meta que él ya construyó se conservan; cambia el modelo de datos debajo.

## 5. Orquestación — hand-coded (MVP), LangGraph como destino de escala

**Decisión 2026-06-05:** la orquestación del MVP se escribe **a mano en FastAPI** (más barato, rápido y simple; sin dependencia nueva ni riesgo de token-bloat), confirmando el Enfoque A del benchmark. **LangGraph queda como destino de escala documentado** (§5.4); el diseño en costuras (§6, §8, §9) hace esa migración barata cuando llegue el gatillo.

### 5.1 El flujo (mismo grafo lógico, ejecutado por código propio)

```
                         ┌─────────────┐
   webhook Meta ─▶ ingest│ load_context│  (carga config del agente + ventana 10 turnos + funnel_stage)
                         └──────┬──────┘
                                ▼
                         ┌─────────────┐   reglas/menú/keywords (0 IA)
                         │   router    │───────────────┐
                         └──────┬──────┘                │ confianza baja
                  flujo detectado│                       ▼
        ┌──────────────┬─────────┼─────────┐      ┌──────────────┐
        ▼              ▼         ▼         ▼      │ classify_llm │ (Haiku, fallback raro)
   flow_cursos   flow_servicios flow_faq  derive  └──────┬───────┘
        │              │         │(RAG)    │             │
        └──────────────┴────┬────┘         │   (re-entra al router con flujo)
                            ▼              ▼
                     qualify (FSM)   handoff_to_human
                            │              │ valida qualified→handed_off
                            ▼              ▼  handoff_event + is_ai_active=False
                     persist + respond ◀───┘  (Sonnet genera summary 1 vez)
                            │
                            ▼  Redis Pub/Sub ─▶ inbox /crm
```

- **Pasos determinísticos** (sin IA): `load_context`, `router`, handlers templados (saludo/menú, `derive`).
- **Pasos con IA**: `flow_faq` (Haiku + RAG), `qualify` (Haiku loop), `handoff_summary` (Sonnet, 1 vez).
- **Ramas = código** (`if/match` sobre el `{flujo, confianza}` del router), no LLM. Esto mantiene el costo bajo.

### 5.2 Componentes (hand-coded)

- `src/server/modules/agent/services/agent_service.py` — `AgentService.process_message`: orquesta (carga contexto → router → handler → persiste → responde). Recibe repo + `LLMPort` + `ToolRegistry` por inyección.
- `src/server/modules/agent/services/router.py` — router determinístico → `{flujo, confianza}`; fallback LLM (Haiku) solo si confianza baja.
- `src/server/modules/agent/domain/funnel_fsm.py` — `validate_transition()` pura (§9).
- Loop tool_use propio dentro del handler (~120-160 líneas): mensaje → `LLMPort.complete` → tool_use → ejecutar función → repetir hasta respuesta/handoff, con `max_iterations`.

### 5.3 Estado y persistencia

- `State` = objeto Pydantic en memoria del request: `{tenant_id, conversation_id, agent_id, funnel_stage, flow, messages_window, ...}`. **`tenant_id` jamás entra al contexto del LLM** (vector de prompt-injection); solo para scoping de queries y namespaces Redis.
- **Fuente de verdad de la conversación = `ai_chat_histories`** (Postgres). **Working memory = Redis** (ventana 10 turnos, TTL, namespace `tenant:{id}:*`). Resumibilidad = recargar de Postgres; no hace falta checkpointer externo en el MVP.

### 5.4 Cuándo y cómo migrar a LangGraph

- **Gatillo:** flujos con ramas profundas / subagentes / multi-step no determinista, o necesidad de ejecución durable + human-in-the-loop sofisticado across muchos flujos. Hoy (~5 flujos, router→calificar→derivar) **no se llega**.
- **Por qué la migración es barata:** la lógica de dominio vive en las costuras (`LLMPort`, `ToolRegistry`, `funnel_fsm`), fuera de la orquestación. Migrar = reescribir el **cableado** (el grafo), reusando FSM + tools + adapter intactos.
- **Disciplina al migrar:** LangGraph **solo como orquestador** — los nodos llaman a nuestro `LLMPort`, nunca `langchain-anthropic`/agents por defecto. Eso neutraliza las objeciones del benchmark (+40-60% tokens / opacidad / lock-in). Checkpointer = PostgresSaver sobre el mismo Postgres (una sola fuente de verdad + `interrupt()` para handoff).

## 6. Runtime LLM (adapter propio, multi-provider)

- `src/server/modules/agent/domain/llm_port.py` — `LLMPort(Protocol)`: `async complete(messages, tools, system) -> Turn`. Contrato neutro (`Message`/`ToolUse`/`ToolResult`/`ToolSpec`/`Turn`), agnóstico de provider. Es la costura multi-provider.
- `src/server/modules/agent/services/anthropic_adapter.py` — implementa el puerto; `cache_control: ephemeral` en system+tools; model ID **pineado** (`claude-haiku-4-5-20251001`, `claude-sonnet-4-6`), nunca alias `-latest`. **Default.**
- `src/server/modules/agent/services/openai_adapter.py` — implementa el mismo puerto sobre la Chat Completions API (function calling). Mapea el contrato neutro ↔ OpenAI (`tool_calls`/`role:tool`); sin prompt caching (no-op); model IDs **pineados** por config. **Alterno.**
- `src/server/modules/agent/services/llm_factory.py` — `build_llm(settings, role)` decide el adapter según `LLM_PROVIDER` (un único punto de selección). `role` ∈ {`loop`, `summary`} elige el modelo por rol.
- El `AgentService` y los handlers invocan el `LLMPort` (nunca un SDK directo) → el runtime queda desacoplado de la orquestación, del provider, y de un futuro LangGraph.

## 7. Router determinístico + costo

El "AI classifier que elige el flujo" se implementa **determinístico primero**:

| Capa | Mecanismo | Costo IA |
|---|---|---|
| 0 · reglas | keywords, menú/quick-replies nativos de WhatsApp, comandos ("asesor", "1", "2"), horario | **0** |
| 1 · fallback | si confianza < umbral → 1 call **Haiku** structured-output `{flujo, confianza}` | bajo, ocasional |
| 2 · generación | solo en nodos que requieren redacción libre (FAQ, calificación) | 1 call Haiku (cached) |

**Costo por tipo de turno:**

| Turno | Resolución | Costo IA |
|---|---|---|
| Saludo / menú / selección / "hablar con humano" / fuera de horario | regla + plantilla | **0** |
| FAQ / pregunta libre | Haiku + RAG (`kb_chunk`) | 1 (cached) |
| Diálogo de calificación | Haiku (flujo+tool en la misma llamada) | 1 (cached) |
| Resumen de handoff | Sonnet, 1 vez al derivar | 1 |

Mitigaciones de mis-routing: umbral de confianza, **fallback como ruta primaria** (no excepción), re-clasificar cada turno, y handlers que siguen siendo mini-agentes (toleran error de ruteo).

## 8. Tools (contrato formal)

- `src/server/modules/agent/domain/tools.py` — `ToolDefinition` dataclass (`name`, `description` —incluye *cuándo NO* usarla—, `input_schema`, `handler`). **Protocolo formal = costura MCP-ready.**
- `ToolRegistry` inyectado al servicio.
- Catálogo MVP: `get_oferta` (cursos/servicios desde config), `consultar_faq` (estático desde config; RAG `consultar_kb` sobre `kb_chunk` diferido), `set_lead_stage` (propone transición de funnel, valida con `validate_transition`), `handoff_to_human`, `out_of_scope` (guardrail Meta auditable).
- Salida estructurada: `tool_choice` forzado + validación Pydantic. Nunca parsear texto libre para decidir estado.

## 9. Funnel state machine

```
new ─▶ engaging ─▶ qualifying ─▶ qualified ─▶ handed_off
  └──────────────┴──────────────┴─▶ disqualified
```

- `src/server/modules/agent/domain/funnel_fsm.py` — `FunnelStage` enum + `ALLOWED_TRANSITIONS` + `validate_transition()` **puro, testeable sin LLM**.
- El LLM propone `{transición, razón}` vía `set_lead_stage`/`handoff_to_human`; el código acepta o rechaza.
- **`handed_off` alcanzable desde cualquier etapa activa** (`new`/`engaging`/`qualifying`/`qualified`), no solo `qualified`. Motivo de handoff ∈ `{explicit_request, payment_validation, agent_error}` (FLUJO §1): `payment_validation` llega desde `qualified`; `explicit_request` y `agent_error` pueden ocurrir en cualquier turno.
- **`handed_off` = derivación humana**: estado terminal del agente. Al entrar: escribe `handoff_event`, pone `is_ai_active=False` (silencio), notifica al humano vía Redis Pub/Sub → inbox `/crm`. El agente no responde hasta reactivación manual.

## 10. Flujos del negocio — cursos (1) + servicios (2)

Mirko usa **un solo número → un `agent_instance`**. Cursos y servicios son **dos flujos de venta dentro de un mismo agente** (no dos agentes):

```
saludo → mostrar oferta (cursos | servicios)      [determinístico, 0 IA]
    ├─ flow_cursos    → info/FAQ → qualify → derivación
    └─ flow_servicios → info/FAQ → qualify → derivación
   (flow_faq y derivación son transversales a ambas líneas)
```

- Cada línea tiene su propio contenido en config + sus propios `kb_chunk`.
- `cobro` **no entra** al MVP (§15).

## 11. Multi-dominio / alta de un rubro nuevo

`product` (rubro) + `agent_template` → `agent` (la org lo adopta y ajusta) → `agent_instance` (su número) → `kb_chunk` (su conocimiento). El 2º cliente es **filas en DB, no deploy**.

## 12. Precondiciones bloqueantes (antes de encender el runner)

| # | Precondición | Riesgo si se omite |
|---|---|---|
| 1 | Fix `get_by_external_id` con filtro `tenant_id` ([conversation_repository.py:44](../src/server/modules/agent/repositories/conversation_repository.py#L44)) | Fuga cross-tenant silenciosa al 2º tenant |
| 2 | Dispatch: el webhook hoy **guarda y se detiene** → falta cola + worker | Sin esto el agente no responde |
| 3 | Lock por `conversation_id` (Redis SETNX) | Ráfagas/re-entregas de WhatsApp → historial duplicado, FSM inconsistente |
| 4 | Migración del modelo `agents` + `funnel_stage`/`is_ai_active` | Sin estado persistido no hay funnel |
| 5 | Criterios de calificación cerrados con Mirko (budget/timeline/intent) | Define el schema de `qualify_lead` |

## 13. Fuera de scope / Fase 2

| No entra ahora | Vuelve cuando |
|---|---|
| **Cobro real** | tras validar modelo de negocio (lado Christian) |
| **Seguimientos proactivos** (ventana 24h / plantillas HSM de Meta) | Fase 2, lado Christian, tras validar negocio |
| MCP factory | heterogeneidad de tools entre tenants (3+) |
| Tabla `meta_integrations` (multi-número) | 2º cliente con su propio número |

## 14. Plan por fases

- **Fase 0 — Precondiciones (§12):** fix cross-tenant, dispatch+worker, lock Redis, migración modelo `agents`+funnel, criterios de calificación.
- **Fase 1 — Núcleo:** `funnel_fsm.py` (+tests sin LLM) · `LLMPort`+`AnthropicAdapter` (Haiku+caching) · `ToolDefinition`+tools · `AgentService` hand-coded (router determinístico + flows cursos/servicios/faq + qualify, ventana 10 turnos) · cableado del sender.
- **Fase 2 — Handoff + observabilidad:** `handoff_event`+summary (Sonnet) · Pub/Sub → inbox `/crm` · modo silencioso · tabla de turnos (tokens/latencia) · opt-in LGPD + retención 90d.
- **Fase 3 — Hardening + evals:** `max_iterations` + fallback · ~20 fixtures de conversación · config por tenant desde DB.
- **Fase 4 — Escala:** Langfuse · `agent_version` activo · evaluar **migración a LangGraph** si el grafo de flujos se vuelve complejo (§5.4) · evaluar MCP solo si hay heterogeneidad de tools.

**Boundary con Christian:** él = inbound Meta (webhook+HMAC+sender, ya hecho) + define el modelo de negocio para cobro/seguimientos. Reescritura de modelos del módulo `agent` = coordinada (toca su código).

## 15. Decisiones de negocio abiertas

1. Criterios de calificación (budget/timeline/intent) — definen `qualify_lead`.
2. Catálogo de cursos y servicios (contenido para config + `kb_chunk`).
3. Persona/tono del agente.
4. Canal de notificación de handoff (inbox / email / WhatsApp a Mirko).
5. ¿Ambas líneas (cursos y servicios) están activas hoy, o se prioriza una para el MVP?
