# Estado del proyecto y runbook (cómo levantar y probar)

> **Actualizado 2026-06-10.** Reemplaza `CONTINUE.md`, `HAPPY_PATH.md` y `ROADMAP_INFRA.md` (obsoletos, borrados).

## Estado actual

- **Pago → entrega → eventos → escáner (2026-08-23):** el comprobante se valida por visión con checks determinísticos, la entrega sale sola (entrada QR para presencial, links para virtual), los pagos auto-aprobados pasan por una cola de conciliación humana, los eventos son un catálogo propio y la entrada se escanea en la puerta. **Implementado y probado, sin desplegar.** Documento canónico: [FLUJO_PAGO_Y_EVENTOS.md](FLUJO_PAGO_Y_EVENTOS.md), con su checklist de deploy (§10) y sus deudas abiertas (§11).
- **web — front MVP completo (B1–B6, 2026-06-10):** live en `mirkocalzadilla.com` (Vercel Hobby, cuenta personal; repo **público** `funnelops-marketing-services/web`). CRM en `/crm` con tablero real (API slice 2), RBAC alineado, `/generarEntrada`, input de respuesta humana, espejo de media en el hilo (B1–B4), **realtime SSE** (B5, PR #11; DoD de runtime validado 2026-06-10 contra el backend Docker) y **M-Config front** `/crm/agents` + `/crm/settings` (B6, PR #12 + `GET /agents` server PR #46). Sin pendientes de front para el MVP.
- **server — track MVP completo en main (2026-06-10):** FastAPI async + Postgres + Redis. Módulo `core` (tenants/users/auth + **RBAC 3 niveles**) + módulo `agent` (esquema `agents`, hand-coded: router determinístico + FSM + loop tool_use; **LLM multi-provider**, OpenAI validado) + módulo `crm` (tablero, card = proyección del funnel, hilo espejo con **media**, reply humano, QR de entrada). **Realtime SSE** `GET /crm/events` (PR #35, ver smoke abajo) · **M-Config** `GET/PUT /agents/{id}` versionado + ABM users/roles (PR #36) · **espejo de media** en el hilo (PR #37). Meta Cloud API entra (webhook + HMAC) y sale (`WhatsAppSender`); el envío saliente queda pendiente de token real (hoy 401 placeholder, atrapado).
- **Deploy:** pipeline M-Deploy mergeado (PRs #33/#34: `docker-compose.prod.yml` + Caddy/TLS + `deploy.yml` → ghcr.io → SSH al VPS; ver [RUNBOOK_DEPLOY.md](RUNBOOK_DEPLOY.md)). Primer deploy real al VPS pendiente.
- **Validado e2e (2026-06-06):** agente **generativo vía OpenAI** (`LLM_PROVIDER=openai`, `gpt-4o-mini`) — turnos offer/qualify + handoff con resumen LLM + movimiento de card (incl. cruce al pipeline `kind: 'human'`, hoy Gestión Postventa). Ver [BITACORA](../BITACORA.md) (entrada "VALIDACIÓN e2e") y la sección "Smoke del agente generativo" abajo.
- **Decisión vigente (2026-06-05):** reescribir el módulo `agent` al **esquema `agents`** (multi-dominio, versionado, RAG) y construir el agente **hand-coded** (router determinístico + FSM + loop tool_use). El webhook + sender Meta se conservan. Plan de trabajo: [SPECS_MVP.md](SPECS_MVP.md).

## Cómo levantar (Docker)

```powershell
cd c:\desarollo\marketing-services\server
$env:Path += ';C:\Program Files\Docker\Docker\resources\bin'   # docker no está en el PATH por defecto
docker compose up -d --build      # postgres(pgvector) + redis + backend (corre alembic + uvicorn)
docker compose ps
docker compose logs backend -f
docker compose down               # 'down -v' borra los datos del volumen
```

API: http://localhost:8000/docs · creds de prueba (las crea `scripts/seed_mirko.py`): `operador@mirko.com` / `mirko1234` (tenant `mirko`).
Front: `cd ..\web; npx pnpm dev` (:3000, CORS ya permitido).

## Smoke del agente generativo (webhook → worker → agente → card)

Prueba el lazo completo con un mensaje real. Requiere `LLM_PROVIDER=openai` + `OPENAI_API_KEY` en `.env` (ver bloque "LLM runtime"). El envío saliente a Meta dará **401** (token placeholder) y se atrapa — no afecta la validación del turno ni el movimiento de card.

```powershell
docker compose up -d --build                 # --build para hornear la dep `openai` en la imagen
docker compose exec backend python scripts/seed_mirko.py   # instancia +59100000000, pipelines CRM
```

Disparar mensajes (lead `59170000000` → instancia `+59100000000`). Cada mensaje necesita un `id` (wamid) único; la conversación se identifica por `from` (wa_id), no por wamid:

```powershell
$body = @'
{ "object":"whatsapp_business_account","entry":[{"id":"WABA_TEST","changes":[{"field":"messages","value":{
  "messaging_product":"whatsapp","metadata":{"display_phone_number":"+59100000000","phone_number_id":"PNID_TEST"},
  "contacts":[{"profile":{"name":"Lead"},"wa_id":"59170000000"}],
  "messages":[{"from":"59170000000","id":"wamid.T1","timestamp":"1749240000","type":"text","text":{"body":"hola"}}]}}]}]}
'@
Invoke-RestMethod -Uri http://localhost:8000/webhooks/whatsapp -Method Post -ContentType application/json -Body $body
# repetir cambiando id (wamid.T2…) y body: "cuanto cuesta el curso?" (OFFER generativo), "quiero inscribirme, como pago?" (QUALIFY→handoff)
```

Verificar en Postgres (todas las tablas están en el schema `public`, no `agents`):

```powershell
# turnos persistidos (la reply generativa es texto libre fundado en agent.config)
docker compose exec -T postgres psql -U marketing -d marketing_platform -c "SELECT message_order, message->>'role' role, left(message->>'content',120) c FROM ai_chat_histories WHERE thread_id=(SELECT id::text FROM conversation WHERE external_id='59170000000') ORDER BY message_order;"
# funnel + card (espejo del funnel; handoff cruza a pipeline human 'Por validar pago')
docker compose exec -T postgres psql -U marketing -d marketing_platform -c "SELECT c.funnel_stage, c.is_ai_active, s.name, p.kind FROM conversation c JOIN card cd ON cd.conversation_id=c.id JOIN stage s ON s.id=cd.stage_id JOIN pipeline p ON p.id=s.pipeline_id WHERE c.external_id='59170000000';"
# provider efectivo: llamadas salientes
docker compose logs worker | Select-String "api.openai.com/v1/chat/completions"
# eventos realtime del CRM (canal Redis crudo)
docker compose exec -T redis redis-cli SUBSCRIBE crm:events:<tenant_id>
```

SSE del CRM (slice 2b) — el stream que consume el front; `<jwt>` sale del login:

```powershell
curl.exe -N "http://localhost:8000/api/v1/crm/events?token=<jwt>"   # heartbeat ': ping' cada ~15s
# en paralelo: POST /crm/cards/{id}/move → llega data: {"type":"card_moved",...} (shape unificado)
# al cortar: docker compose exec redis redis-cli CLIENT LIST no debe mostrar el 'subscribe'
```

Señales de éxito en `docker compose logs worker`: `crm.card_synced` (kind/stage), `handoff.published` (en derivaciones), `agent.dispatch_error` con 401 de Meta (esperado, el turno ya quedó commiteado). **Nota:** el primer mensaje de una conversación nueva siempre rutea a GREETING (determinístico) aunque el texto sea otro (`is_first_turn = funnel==new`); mandá un "hola" primero y recién el 2º turno entra a los flujos generativos.

## Gotchas (no perder)

- El backend **debe correr en Docker, NO local con venv**: asyncpg desde el host Windows hacia el Postgres del contenedor falla (`WinError 64`). El `.env` usa hosts `postgres`/`redis`.
- Hay una env var de usuario `DATABASE_URL` (proyecto printshop, `:5433`) que pisa el `.env` si se corre local — irrelevante en Docker, **no borrar**.
- `pytest` no corre en SQLite por columnas JSONB/PG → validar con Postgres real, o tests de **lógica pura** (FSM, tools) sin DB.
- Imagen Postgres = **`pgvector/pgvector:pg16`** (para el esquema `agents`).
- Usar **Python 3.12** (el del sistema es 3.14, no usar).

## Infra

- **Front:** Vercel. **Backend:** pendiente de deploy en VPS Hostinger `2.24.101.201` (Ubuntu 24.04); subdominio futuro `api.mirkocalzadilla.com`. Detalles (cuentas, dominios, DNS) en la memoria del proyecto (`reference_infrastructure`).

## Docs del proyecto

| Doc | Para qué |
|---|---|
| [DESIGN_AGENT_SALES_FLOW.md](DESIGN_AGENT_SALES_FLOW.md) | Negocio / modelo de producto del agente |
| [DESIGN_AGENT_ARCHITECTURE.md](DESIGN_AGENT_ARCHITECTURE.md) | Diseño técnico (datos, orquestación, runtime, FSM) |
| [FLUJO_AGENTE.md](FLUJO_AGENTE.md) | Comportamiento (flujo, persona, config) |
| [SPECS_MVP.md](SPECS_MVP.md) | Specs de trabajo / división Chris ↔ Natalia |
| [BITACORA.md](../BITACORA.md) | Registro de cambios antes de cada commit |
