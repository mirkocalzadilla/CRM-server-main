# Specs de trabajo MVP — piezas para armar como rompecabezas

> **Idea:** cada pieza se desarrolla por separado **contra su contrato**, se prueba **aislada en consola** con la estructura correcta, y al final se **integran** y se hace un smoke real.
> Owners: **C** = Christian · **N** = Natalia. Comportamiento: [FLUJO_AGENTE.md](FLUJO_AGENTE.md). Técnico: [DESIGN_AGENT_ARCHITECTURE.md](DESIGN_AGENT_ARCHITECTURE.md).

## Contratos compartidos (los "bordes" que hacen encajar las piezas)

Estos son los acuerdos que NO se cambian sin actualizar este doc. Permiten trabajar en paralelo contra stubs.

| Contrato | Forma | Dueño | Lo consume |
|---|---|---|---|
| **Cola de dispatch** | Redis list `agent:dispatch`; payload `{conversation_id, tenant_id, kind}`; lock `agent:lock:{conversation_id}` (SETNX + TTL) | C (encola en webhook) | N (worker) |
| **Cola de visión** | Redis list `agent:vision`; payload `{conversation_id, tenant_id, wamid, kind}`; lock `agent:vision:lock:{wamid}` (TTL 180s). **Cola aparte** porque el consumidor es único y secuencial: una llamada de visión en la cola de turnos congelaría las respuestas de todos los leads mientras dura | webhook (media) | worker (loop propio, en paralelo) |
| **Modelos + repos** (esquema `agents`) | SQLAlchemy async tenant-scoped; `ConversationRepo.get_by_external_id(wa_id, tenant_id)` (con filtro tenant) | C | N (AgentService) |
| **`WhatsAppSender`** | `async send_text(to, body)` (ya existe) | C | N (AgentService) |
| **`LLMPort`** | `async complete(system, messages, tools) -> Turn{text, tool_uses[], stop_reason}` | N | N |
| **`ToolDefinition` + `ToolRegistry`** | dataclass `{name, description, input_schema, handler: async(ctx, input)->dict}` | N | N |
| **`funnel_fsm`** | `FunnelStage` enum + `ALLOWED_TRANSITIONS` + `validate_transition(cur, target)` | N | N, tools |
| **`AgentService.process_message(conversation_id, tenant_id)`** | orquesta: carga → router → handler (loop) → persiste → envía | N | worker (M1) |
| **Config ABM API** | `GET/PUT /api/v1/agents/{id}/config` (system_prompt + config jsonb); admin-only | N | front |

## Paquetes de trabajo

