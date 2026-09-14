# Spec — generate-entry: entrada (QR) de acceso

> **Estado:** ✅ **Completado**. **Owner: Chris.** Carve-out de la Parte B de M-CRM-api slice 2b (la Parte A — SSE — sigue siendo de Natalia). Depende de: M-CRM-api slice 1 (#15) + slice 2 (#16), ambos en main. Ref: [FLUJO_AGENTE.md](FLUJO_AGENTE.md) §1, [SPECS_MVP.md](SPECS_MVP.md) §"M-CRM".
>
> **Por qué es de Chris:** el ciclo de vida del QR (generar → **enviar por WhatsApp**) cruza al dominio Meta. El envío es media saliente, que hoy no existe y se habilita con M-Meta-impl (suya). Darle el ciclo completo deja la entrada bajo un solo dueño y se auto-destraba.

## 1. Intent

La acción de Gestión Humana al **validar el pago**: genera la **entrada (un QR de acceso al evento)**, mueve la card a `Entrada enviada` (pipeline humano), registra el movimiento y publica el evento al tablero. El **envío del QR al lead por WhatsApp** se cablea cuando M-Meta habilite media saliente.

## 2. Estado actual (de qué partimos)

- **Slice 1 (#15) + slice 2 (#16) en main:** `CardService.sync` reconcilia funnel→card; `BoardService.move_card(card_id, stage_id, user_id, tenant_id)` mueve + registra `card_move(moved_by=user_id)` + publica `card_moved` a `crm:events:{tenant_id}`. API CRM REST operable (`crm/api/router.py`).
- **Pipeline humano** (seed 2 pipelines, M-CRM-data #14): tiene los stages de Gestión Humana, entre ellos `Pago validado` y `Entrada enviada` (verificar los `code`/`name` exactos en `crm/seed.py` al construir).
- **Pub/Sub:** contrato del canal `crm:events:{tenant_id}` en [`shared/pubsub.py`](../src/server/shared/pubsub.py); `Publisher.publish` ya existe.
- **No existe:** ninguna noción de "entrada"/QR en el modelo de datos ni endpoint para generarla. `send_text` (WhatsApp) hoy es **solo texto** (no media).

## 3. Alcance

### IN (lo que entrega esta pieza)
1. Modelo + migración de la tabla `qr_entry` (ver §4).
2. Endpoint `POST /api/v1/crm/cards/{card_id}/generate-entry` (auth + tenant-scoped) que:
   - valida que la card esté en el pipeline `human` (idealmente en `Pago validado`),
   - **genera la entrada**: token de acceso (`uuid4`) + representación QR, persiste una fila `qr_entry`,
   - mueve la card a `Entrada enviada` vía el `BoardService` existente (reusa `move_card` → `card_move(moved_by=user_id)` + publica `card_moved`),
   - es **idempotente**: si la card ya tiene `qr_entry`, no crea otra (devuelve la existente).
3. La entrada queda **generada y disponible en la card** (token + QR descargable/visible).
4. **Envío por WhatsApp del QR** (media saliente) — se implementa **junto con / después de M-Meta-impl** (misma dueña). Hasta entonces queda generada y visible; el envío automático es un paso aditivo posterior, no bloquea el resto del DoD.

### OUT (borde duro — no hacer en esta pieza)
- **SSE / realtime** → es de Natalia (Parte A de 2b). No tocar `shared/pubsub.subscribe` ni el endpoint de eventos.
- **Escaneo del QR + marcado de asistencia** → Fase 2 (FLUJO §"Fase 2").
- **No modificar** auth/RBAC, el modelo de datos CRM existente (pipeline/stage/card/card_move) ni los endpoints REST del slice 2, fuera de **agregar** el endpoint nuevo y la tabla `qr_entry`.

## 4. Modelo de datos — tabla `qr_entry` (decidido)

Tabla **dedicada** (no columna en `card`): mantiene `card` liviana y sirve para el escaneo/asistencia de Fase 2. Nombre descriptivo `qr_entry` (modelo `QrEntry`) — la entrada es un QR de acceso.

| Columna | Tipo | Notas |
|---|---|---|
| `id` | UUID PK | |
| `card_id` | UUID FK → `card.id` | `unique` (1 entrada por card en el MVP) |
| `token` | text | `uuid4`, valor de acceso codificado en el QR; `unique` |
| `qr_ref` | text | referencia a la imagen QR (data-URI/base64 o path; decidir en build, ver §8) |
| `created_at` | timestamptz | |

- **Migración Alembic** encadena después del head actual (`alembic heads` al ramificar — hoy `c4f1a2b3d5e6`). **Identificadores y nombres en inglés** (CLAUDE.md). SQLite/tests usan `Base.metadata.create_all`, no la migración.
- Multi-tenant: `qr_entry` cuelga de `card`, que ya está scoped por tenant; toda query parte de la card validada por `tenant_id`.

## 5. Endpoint (contrato)
`POST /api/v1/crm/cards/{card_id}/generate-entry` — auth (`CurrentUser`) + tenant-scoped (`ctx.tenant.id`).
- 200 → `QrEntryOut` (`token`, `qr_ref`, `card_id`); card movida a `Entrada enviada`.
- 400 si la card no está en el pipeline `human` (o no en `Pago validado`, según se fije).
- 404 si la card no existe en el tenant.
- Idempotente: segunda llamada devuelve la `qr_entry` existente sin re-mover.

## 6. Dependencia Meta (se auto-destraba)
- El **envío real** del QR usa media saliente → bloqueado por **M-Meta-inv** ([[project-mmeta-standby]]) y se construye en **M-Meta-impl**, ambas de Chris.
- **Secuencia recomendada:** (1) genera+registra+mueve ahora (cumple el DoD core), (2) cablea el envío por WhatsApp cuando M-Meta-impl exponga `send_media`.

## 7. Criterios de aceptación (DoD)
- Card en `Pago validado` → `POST .../generate-entry` → fila `qr_entry` creada (token único + `qr_ref`) + card en `Entrada enviada` + `card_move(moved_by=<user_id>)` + evento `card_moved` publicado.
- Segunda llamada sobre la misma card → idempotente (no duplica `qr_entry`, no re-mueve).
- Card fuera del pipeline `human` → 400; card inexistente / de otro tenant → 404; sin token → 401.
- Migración aplica sobre el head real y revierte (`downgrade`); identificadores en inglés.
- `ruff` / `ruff format --check` / `mypy --strict` / `pytest` verdes; archivos <200 líneas.
- (Envío por WhatsApp del QR: diferido a M-Meta-impl, documentado — no bloquea este DoD.)

## 8. No-funcionales / constraints
- **Librería QR: `segno`** (puro Python, sin dependencias de imagen pesadas). Fijada para no elegir a ciegas; si se descarta, justificar en build.
- `qr_ref`: data-URI/base64 en la columna para el MVP (simple, sin storage externo); migrable a object storage en Fase 2.

## 9. Coordinación con Natalia (evitar choques)
- **Árbol Alembic:** RBAC (N) también agrega una migración y ramifica del **mismo head** → el árbol se bifurca y pide *merge migration* (precedente: `33feacab726f_merge_t1_t2`). **Regla:** rama desde `main` actualizado, `down_revision` = head del momento (`alembic heads`); si RBAC mergea primero, rebasá tu `down_revision` sobre él. (Misma regla branch-from-main del repo — no stackear.)
- El **modelo de datos CRM** lo parió N (M-CRM-data #14): `qr_entry` es **aditiva** (tabla nueva, FK a `card`), no toca el esquema existente. Si surge necesidad de cambiar `card`/`card_move`, **coordinar con N antes**.

## 10. Decisiones abiertas
1. Stage de origen exigido: ¿solo `Pago validado`, o cualquier stage del pipeline `human`? (recomiendo exigir `Pago validado`).
2. `qr_ref`: data-URI en columna (MVP, recomendado) vs storage externo (Fase 2).
