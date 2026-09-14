# SPEC — Observabilidad Fase 1: Sentry (errores back + front)

> **Estado:** ✅ **Completado**
## Objetivo

Centralizar **errores y crashes** del sistema en un solo lugar (Sentry) con alertas por
email, para detectar y diagnosticar fallas en producción del backend (FastAPI app +
worker) y del frontend (Next.js en Vercel).

**Fase 1 = SOLO error tracking.** Decidido con Natalia (2026-06-20): Sentry **SaaS free**,
errores únicamente. Logs ricos de la IA y agregador de logs van en Fase 2.

## Decisiones (acordadas)

| Decisión | Valor |
|---|---|
| Hosting | Sentry **SaaS free** (~5k errores/mes, 30 d retención). NO self-host. |
| Alcance | Errores back + front. Sin performance/tracing intensivo (`traces_sample_rate=0`). |
| Organización | 1 org (cuenta de Natalia), 2 proyectos: `marketing-server` (python) + `marketing-web` (nextjs) → se ven juntos, separados por proyecto. |

## No-objetivos (Fase 2+, NO en este spec)

- Logging del LLM (tokens, latencia, prompt/respuesta, costo, decisión del router).
- Agregador de logs buscable (Loki/Axiom/Better Stack).
- Tracing distribuido / request-id por contextvars.
- `log_level` por env (hoy hardcoded INFO prod / DEBUG dev — no se toca).

## Estado actual (del mapeo del backend)

- **structlog** ya configurado (`shared/logger.py`, JSON en prod) — base buena, se conserva.
- Exception handling: **solo** `domain_exception_handler` (`shared/handlers.py`) para
  `DomainException` (4xx de negocio). **No hay catch-all** → las 5xx inesperadas se pierden
  sin traceback centralizado.
- Dos `try/except` que **tragan** la excepción (loguean y siguen, no relanzan):
  `webhook_router.py` (~64-69, responde 200 a Meta) y `dispatch_handler.py` (~51-56, worker).
- Sin `sentry-sdk` ni APM. Settings vía Pydantic (`config.py`), `.env` con `APP_ENV`.
- Docker prod: app + worker desde imágenes ghcr.io; **sin log driver** configurado → riesgo
  de que `docker logs` llene el disco del VPS.

## Diseño — Backend (`server`)

### Init de Sentry
- Nuevo `shared/observability.py` con `init_sentry()`; llamado al arranque de **app**
  (`main.py`) y de **worker** (`worker.py main()` — proceso separado, init propio).
- Parámetros desde Settings:
  - `send_default_pii=False`
  - `environment` = `APP_ENV`
  - `release` = `SENTRY_RELEASE` (commit SHA, opcional)
  - `traces_sample_rate` = `SENTRY_TRACES_SAMPLE_RATE` (default **0.0** → solo errores)
  - `integrations`: `FastApiIntegration` (auto-captura 5xx no manejadas).
- **DSN vacío ⇒ no se inicializa** (dev/local sin ruido). Sin DSN nada rompe.

### Capturas explícitas (errores que hoy se tragan)
| Punto | Acción |
|---|---|
| `webhook_router.py` except (responde 200 a Meta) | `sentry_sdk.capture_exception(e)` antes de loguear |
| `dispatch_handler.py` except (worker, no relanza) | `sentry_sdk.capture_exception(e)` antes de loguear |

`domain_exception_handler` (4xx de negocio) **no** se envía a Sentry (son esperados).

### PII / privacidad (crítico — datos del cliente)
- `send_default_pii=False`.
- `include_local_variables=False`: evita que el contenido de los mensajes de WhatsApp
  (datos personales) viaje en las variables locales de los tracebacks. Se pierde algo de
  contexto de debug a cambio de no exponer PII a un SaaS externo.
- `before_send` hook que descarta/ofusca campos sensibles conocidos (texto del mensaje,
  `wa_id`, API keys). **Nunca** adjuntar prompts ni respuestas del LLM al scope.

### Config (`config.py` + `.env.example`)
Nuevas vars: `SENTRY_DSN` (default `""`), `SENTRY_TRACES_SAMPLE_RATE` (default `0.0`),
`SENTRY_RELEASE` (default `""`). Documentar en `.env.example` (DSN vacío = off).
En el VPS: setear `SENTRY_DSN` real en `/home/deploy/marketing-services/.env`.