| Pkg | Owner | Objetivo | Prueba local (consola, aislada) |
|---|---|---|---|
| **M0 · Datos** | **C** | Imagen `pgvector/pgvector:pg16`; migración del esquema `agents` + `funnel_stage`/`is_ai_active`; fix cross-tenant `get_by_external_id`; seed (product curso, template, agent, instance con número Mirko) | `docker compose up -d` → `alembic upgrade head` → script seed → `psql \dt` + select de las filas seed |
| **M1 · Dispatch** | **C+N** | Webhook encola `conversation_id` (C); worker async consume + lock SETNX (N); handler stub que loguea | `curl` webhook falso → ver Message en DB → worker stub loguea el `conversation_id` (sin LLM). Detalle: §"M1 en detalle" |
| **M2 · Funnel FSM** | **N** | `funnel_fsm.py` (estados + transiciones + validación) | `pytest tests/test_funnel_fsm.py` (puro, sin DB ni LLM) |
| **M3 · Runtime + tools** | **N** | `LLMPort`+`AnthropicAdapter` (Haiku, caching); `ToolDefinition`+`ToolRegistry`+tools (`get_oferta`, `consultar_faq`, `set_lead_stage`, `handoff_to_human`, `out_of_scope`) | script de consola: registry + cada tool con input de ejemplo; **1** llamada Haiku real (barata) imprime la salida |
| **M4 · Router + AgentService** | **N** | `router.py` determinístico (+fallback Haiku); `AgentService.process_message` | script de consola: mensajes simulados → AgentService con **sender STUB que imprime** → verificar flujo + transición de funnel (sin WhatsApp real) |
| **M-Config · ABM + roles** | **N** | RBAC 3 niveles (§"RBAC"); editar prompt/emojis/temperatura = **platform-operator only** (no el cliente); gestión de usuarios/roles = **operador o client_admin** (`require_user_manager`, tenant-scoped, #200); `agent_version` por guardado | `pytest` de endpoints + chequeo de los 3 roles; manual en `/docs` |
| **M5 · Handoff + inbox** | **N** | `handoff_event`+summary (Sonnet) + `is_ai_active=false` + Redis Pub/Sub; front `/crm` (lista, hilo, toggle takeover, badge) | consola: disparar handoff → ver `handoff_event` + `is_ai_active=false` + mensaje pub/sub; front manual |
| **M-CRM-data · Tablero (datos)** | **C** | tablas `pipeline/stage/stage_status/card/traceability` (+contact mínimo) basadas en Firefly, multi-tenant; seed de los 2 pipelines (Gestión Venta + Gestión Postventa) y sus stages. Detalle: §"M-CRM" | migración + seed → `psql`: 2 pipelines, sus stages ordenadas, 0 cards |
| **M-CRM-api · Tablero (API + wiring)** | **N** | handoff crea/mueve card; API tablero (boards, mover card→traceability, hilo **espejo** de WA, toggle `is_ai_active`, `/generarEntrada`); realtime Redis→WS/SSE; front: reemplazar mock + reusar Firefly-App. Detalle: §"M-CRM" | consola/stub: mover card escribe traceability; hilo espejo arma ai+app+inbound en orden; front manual contra mocks |
| **M-Meta-inv · Investigación CRM↔Meta** | **C** | investigar gaps Meta antes de finalizar reply-humano y QRs: media in/out, ventana 24h, statuses, HSM. Detalle: §"M-Meta" | entregable = doc de findings (no código) |

## Orden de integración (el rompecabezas)

```
C:  M0 ───────────▶ M1(encolar) ──────────────────┐
N:  M2 ∥ M3 ∥ M4(sender stub) ∥ M-Config ──────────┤
                                                    ▼
        INTEGRACIÓN: cablear worker → AgentService → WhatsAppSender real
                                                    ▼
                          SMOKE e2e con número de WhatsApp de prueba
                                                    ▼
                              M5 (handoff + inbox) sobre lo integrado
```

- **M2** (FSM) no depende de nada → arranca ya, en paralelo a **M0** de Chris.
- **M4** se desarrolla con un **sender stub** (imprime en consola) hasta la integración; ahí se cambia por el `WhatsAppSender` real de Chris.
- Boundary a respetar: el **contrato de la cola** (M1) y los **modelos/repos** (M0).
- **Track CRM (paralelo):** **M-CRM-data** (C) puede arrancar apenas M0 defina `conversation`; **M-CRM-api + front** (N) van sobre M5. **M-Meta-inv** (C) es investigación que **debe cerrar antes** de finalizar el reply-humano del CRM y el envío de QRs (media in/out + ventana 24h).
- **Para el e2e que ve "el card avanzar":** hace falta M0 + M-CRM-data + M-CRM-api + handoff (M5). El reply-humano real por WhatsApp y los QRs dependen de **M-Meta-inv**.

## Regla de pruebas (hasta integrar)

Mientras una pieza no esté integrada: **pruebas locales en consola con la estructura correcta** (pytest puro, scripts de consola, stubs para sender/LLM/cola). Nada de probar contra WhatsApp real ni contra el flujo completo hasta el paso de integración. Cada pieza se valida contra su contrato.

---

## M0 en detalle — guía para Chris

> Todo esto es una migración Alembic sobre el schema existente. **No correr el SQL del dump directamente.** El dump en [`reference/agents.schema.sql`](reference/agents.schema.sql) es la referencia de qué tablas crear, no un script para correr.

### Qué eliminar (tablas planas viejas)

En la migración, **drop** de las tablas del módulo plano actual (no hay datos de producción):

```sql
DROP TABLE IF EXISTS messages    CASCADE;
DROP TABLE IF EXISTS conversations CASCADE;
DROP TABLE IF EXISTS agents      CASCADE;  -- tabla plana vieja, no la nueva del esquema agents
DROP TYPE  IF EXISTS conversation_status;
DROP TYPE  IF EXISTS message_role;
```

### Qué crear — esquema `agents` (del dump de referencia)

Crear en orden (respetar FKs). Ver DDL completo en `reference/agents.schema.sql`:

```
product → agent_template → agent → agent_version → agent_instance
                                                       │
                                                   kb_chunk
ai_chat_histories  (mensajes LLM, thread_id = conversation.id::text)
app_chat_histories (mensajes humano CRM)
handoff_event
blacklist
```

Extensiones requeridas (añadir al inicio de la migración si no existen):
```sql
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS vector;
```

### Qué agregar — tabla `conversation` (nueva, no está en el dump)

Esta tabla es la que mantiene el **estado del lead** (funnel + control IA). Es nuestra, no viene de noxis.

```sql
CREATE TYPE public.funnel_stage AS ENUM (
    'new', 'engaging', 'qualifying', 'qualified', 'handed_off', 'disqualified'
);

CREATE TABLE public.conversation (
    id              uuid        NOT NULL DEFAULT gen_random_uuid(),
    instance_id     uuid        NOT NULL,          -- → agent_instance.id
    organization_id uuid        NOT NULL,          -- = tenant.id del módulo core
    external_id     text        NOT NULL,          -- número WhatsApp del lead (wa_id)
    funnel_stage    funnel_stage NOT NULL DEFAULT 'new',
    is_ai_active    boolean     NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    UNIQUE (organization_id, external_id),
    FOREIGN KEY (instance_id) REFERENCES public.agent_instance(id) ON DELETE CASCADE
);

CREATE INDEX conv_org_external ON public.conversation (organization_id, external_id);
CREATE INDEX conv_instance     ON public.conversation (instance_id);
```

> **Relación con mensajes:** `ai_chat_histories.thread_id = conversation.id::text`. El historial LLM se agrupa por conversación via `thread_id`.

### SQLAlchemy (sketch del modelo)

```python
class FunnelStage(str, enum.Enum):
    NEW = "new"
    ENGAGING = "engaging"
    QUALIFYING = "qualifying"
    QUALIFIED = "qualified"
    HANDED_OFF = "handed_off"
    DISQUALIFIED = "disqualified"

class Conversation(Base):
    __tablename__ = "conversation"
    __table_args__ = (UniqueConstraint("organization_id", "external_id"),)

    id:              Mapped[uuid.UUID]    = mapped_column(primary_key=True, default=uuid.uuid4)
    instance_id:     Mapped[uuid.UUID]    = mapped_column(ForeignKey("agent_instance.id", ondelete="CASCADE"), index=True)
    organization_id: Mapped[uuid.UUID]    = mapped_column(index=True)
    external_id:     Mapped[str]          = mapped_column(String(64))
    funnel_stage:    Mapped[FunnelStage]  = mapped_column(default=FunnelStage.NEW)
    is_ai_active:    Mapped[bool]         = mapped_column(default=True)
    created_at:      Mapped[datetime]     = mapped_column(default=datetime.utcnow)
    updated_at:      Mapped[datetime]     = mapped_column(default=datetime.utcnow, onupdate=datetime.utcnow)
```

### Fix: `get_by_external_id` con tenant

En `conversation_repository.py`, reemplazar el método actual (sin filtro tenant) por:

```python
async def get_by_external_id(
    self, external_id: str, organization_id: uuid.UUID
) -> Conversation | None:
    result = await self.session.execute(
        select(Conversation).where(
            Conversation.external_id == external_id,
            Conversation.organization_id == organization_id,
        )
    )
    return result.scalar_one_or_none()
```

> **¿Por qué importa?** Sin el filtro `organization_id`, si hay 2 tenants con el mismo número de WhatsApp (posible al escalar), un lead ve la conversación de otro. Es una fuga cross-tenant silenciosa.

### `scripts/seed_mirko.py` — spec de build

> Corre **1 vez** después de `alembic upgrade head`. **Idempotente:** re-correrlo no debe duplicar ni explotar (varios campos son UNIQUE). El sketch viejo de esta sección estaba incompleto contra el DDL — esta es la guía vigente. DDL: [`reference/agents.schema.sql`](reference/agents.schema.sql).

**Entradas a resolver (no hardcodear UUIDs a ciegas):**

| Dato | De dónde sale |
|---|---|
| `organization_id` | **Lookup** del tenant `mirko` en el módulo `core` (tabla `tenants`, por `slug='mirko'`). Si no existe, abortar con mensaje claro — el seed de `core` corre primero. |
| `whatsapp_number` | Número real de WhatsApp de Mirko (placeholder hoy → decisión de negocio abierta, FLUJO §6). UNIQUE en `agent_instance`. |
| `system_prompt`, nombre del curso, precio | Placeholders hoy (FLUJO §6). El seed pone texto provisional; se afina luego vía M-Config (ABM), no re-seedeando. |

**Filas a crear y orden (respetar FKs + el ciclo agent↔version):**

```
1. product            slug='cursos-mirko'            (PK = slug, no uuid)
2. agent_template     slug='ventas-cursos'           (product_slug → product; system_prompt y default_model son NOT NULL)
3. agent              (organization_id, product_slug='cursos-mirko', template_id, system_prompt, model)
                      UNIQUE(organization_id, product_slug); current_version_id = NULL por ahora
4. agent_version      (agent_id, version_number=1, system_prompt, model, tools, config)
                      UNIQUE(agent_id, version_number)
5. UPDATE agent       SET current_version_id = <agent_version.id>   ← cierra el ciclo (FK fk_agent_current_version)
6. agent_instance     (agent_id, display_name='WhatsApp Mirko', whatsapp_number=<real>)  UNIQUE(whatsapp_number)
```

> **El ciclo:** `agent.current_version_id → agent_version.id` y `agent_version.agent_id → agent.id`. Por eso se inserta `agent` con `current_version_id` nulo, luego la versión, y recién después el `UPDATE`. No se puede en un solo `add_all`.

**Idempotencia (cómo:** chequear-antes-de-insertar por la clave natural de cada fila):

| Tabla | Clave natural para el lookup |
|---|---|
| `product` | `slug` |
| `agent_template` | `slug` |
| `agent` | `(organization_id, product_slug)` |
| `agent_version` | `(agent_id, version_number)` |
| `agent_instance` | `whatsapp_number` |

Si la fila existe → reusarla (no re-insertar). El script imprime qué creó vs qué ya estaba.

**Fuera del alcance del seed M0:**
- `kb_chunk` — exige `embedding vector(1536) NOT NULL`; no hay pipeline de embeddings ni contenido de KB todavía (FAQ es decisión abierta, FLUJO §6). Se seedeará cuando exista el contenido + el pipeline de embedding.
- `conversation` — no se seedea; los leads entran por el webhook. Verificar que quede **vacía**.

**Esqueleto (orientativo, no copiar literal):**

```python
# scripts/seed_mirko.py — run once after `alembic upgrade head`; idempotent.
WA_NUMBER = "+591XXXXXXXXX"          # real number — pending business decision
HAIKU = "claude-haiku-4-5-20251001"

async def seed(session: AsyncSession) -> None:
    org_id = await _lookup_tenant_id(session, slug="mirko")   # abort if missing
    product  = await _get_or_create_product(session, slug="cursos-mirko", ...)
    template = await _get_or_create_template(session, slug="ventas-cursos",
                                             product_slug=product.slug,
                                             system_prompt="<placeholder>", default_model=HAIKU)
    agent    = await _get_or_create_agent(session, organization_id=org_id,
                                          product_slug=product.slug, template_id=template.id,
                                          system_prompt="<placeholder>", model=HAIKU)
    version  = await _get_or_create_version(session, agent_id=agent.id, version_number=1,
                                            system_prompt=agent.system_prompt, model=agent.model,
                                            tools=agent.tools, config=agent.config)
    agent.current_version_id = version.id                     # close the cycle
    await _get_or_create_instance(session, agent_id=agent.id,
                                  display_name="WhatsApp Mirko", whatsapp_number=WA_NUMBER)
    await session.commit()
```

### Prueba local de M0

```powershell
docker compose up -d
alembic upgrade head         # debe terminar sin errores
python scripts/seed_mirko.py
# luego en psql:
# \dt   → ver todas las tablas nuevas
# SELECT * FROM product;
# SELECT * FROM agent;
# SELECT * FROM agent_instance;
# SELECT id, funnel_stage, is_ai_active FROM conversation LIMIT 5;  -- vacío OK
```

---

## M1 en detalle — guía para Chris (+ worker de Natalia)

> **Depende de M0.** El encolar se agrega en el **webhook ya reescrito por M0** (esquema `agents` + tabla `conversation`). No empezar M1 contra el webhook plano actual.

### Estado hoy (el punto exacto donde engancha M1)

El webhook **entra, persiste el mensaje y se detiene**: en [`services/webhook_service.py`](../src/server/modules/agent/services/webhook_service.py), después de `msg_repo.add(...)` solo hace `logger.info("whatsapp.message_stored", ...)`. **Ahí** — tras persistir el `Message` y tener el `conversation` resuelto — va el encolar. No dispara nada al agente todavía.

Redis ya está listo: dependencia `redis>=5.2.0`, servicio `redis:7-alpine` en `docker-compose.yml`, setting `redis_url` en `config.py` (en Docker resuelve a `redis://redis:6379/0` vía `env_file`).

### El contrato de la cola (lo que NO se cambia sin actualizar este doc)

| Clave | Valor |
|---|---|
| Lista de dispatch | Redis list `agent:dispatch` |
| Payload | JSON `{"conversation_id": "<uuid>", "tenant_id": "<uuid>"}` — `tenant_id` = valor de `conversation.organization_id` |
| Lock por conversación | `agent:lock:{conversation_id}`, vía `SET key 1 NX EX <ttl>` (SETNX + TTL) |
| Producer | **C** — `LPUSH agent:dispatch <payload>` en el webhook |
| Consumer | **N** — worker con `BRPOP agent:dispatch` (FIFO) |
| Heartbeat del worker | `worker:heartbeat`, `SET key 1 EX 60` en cada iteración del loop; `/health/deep` chequea que exista (#246) |

> **Por qué lock por conversación:** si el lead manda 3 mensajes seguidos, no queremos 3 procesamientos en paralelo de la misma conversación (responses pisadas, doble handoff). El lock serializa por `conversation_id`; mensajes de conversaciones distintas siguen en paralelo.

### Parte de C — encolar en el webhook

Después de persistir el `Message` (donde hoy está el `logger.info`):

```python
# en WhatsAppWebhookService, tras msg_repo.add(...)
await self.dispatcher.enqueue(
    conversation_id=conversation.id,
    tenant_id=conversation.organization_id,
)
```

- El `Dispatcher` es un wrapper fino sobre el cliente Redis async (`redis.asyncio.from_url(settings.redis_url)`); expone `async enqueue(conversation_id, tenant_id)` que hace `LPUSH agent:dispatch` con el payload JSON del contrato.
- **No** mete el lock acá — el lock es responsabilidad del worker (consumer), no del producer.
- El webhook debe seguir devolviendo `200` rápido a Meta: encolar y responder; nada de procesar inline.

### Parte de N — worker + lock + handler stub

Proceso aparte (entrypoint nuevo, p. ej. `src/server/worker.py`; en compose, un servicio que corre el worker en vez de uvicorn):

```
loop:
  payload = BRPOP agent:dispatch        # bloquea hasta que haya item
  got = SET agent:lock:{conversation_id} 1 NX EX 30
  if not got:                           # ya hay otro turno en vuelo → re-encola y sigue
      LPUSH agent:dispatch payload; continue
  try:
      await handler(conversation_id, tenant_id)   # M1: STUB que loguea; integración: AgentService.process_message
  finally:
      DEL agent:lock:{conversation_id}
```

- **Handler en M1 = stub**: solo `logger.info("dispatch.received", conversation_id=..., tenant_id=...)`. Sin LLM, sin DB más allá de lo que el stub quiera leer. En la **integración** se reemplaza por `AgentService.process_message(conversation_id, tenant_id)` (M4).
- TTL del lock (`EX 30`) como red de seguridad: si el worker muere a mitad, el lock no queda colgado.
- El worker abre su propia `AsyncSession` por item (no comparte la del request).

### Build spec — `dispatcher.py` + `worker.py`

**`shared/dispatcher.py` (lo usan producer C y consumer N — fuente única del contrato):**
- Wrapper fino sobre `redis.asyncio`; construido desde `settings.redis_url` (en Docker `redis://redis:6379/0`).
- `async enqueue(conversation_id, tenant_id) -> None` → `LPUSH agent:dispatch` con `json.dumps({"conversation_id": str(...), "tenant_id": str(...)})`.
- `async dequeue(timeout) -> dict | None` → `BRPOP agent:dispatch` y `json.loads` del payload.
- `async acquire_lock(conversation_id, ttl=30) -> bool` → `SET agent:lock:{id} 1 NX EX ttl`; `async release_lock(conversation_id)` → `DEL`.
- Nombres de lista/lock/payload **viven acá**; cambiarlos = actualizar el contrato en este doc.

**`worker.py` (entrypoint nuevo, corre como proceso aparte):**
- `python -m server.worker` arranca el loop `dequeue → acquire_lock → handler → release_lock` (pseudocódigo arriba).
- **Session por item:** usa el `async_sessionmaker` compartido; abre/cierra una `AsyncSession` por mensaje, no una global.
- **Handler M1 = stub** que loguea (structlog vía `shared/logger`); en integración se inyecta `AgentService.process_message`. El handler es un parámetro/inyección, no un import duro — así M1 no depende de M4.
- **Graceful shutdown:** atrapar `SIGINT`/`SIGTERM`, terminar el item en vuelo, liberar lock, cerrar conexión Redis y engine. Que `docker compose down` no deje locks colgados (el TTL es la red de seguridad, no la vía normal).
- **Catch-up al arrancar (#187):** antes del loop, `_catch_up_on_start` corre `run_catch_up` una vez — barre `conversation` AI-elegibles (`is_ai_active` y sin `closed_at`) cuyo último turno en `ai_chat_histories` es un `user` sin responder y las **re-encola** a `agent:dispatch`. Recupera los inbounds que quedaron colgados si la cola de Redis se perdió/vació estando el sistema caído (Redis persistente ya drena solo; esto cubre la pérdida de cola). Idempotente: `process_message` corta si el último turno ya es del agente (dispatch duplicado) y el lock por conversación serializa. Best-effort: un fallo del barrido se loguea y no impide consumir. Contrato en `services/catch_up_service.py`.
- Tamaño: <200 líneas, funciones <50; `ruff`+`mypy` limpios.

**Compose:** agregar servicio `worker` en `docker-compose.yml` reusando la imagen del backend, con `command: python -m server.worker`, mismo `env_file`, `depends_on: [postgres, redis]`. No expone puertos.

### Prueba local de M1 (aislada, sin LLM)

Dos terminales. Webhook real persistiendo + worker stub consumiendo. **No** se prueba contra WhatsApp real.

```powershell
# Terminal 1 — stack + worker
docker compose up -d postgres redis backend   # NO el servicio worker: lo corremos en el host
python -m server.worker                        # worker stub escuchando agent:dispatch
# Nota: en el host, redis_url/database_url deben apuntar a localhost (default de config.py),
# no a los hostnames de Docker (redis/postgres). Sembrar antes: python scripts/seed_mirko.py
```

```powershell
# Terminal 2 — disparar un webhook falso. display_phone_number DEBE matchear el WA_NUMBER
# del seed (+59100000000); `from`/wa_id es el número del lead.
curl.exe -X POST http://localhost:8000/webhooks/whatsapp `
  -H "Content-Type: application/json" `
  -d '{ "object": "whatsapp_business_account",
        "entry": [ { "id": "WABA_TEST", "changes": [ { "field": "messages", "value": {
        "messaging_product": "whatsapp",
        "metadata": { "display_phone_number": "+59100000000", "phone_number_id": "PNID_TEST" },
        "contacts": [ { "wa_id": "59170000000", "profile": { "name": "Test" } } ],
        "messages": [ { "from": "59170000000", "id": "wamid.TEST1", "timestamp": "1700000000",
                        "type": "text", "text": { "body": "hola" } } ] } } ] } ] }'
```

Verificar, en orden:
1. **DB:** `SELECT id, external_id, funnel_stage FROM conversation WHERE external_id='59170000000';` → 1 fila; y un turno en `ai_chat_histories` (`role=user`, `thread_id = conversation.id`) según M0.
2. **Redis (opcional):** mientras el worker está parado, `redis-cli LLEN agent:dispatch` → 1; al levantarlo, vuelve a 0.
3. **Worker:** loguea `dispatch.received` con el `conversation_id` correcto, **una sola vez** por mensaje.
4. **Lock:** mandar 2 curl seguidos del mismo `wa_id` → el worker procesa de a uno, sin solaparse.

### Definition of Done — M1

- [ ] **C:** webhook hace `LPUSH agent:dispatch` con el payload del contrato tras persistir, y responde `200` sin procesar inline.
- [ ] **N:** worker consume con `BRPOP`, toma lock `SET ... NX EX`, libera en `finally`, re-encola si el lock está tomado.
- [ ] Handler es **stub** (loguea); el cambio a `AgentService` queda para integración, no para M1.
- [ ] La prueba de consola de arriba pasa los 4 chequeos.
- [ ] `ruff` + `mypy` limpios; archivos <200 líneas, funciones <50.

---

## RBAC — 3 niveles (reemplaza el "admin = Mirko" anterior)

> **Cambio vigente (2026-06-06):** el cliente (Mirko) **no** es admin de plataforma. Editar el agente y la configuración son **solo del operador de plataforma** (Natalia + equipo, p. ej. Chris). Esto **anula** el "Config del agente: solo admin (Mirko)" de FLUJO §4 / FRONTEND_SPEC / web CLAUDE.md (esos docs se actualizan para reflejarlo).
>
> **Cambio vigente (2026-07-19, issue #200):** se **separa** la capacidad *gestionar usuarios* de *config del agente*. `client_admin` ahora **gestiona su propio staff** (alta/baja/cambio de rol) dentro de su tenant, sin tocar la config del agente ni otros tenants. Los 4 endpoints de `/users` pasan a `require_user_manager` (operador **o** client_admin); `/agents` y settings siguen en `require_platform_operator`.

| Rol | Quién | Ve / puede |
|---|---|---|
| **`platform_operator`** | Natalia + equipo (Chris) | **Todo, cross-tenant:** settings, crear/editar users y roles (incl. otros operadores, vía script), **editar agente** (system_prompt, emojis, temperatura) → genera `agent_version`, ABM completo |
| **`client_admin`** | Mirko | **Solo su organización:** inbox, tablero CRM (Gestión Venta + Gestión Postventa), sus leads/cards, takeover, **+ gestión de su propio staff** (alta/baja/cambio de rol `client_admin`↔`staff` dentro de su tenant). **NO** settings, **NO** edición del agente, **NO** crear/tocar `platform_operator`s, **NO** otros tenants |
| **`staff`** | hermana de Mirko | **Operación acotada:** inbox + pipeline Gestión Postventa, takeover. NO config, **NO gestión de usuarios/roles** |

**`client_admin` vs `staff` (decisión #200):** la distinción funcional real es la **gestión de usuarios**. `client_admin` administra el staff de su tenant; `staff` solo opera el CRM. En lo demás (inbox, tablero, takeover) son equivalentes.

**Matriz RBAC — quién puede cambiar/eliminar a quién:**

| Actor \ Target | `staff` | `client_admin` | `platform_operator` |
|---|---|---|---|
| **`platform_operator`** | ✅ (cualquier tenant) | ✅ (cualquier tenant) | ✅ (flag `is_superuser` solo por script; no vía API) |
| **`client_admin`** (su tenant) | ✅ | ✅ (incl. otros admins del tenant) | ❌ 403 |
| **`staff`** | ❌ 403 | ❌ 403 | ❌ 403 |

- **Anti-self-lockout:** nadie puede eliminar su propia cuenta ni cambiar su propio rol; `PATCH /users/me` no permite auto-desactivarse (`is_active=false` sobre uno mismo → 400).
- **Invariante "siempre ≥1 platform_operator":** no se puede eliminar al último `is_superuser`.
- **Escalamiento bloqueado:** el enum de rol asignable por API es solo `client_admin`/`staff`; el flag global `is_superuser` **no** se expone en ningún endpoint (alta de operadores queda script-only).
- **Delete = quitar membresía del tenant activo;** el `User` global se borra solo si era su última membresía (evita impacto cross-tenant).

**Reglas de enforcement:**
- El **backend revalida** cada endpoint contra el rol (no confiar en que el front oculte). `/agents` y settings exigen `platform_operator`; `/users` exige `require_user_manager` (operador o client_admin) + guardas tenant-scoped de la matriz.
- El **front** oculta UI y aplica guards de ruta por rol (reusar `hooks/use-permissions.ts` de Firefly-App, ver [[noxis-recycling]]): `canManageUsers` (operador || client_admin) separado de `canManageConfig` (operador).
- **Tenant scoping:** `client_admin`/`staff` ven y gestionan solo su `organization_id`; `platform_operator` cruza tenants. Nunca exponer ni mutar datos de otro tenant.
- Extiende el RBAC del módulo `core` (no inventar uno nuevo).

---

## M-CRM en detalle — Tablero (pipelines Gestión Venta + Gestión Postventa), basado en Firefly

> **Naming (rename 2026-08-30, migración 0035).** Los pipelines se identifican por `kind` (`'ia'` / `'human'`), que **nunca cambia**; el `name` es dato por organización y es lo único que se renombró: "Gestión IA" → **Gestión Venta**, "Gestión Humana" → **Gestión Postventa**. Se llamaban por quién trabajaba en ellos, y con la validación del comprobante y la entrega automáticas el sistema trabaja en los dos; ahora se nombran por la fase del negocio. Ningún código resuelve un pipeline por nombre.

> Owners: **C** = datos (migración + seed); **N** = API + wiring + front. Modelo de datos basado en el CRM de Firefly (`kb/schema/firefly_dev.schema.sql`), adaptado a nuestro stack async/ORM/multi-tenant. **Es extensión del modelo de datos** → este doc + DESIGN se actualizan juntos.

### Mapeo Firefly → nuestro modelo

| Firefly | Nuestro | Nota |
|---|---|---|
| `pipelines (type)` | `pipeline (kind ∈ {ia, human})` | un tablero por kind |
| `stages (position, id_status)` | `stage (position, status_code)` | columnas ordenadas |
| `stage_statuses (code won/lost)` | `stage_status (code ∈ {open, won, lost})` | lookup global; marca columnas terminales |
| `cards (id_stage)` | `card (stage_id, conversation_id)` | **1 card por conversation** |
| `traceability (from,to,at)` | `card_move (stage_from, stage_to, by, at)` | historial de movimientos |
| `opportunities (value, contact)` | (diferido) | producto único 480 Bs; `value`/contact separado = Fase 2 |
| `contacts` | (se reusa `conversation` para MVP) | nombre/phone ya viven en `conversation` |

### DDL (sketch, convenciones nuestras — `id uuid`, `organization_id`, `timestamptz`)

```sql
CREATE TABLE pipeline (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('ia','human')),
    name text NOT NULL, position int NOT NULL, is_active boolean NOT NULL DEFAULT true,
    UNIQUE (organization_id, kind));

CREATE TABLE stage_status (code text PRIMARY KEY, name text NOT NULL, color text);  -- seed: open/won/lost

CREATE TABLE stage (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_id uuid NOT NULL REFERENCES pipeline(id) ON DELETE CASCADE,
    name text NOT NULL, position int NOT NULL,
    status_code text NOT NULL REFERENCES stage_status(code) DEFAULT 'open',
    UNIQUE (pipeline_id, position));

CREATE TABLE card (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    conversation_id uuid NOT NULL UNIQUE REFERENCES conversation(id) ON DELETE CASCADE,
    stage_id uuid NOT NULL REFERENCES stage(id),
    title text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE card_move (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    card_id uuid NOT NULL REFERENCES card(id) ON DELETE CASCADE,
    stage_from_id uuid REFERENCES stage(id), stage_to_id uuid NOT NULL REFERENCES stage(id),
    moved_by text NOT NULL,                 -- 'agent' | user_id::text
    moved_at timestamptz NOT NULL DEFAULT now());
```

### Seed de los 2 pipelines (por organización, en M-CRM-data)

| Pipeline `kind` | Stages (position → name, status) |
|---|---|
| `ia` | 1 Nuevo · 2 Enganchando · 3 Calificando · 4 Calificado (open); Descalificado (lost) |
| `human` | 1 Por atender · 2 Por validar pago · 3 Pago validado · 4 **Entregado** · 5 **Cerrado** (won) · 6 **Perdido** (lost); [F2] Asistió · No asistió |

### Reglas de movimiento de card (la sinergia con el agente)

- **`funnel_stage` sigue siendo la verdad del agente** (FSM validada en M2). La card es la **proyección CRM**.
- Cuando el agente cambia `funnel_stage` → la card se mueve en el **pipeline IA** al stage espejo, y se escribe `card_move(moved_by='agent')`. Mapeo `funnel_stage → stage IA`: `new→Nuevo`, `engaging→Enganchando`, `qualifying→Calificando`, `qualified→Calificado`, `disqualified→Descalificado`.
- En **`handoff`** → la card cruza al **pipeline Gestión Postventa**, stage `Por validar pago`; `is_ai_active=false`. `card_move(moved_by='agent')`.
- En Gestión Postventa mueve el humano (drag → `card_move(moved_by=user_id)`) **o el sistema** (`moved_by='system'`): la validación del comprobante y la entrega automática mueven la card por el mismo camino, que es el único con el hook de won. Dentro de este pipeline el stage es **monótono** salvo move manual. Detalle: [FLUJO_PAGO_Y_EVENTOS.md](FLUJO_PAGO_Y_EVENTOS.md).

### Card ↔ conversación = **espejo de WhatsApp** (read model)

Al abrir una card, el panel muestra el **hilo completo de WhatsApp**, en orden temporal, uniendo **3 fuentes**:

| Fuente | Qué | Quién escribe |
|---|---|---|
| `ai_chat_histories` (role `user`) | mensajes **entrantes** del lead | webhook (M0) |
| `ai_chat_histories` (role `assistant`) | respuestas del **agente IA** | AgentService |
| `app_chat_histories` | mensajes del **humano** (takeover) | API CRM |

- El hilo del CRM es **read-only-merge** de esas 3, ordenado por timestamp → es un espejo fiel de lo que ve el lead en WhatsApp.
- **Requisito duro (sinergia con M-Meta):** **todo entrante debe persistir aunque `is_ai_active=false`** (si no, el espejo se rompe cuando el humano toma la conversación). Hoy el webhook solo guarda **texto** → el **comprobante (imagen)** se pierde: bloqueado por la investigación M-Meta.
- **Realtime:** mensajes nuevos aparecen sin recargar vía Redis Pub/Sub → WS/SSE propio (patrón `chat:new` de Firefly-Monitor, ver [[noxis-recycling]]). Hasta integrar: polling.

### API CRM (N) — superficie

`GET /crm/boards` (pipelines+stages+cards de la org) · `GET /crm/cards/{id}` (card + hilo espejo) · `POST /crm/cards/{id}/move` (→ valida + `card_move`) · `PUT /crm/conversations/{id}/ai-active` (toggle) · `POST /crm/cards/{id}/send` (mensaje humano → Meta) · `POST /crm/cards/{id}/generar-entrada`. Todo tenant-scoped + RBAC (§RBAC).

### Front (N) — reuse de Firefly-App

Reemplazar el mock de `components/crm-pipeline.tsx` por datos reales (React Query + Axios). Reusar de Firefly-App: burbujas `conversation-message.tsx`, panel takeover `opportunity-conversation-panel.tsx` (adaptar envío a Meta), `use-permissions.ts`. Stages reales (no las demo "Nuevo Lead/Seguimiento/PDF Enviado").

### Reparto + DoD

- **C (M-CRM-data):** migración de las 5 tablas + seed de los 2 pipelines con sus stages por organización. DoD: `psql` muestra 2 pipelines, stages ordenadas con `status_code`, 0 cards; FKs OK; idempotente.
- **N (M-CRM-api):** lógica handoff→card + mapeo `funnel_stage`→stage IA, API CRM, hilo espejo, realtime, front. DoD: mover card escribe `card_move`; espejo arma las 3 fuentes en orden; toggle persiste; RBAC revalidado en backend.

---

## M-Meta en detalle — Investigación CRM↔Meta (Chris · Pattern A, investigación primero)

> **Entregable = documento de findings, NO código.** Resuelve los gaps reales (verificados en `whatsapp_service.py` + `whatsapp_schemas.py` + `webhook_service.py`) **antes** de finalizar el reply-humano del CRM y el envío de QRs. Dueño: **Chris** (Meta es su dominio).

### Gaps a investigar (cada uno: ¿cómo se resuelve? ¿MVP o Fase 2?)

| # | Gap (estado hoy) | Pregunta a responder |
|---|---|---|
| 1 | **Entrante no-texto se descarta** (`webhook_service` salta `type != "text"`). El **comprobante de pago es imagen**. | ¿Cómo recibir/persistir media entrante (imagen del comprobante)? ¿Se descarga el media de Meta y se guarda dónde? Impacto en el espejo del CRM (M-CRM). |
| 2 | **Saliente solo texto** (`WhatsAppSender.send_text`). **QR de pago y QR de entrada son imágenes.** | ¿Cómo enviar media saliente vía Cloud API (upload vs link)? ¿`send_image`/`send_media`? Contrato para el agente y `/generarEntrada`. |
| 3 | **Ventana de 24h de Meta.** El staff puede responder desde el CRM **>24h** después del último mensaje del lead. | Fuera de 24h, el envío libre **falla** → requiere **plantilla HSM aprobada**. ¿Cómo detectar la ventana? ¿Qué pasa en el reply-humano del CRM fuera de ventana (bloquear UI / usar plantilla)? |
| 4 | **Statuses (delivery/read) no se procesan** (el schema los acepta, nada los consume). | ¿Vale reflejar enviado/entregado/leído en las burbujas del CRM (estados Firefly)? ¿MVP o Fase 2? |
| 5 | **HSM templates** (no implementado; FLUJO lo marca Fase 2). | Lead time de aprobación de Meta, qué plantillas mínimas hace falta para el MVP (si alguna), y cuáles son claramente Fase 2 (recordatorio + pago 2). |

### DoD — M-Meta-inv

- [ ] Doc de findings con, por cada gap: **cómo se resuelve técnicamente** + **veredicto MVP vs Fase 2** + estimación de esfuerzo.
- [ ] Clarifica el **contrato de envío de media** (lo consume el agente para QR de pago y `/generarEntrada` para QR de entrada).
- [ ] Clarifica el **manejo de entrante con media** (lo consume el espejo del CRM en M-CRM).
- [ ] Marca qué de esto **bloquea** el e2e completo (WhatsApp + card avanzando) y qué puede ir después.
