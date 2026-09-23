# Spec — M-Outbound: envíos iniciados por el negocio (plantillas de Meta)

Estado: **etapa A implementada** (2026-09-23). Etapas B–E pendientes, ver §6.

## 1. Intent

Hasta la etapa A el sistema solo **respondía**: el agente y el CRM escriben dentro de la
ventana de 24 h que abre el lead. Todo lo que el negocio necesita **iniciar** —entregar
una entrada a quien ya no escribe, recordar un evento, reactivar un lead frío— requiere
una plantilla aprobada por Meta, y nada en el backend las enviaba, registraba ni sabía si
llegaron (#267, #298).

M-Outbound es el bounded context `outbound`: **una sola puerta de salida** para todo
mensaje iniciado por el negocio, con registro, idempotencia, baja y estados de entrega.

## 2. Reglas de Meta que condicionan el diseño

- Fuera de la ventana de 24 h solo se puede enviar una **plantilla aprobada**. Meta cobra
  por conversación iniciada; las de categoría MARKETING se cobran siempre.
- Meta responde 2xx al aceptar y puede descartar después. La única señal es el array
  `statuses` del webhook (`sent` → `delivered` → `read`, o `failed` con código).
- El lead puede pedir no recibir más mensajes. Meta exige honrarlo; una tasa alta de
  bloqueos baja la calidad del número y el tope diario.
- Tope de conversaciones iniciadas por el negocio: hoy 2.000/24 h (negocio verificado).

## 3. Modelo de datos (migración `0038_outbound_messages`)

`outbound_message` — una fila por plantilla enviada o descartada a propósito.

| Columna | Uso |
|---|---|
| `template_name`, `language`, `variables`, `rendered_text` | Qué se mandó, con el texto ya renderizado para el hilo del CRM |
| `purpose` | `entry` · `event_reminder` · `reactivation` · `manual`. Sirve para topes diarios y filtros |
| `dedupe_key` (única) | Idempotencia de los jobs automáticos (`reminder:{event}:{card}:48h`) |
| `wamid` (único) | Id que devuelve Meta; cruza con los `statuses` del webhook |
| `status` | `queued` · `sent` · `delivered` · `read` · `failed` · `skipped` |
| `error_code`, `error_detail` | Del status `failed` de Meta, o del error HTTP al enviar |
| `conversation_id`, `card_id`, `wa_id` | Trazabilidad hacia el CRM |

`marketing_opt_out` — números que pidieron la baja, únicos por organización.

## 4. Piezas (etapa A)

- **`domain/templates.py`** — registro local de las plantillas cargadas en Meta
  (`entry_qr_ready`, `recordatorio_evento`, `reactivacion_leads`): cuerpo, categoría,
  si lleva imagen de encabezado. `build_components()` arma el payload de Meta y valida
  la cantidad de variables. Cambiar un texto acá **no** cambia lo que Meta envía: hay que
  editarlo en el Administrador de WhatsApp y pasar por revisión.
- **`services/template_sender.py`** — `TemplateSender.send(SendRequest)`:
  1. `dedupe_key` ya usada → `skipped/duplicate`, sin tocar Meta.
  2. Número en `marketing_opt_out` → fila `skipped/opt_out`, sin tocar Meta.
  3. Inserta la fila como `failed` **antes** de llamar a Meta (una caída a mitad deja rastro).
  4. Llama `WhatsAppSender.send_template()`, que ahora devuelve el `wamid`.
  5. Éxito → `sent` + `wamid` + espejo en `ai_chat_histories` (`kind: template`) para
     que el hilo del CRM lo muestre como mensaje del agente.
- **`services/status_service.py`** — `OutboundStatusService.apply(statuses)`: actualiza
  por `wamid` con precedencia (`sent` < `delivered` < `read`; `failed` es terminal). Los
  mensajes libres del agente/humano no están en la tabla y se ignoran.
- **`services/opt_out_service.py`** + **`domain/opt_out.py`** — si el texto entrante,
  normalizado, es una palabra de baja (`BAJA`, `STOP`, "no quiero recibir más mensajes"…),
  el webhook registra la baja, responde una confirmación fija dentro de la ventana y
  **no encola el turno del agente**. La baja nunca se pierde por un fallo del envío.
- **`webhook_service.py`** — llama `status_service.apply()` en cada change y desvía el
  texto de baja antes de `dispatcher.enqueue()`.

## 5. Decisiones

- **Una tabla, no un campo en `ai_chat_histories`.** El hilo es del lead; el registro de
  envíos es operativo (topes, fallos, auditoría) y necesita índices propios.
- **Fila pesimista.** Se inserta `failed` y se sube a `sent`: si el proceso muere entre
  el POST y el commit, la fila cuenta como fallo y no como "nunca pasó".
- **Baja por texto exacto, no por LLM.** Determinístico y auditable; el agente no decide
  sobre una obligación legal. Falsos negativos ("ya no me escriban por favor") los
  atiende el agente como conversación normal y un humano puede registrar la baja a mano
  (etapa E).
- **El espejo usa `role: assistant`** con `kind: template` para que el mirror existente lo
  muestre sin cambios; el CRM puede distinguirlo por `kind` cuando quiera un chip.

## 6. Pendiente (etapas B–E)

- **B** — `fulfillment_service`: al caer en `delivery_pending` por ventana, enviar
  `entry_qr_ready` con la imagen del QR vía `TemplateSender` en vez de esperar al lead.
- **C** — job diario en el worker: `recordatorio_evento` 48 h y 3 h antes a las entradas
  válidas de cada evento futuro; `dedupe_key` por evento+card+ventana.
- **D** — job de reactivación: regla por etapa del embudo + días sin respuesta + novedad
  del mes; exclusiones (baja, cerradas, humano activo, contacto reciente); tope diario
  (100) y horario permitido (07:00–22:00 America/La_Paz).
- **E** — CRM: pantalla Seguimientos, reglas en Ajustes, botón de envío manual en la card
  y registro manual de la baja.