### Docker (higiene de logs, independiente de Sentry)
Agregar a app y worker en `docker-compose.prod.yml`:
```yaml
logging:
  driver: json-file
  options: { max-size: "10m", max-file: "3" }
```

## Diseño — Frontend (`web`, Next.js 16 / Vercel)

- Dependencia `@sentry/nextjs` (pineada; fijar versión compatible con Next 16 al implementar).
- Setup **manual** (no wizard interactivo, para CI predecible): `instrumentation.ts`
  (server/edge + `onRequestError`), client init, `withSentryConfig` en `next.config.ts`,
  y `app/global-error.tsx` para el App Router.
- DSN por `NEXT_PUBLIC_SENTRY_DSN` (público, OK exponer). `tracesSampleRate: 0`.
- Source maps a Sentry: **opcional** en Fase 1 (requiere `SENTRY_AUTH_TOKEN` en el build de
  Vercel). Si se omite, los stacktraces quedan minificados pero funcionales.
- DSN ausente ⇒ no-op (no rompe build ni runtime). Verificar que `pnpm build` sigue verde
  (respetar workaround pnpm v11).
- Vercel (cuenta de Natalia): setear `NEXT_PUBLIC_SENTRY_DSN` (+ opcional `SENTRY_AUTH_TOKEN`).

## Reportes y alertas por email (validado)

**Reporte semanal — lo hace Sentry automáticamente.** Sentry envía un **Weekly Report por
email todos los sábados** con un resumen de la actividad de la org (errores nuevos, más
frecuentes, tendencia). Es una notificación **por usuario**, se gestiona en *User Settings →
Notifications* (activada por defecto). ⇒ Para el reporte semanal pedido alcanza con que Natalia
tenga su cuenta en la org con esa notificación ON. (Fuentes al pie.)

> Matiz: el weekly report es un **resumen**, no tiempo real. Para enterarse al instante cuando
> algo se rompe se suma una **Issue Alert**.

| # | Notificación | Cuándo | Recomendación |
|---|---|---|---|
| N1 | **Weekly Report** (nativo) | sábados, resumen | ✅ siempre (cero setup, cubre lo pedido) |
| N2 | **Issue Alert: nuevo issue → email** | al instante, primer evento de un error nuevo | ✅ recomendado (no esperar al sábado para un crash) |
| N3 | **Issue Alert: spike** (frecuencia > umbral) | cuando un error se dispara | opcional, si hay volumen |

Recomiendo **N1 + N2**. Todo en el free tier; se configura en Alerts (proyecto) + Notifications (usuario).

## Opciones de captura de errores (elegir cobertura)

El usuario quiere "capturar los errores con try/catch y enviarlos a Sentry". Nota de diseño:
**no hay que envolver todo en try/catch** — la auto-instrumentación ya captura las excepciones
no manejadas de las rutas HTTP. Los `try/except` explícitos se usan donde la auto-instrumentación
**no llega**: código que ya traga el error, y el loop async del worker.

| Opción | Qué cubre | Costo | Veredicto |
|---|---|---|---|
| **C1 — base** | `FastApiIntegration` (5xx HTTP auto) **+** `capture_exception` en los 2 `try/except` que tragan (webhook a Meta, dispatch del worker) | mínimo | ✅ **recomendada para Fase 1** |
| **C2 — amplia** | C1 **+** `LoggingIntegration(event_level=ERROR)`: todo `logger.error/exception` se envía solo, sin tocar cada try/except | 1 línea de config | ⚠️ cobertura total, pero cuidar ruido vs free tier (5k/mes) |
| **C3 — máximo control** | C1 **+** middleware catch-all que enriquece cada evento (path, `tenant_id` sin PII) y re-loguea | más código | para cuando haya volumen/tracing (Fase 2) |

**Recomendación:** arrancar con **C1** (cubre los crashes reales: HTTP, procesamiento de
mensajes, worker). C2 queda como "flip de una línea" si más adelante querés cobertura total.

## Plan de implementación — 2 PRs

**PR-A (`server`):**
1. `pyproject.toml`: `sentry-sdk[fastapi]` (versión pineada).
2. `shared/observability.py`: `init_sentry()` (lee Settings; no-op si DSN vacío) + `before_send` (scrub PII).
3. `main.py`: `init_sentry()` al arranque. `worker.py main()`: idem (proceso aparte).
4. `webhook_router.py` y `dispatch_handler.py`: `capture_exception(e)` en el `except` que hoy traga (opción C1).
5. `config.py` + `.env.example`: `SENTRY_DSN`, `SENTRY_TRACES_SAMPLE_RATE`, `SENTRY_RELEASE`.
6. `docker-compose.prod.yml`: `logging` json-file con rotación (app + worker).
7. `/close` (ruff/mypy/pytest) → PR.

**PR-B (`web`):**
1. `@sentry/nextjs` (versión compatible con Next 16, pineada).
2. `instrumentation.ts` + client init + `withSentryConfig` en `next.config.ts` + `app/global-error.tsx`.
3. `.env` / Vercel: `NEXT_PUBLIC_SENTRY_DSN`.
4. Verificar `pnpm build` verde (workaround pnpm v11) → `/close` (lint/tsc/build) → PR.

## Plan de remediación — triage → subsanar (con opciones)

Cómo se trabaja un error una vez que Sentry lo captura:

1. **Detección:** Sentry agrupa eventos en *issues*. N2 avisa al instante; N1 resume el sábado.
2. **Triage / priorización** por impacto (Sentry da nº de eventos, usuarios afectados, primera/última vez, release):

   | Severidad | Definición | Acción |
   |---|---|---|
   | **P0** | rompe el flujo core (bot no responde, no se crea card, login caído) | fix inmediato |
   | **P1** | degradación parcial (un paso falla, hay workaround) | fix en el día |
   | **P2** | menor / cosmético | backlog |

3. **Subsanar:** reproducir con el stacktrace + contexto de Sentry → fix en el repo por el flujo del proyecto (rama desde `main` → PR → `/close`). Vincular `release` (`SENTRY_RELEASE`=commit) para saber en qué versión entra el fix.
4. **Verificar / cerrar:** marcar *Resolved* en Sentry. Si reaparece en un release posterior, Sentry **reabre** el issue (regression) → señal de fix incompleto.
5. **Prevenir:** revisar el weekly report; un error recurrente → test de regresión.

**Opciones para el ciclo de remediación (elegir):**

| Opción | Qué da | Setup | Veredicto |
|---|---|---|---|
| **R1 — manual** | triage en el dashboard de Sentry → fix manual en el repo | cero | ✅ empezar acá |
| **R2 — Sentry ↔ GitHub** (free) | crear GitHub Issue desde Sentry, *resolve in commit* (`Fixes <ID>` cierra el issue al mergear), release tracking (qué commit introdujo/corrigió) | bajo | ✅ siguiente paso (mucho valor) |
| **R3 — Sentry ↔ Jira** (Atlassian) | auto-crear tickets en Jira | medio | solo si el pipeline de CRs vive en Jira |

**Recomendación:** **R1 ahora + R2 cuando estabilice** (cerrar issues desde los commits del fix).

## Acciones manuales de Natalia (fuera de código)

1. Crear org en sentry.io (free) + 2 proyectos → obtener 2 DSN.
2. Server: `SENTRY_DSN` en el `.env` del VPS + redeploy.
3. Web: `NEXT_PUBLIC_SENTRY_DSN` en env de Vercel + redeploy.
4. **Notificaciones:** confirmar **Weekly Report ON** (User Settings → Notifications) y crear la
   **Issue Alert "nuevo issue → email"** (N2) en cada proyecto.
5. (Opcional) `SENTRY_AUTH_TOKEN` en Vercel para source maps del front; integración GitHub (R2).

## DoD

- Una excepción no manejada en la app y un error en el worker aparecen como issues en Sentry.
- Webhook/dispatch errors capturados (probado provocando un error controlado).
- Un error de cliente en el front aparece en Sentry.
- **PII verificada:** el contenido de los mensajes NO aparece en los eventos.
- **Notificaciones:** Weekly Report confirmado + Issue Alert (N2) creada y testeada (llega el email).
- `/close` verde en ambos repos. Rotación de docker logs aplicada.

## Fuentes (validación weekly report)

- Sentry — Notifications (weekly report: resumen semanal, sábados, por email): https://docs.sentry.io/product/notifications/
- Sentry — Alerts (issue alerts → email): https://docs.sentry.io/product/alerts/
