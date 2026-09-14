# Flujo, persona y config del agente — comportamiento (MVP: curso)

> **Estado:** definido 2026-06-05. Canónico para el **comportamiento** del agente.
> Técnico: [DESIGN_AGENT_ARCHITECTURE.md](DESIGN_AGENT_ARCHITECTURE.md) · Negocio: [DESIGN_AGENT_SALES_FLOW.md](DESIGN_AGENT_SALES_FLOW.md) · Specs de trabajo: [SPECS_MVP.md](SPECS_MVP.md)

## 1. Flujo end-to-end

```
─── FASE IA  (agente, is_ai_active = true) ──────────────────────
lead conversa → info / precio / ciudad-fecha
lead: "quiero inscribirme"
   → agente ENVÍA QR DE PAGO + "pagá y mandame el comprobante"
   → set_lead_stage(qualified)
lead manda comprobante
   → handoff(payment_validation): entra al pipeline "Gestión Postventa"
   → is_ai_active = false  (agente silenciado)

─── VALIDACIÓN + ENTREGA  (automática; detalle en FLUJO_PAGO_Y_EVENTOS.md) ──
comprobante → (el turno que derivó re-encola la validación: la card recién ahora espera, #290)
   → visión (extrae) → checks determinísticos (deciden)
   PASA  → "Pago validado" → entrega ("pago confirmado" + entrada QR + fecha/hora/lugar, o links)
           → "Entregado" → "Cerrado" (won) + queda pendiente de conciliación humana contra el banco
   FALLA → queda en "Por validar pago" con los datos extraídos y su semáforo;
           el lead recibe UN mensaje neutro; el operador valida (1 click) o con nota
   BLOQUEO (sin evento/modalidad/links) → el lead recibe "pago confirmado, en un momento los
           detalles" una sola vez; la card queda con su aviso y un humano termina la entrega

─── DÍA DEL EVENTO ─────────────────────────────────────────────
QR de entrada escaneado → verde con nombre/curso, o el motivo del rechazo
   (ya usada · de otro evento · revocada · es el QR de pago)
─── POST-EVENTO (Fase 2) ────────────────────────────────────────
cron post-fecha: no-show → "No asistió" → +1 día → "Lost" (+ resumen en card, estilo Firefly)
```

### Mapa "si el lead dice X → Y"

| Lead dice / intención | Detección | Tool / acción | Respuesta (estilo) | Funnel |
|---|---|---|---|---|
| 1er mensaje / "hola" | regla | saludo | "Hola! Soy el asistente de Mirko 👋 te interesa el curso de edición y producción?" | new→engaging |
| "de qué trata / qué enseña" | regla/clasif | `get_oferta` | resumen corto + diferenciador | engaging |
| acepta / quiere info de un servicio puntual | regla/clasif | `enviar_material(slug)` → envía el PDF del servicio | "Te paso la info 🙌" + continúa hacia la inscripción (no deriva) | engaging→qualifying |
| "cuánto cuesta" | regla | `get_oferta` (precio) | "Son 480 Bs 🙂" | engaging |
| "dónde / cuándo / qué ciudad" | regla/clasif | `get_oferta` (ciudades+fechas) | lista ciudades/fechas y pregunta cuál le queda | qualifying |
| FAQ: ubicación / cupo / qué llevar | regla/clasif | `consultar_faq` (estático) | respuesta corta | sin cambio |
| "quiero inscribirme / cómo pago" (cierre `pago_qr`) | regla/clasif | pide el nombre completo → `guardar_nombre(nombre)` → `set_lead_stage(qualified)` + `enviar_qr_pago` → al mandar comprobante `handoff(payment_validation)` | "Pasame tu nombre completo para la entrada 🙌" → "Buenísimo! Acá está el QR. Cuando pagues, mandame el comprobante" | qualifying→qualified→handed_off |
| "quiero hablar con alguien" | regla | `handoff(explicit_request)` | "Dale, te conecto con Mirko" | →handed_off |
| fuera de Bolivia / otra moneda | clasif | `out_of_scope` / disqualify | respuesta fija amable | →disqualified |
| agente no entiende / falla (loop sin converger **o el proveedor de IA falla**: outage, credenciales, config — server#288) | fallback | `handoff(agent_error)` + evento `role: system` en el hilo | "Te paso con alguien del equipo para ayudarte mejor" (0 IA) | →handed_off |

**Funnel (FSM):** `new → engaging → qualifying → qualified → handed_off` (+ `disqualified`). El LLM propone, el código valida. `new→engaging` lo emite el código en dos lugares: el saludo enlatado (fila 1 del mapa) **y la entrada al flujo generativo** (#310) — un primer mensaje con intención ("hablo por el curso X") saltea el saludo por precedencia del router, y "Enganchando" significa *hay conversación en curso*, no *corrió el saludo*; sin esto la card quedaba clavada en "Nuevo" hasta el comprobante. En el turno generativo el reply que ve el lead es **solo el texto de la iteración terminal sin tools** (#311→#313): el texto que acompaña a un `tool_use` es narración interna y **nunca se envía ni se persiste** (queda solo como contexto del loop) — en prod llegó a salir "Veo que el servicio tiene flujo_cierre 'handoff_consultivo'. Procedo a guardar el nombre y luego derivar" pese a que la persona lo prohíbe: el prompt reduce la narración, el runtime la filtra. Si el loop corta por handoff, el reply queda vacío a propósito y cae al fallback determinístico del motivo (HANDOFF_REPLY / pedido de comprobante #89; `unknown_service` conserva su silencio de #94 aunque el modelo narre). Si la iteración terminal viene muda, una llamada extra **sin tools** pide solo el mensaje para el lead (best-effort, como el anti-repetición #88): el lead nunca queda sin respuesta ni recibe narración.
**Handoff** → pipeline "Gestión Postventa", `motivo ∈ {explicit_request, payment_validation, agent_error}` → `handoff_event` + `is_ai_active=false` + resumen Sonnet. **Un turno que deriva nunca adjunta el QR de pago (#315):** si el modelo encoló `enviar_qr_pago` en el mismo turno del handoff (pasó en prod con el comprobante: el acuse "Recibí tu comprobante!" salió como caption del QR reenviado, leyéndose como "pagá de nuevo"), la imagen se descarta y el acuse/despedida determinístico sale como texto suelto; el reenvío legítimo del QR no termina en handoff y no cambia. **Fallo del proveedor de IA (server#288):** los adapters traducen las excepciones del SDK a `LLMError(provider, category ∈ {auth, rate_limit, bad_request, provider, network, internal})`; el orquestador lo atrapa (router o loop), persiste un evento `role: system` (`kind: agent_error`, `category`) en `ai_chat_histories` — fuera de la ventana del LLM, visible en el hilo del CRM como chip de error — y cae al mismo `handoff(agent_error)` determinístico: el lead recibe el mensaje de derivación (nunca silencio), la card entra a Gestión Postventa con la alerta `agent_error` y `is_ai_active=false` **hasta que el staff la reactive** cuando el proveedor se recupere. Errores no-LLM del turno (DB, envío a Meta) también dejan el evento de sistema (`category ∈ {internal, delivery}`) + SSE `agent_error`, sin tocar el estado. `temperature` solo viaja a modelos que la aceptan (`model_supports_temperature`, catálogo + familias ≤4.6); ante un 400 por temperature se reintenta sin ella. **Todo cierre legítimo deriva siempre a Gestión Postventa** (#84): la derivación explícita silencia la IA aún si el funnel ya es terminal (con `is_ai_active=false` la card cae al pipeline humano sin importar el funnel); el `out_of_scope`/descalificado es el único cierre que NO va a humano (cierre amable en IA > Descalificado, decisión de negocio).
**Stages de "Gestión Postventa"** (ordenadas; el pipeline se llamaba "Gestión Humana" hasta el rename de 2026-08-30, migración 0035 — el `kind` sigue siendo `'human'`): **Por atender** → Por validar pago → Pago validado → **Entregado** → Cerrado, más **Perdido** (terminal `lost`). El rename de "Entrada enviada" a "Entregado" y el stage "Perdido" son de la iniciativa de pago/eventos — ver [FLUJO_PAGO_Y_EVENTOS.md](FLUJO_PAGO_Y_EVENTOS.md). El **motivo del handoff decide la stage de entrada**: `payment_validation` (mandó comprobante) → **Por validar pago**; `explicit_request` / `agent_error` y cualquier takeover sin motivo → **Por atender** (intake genérico — pedir humano ≠ validar pago). Detalle del ruteo motivo→stage: implementado en PR #54 (ver BITACORA).
**Cierre (won):** humano valida pago → `/generarEntrada` → QR entrada + "te esperamos" → stage "Cerrado". El nombre que el lead dio al calificar queda en `conversation.full_name` (vía `guardar_nombre`, #91); el hook 'won' crea el contacto con ese nombre.

## 2. Los QRs

| QR | Quién lo envía | MVP |
|---|---|---|
| Pago 1 (bancario) | agente | ✅ |
| Pago 2 (bancario) | — | Fase 2 (depende del recordatorio HSM de Meta) |
| Entrada (token opaco `uuid4`; lo que se muestra en la puerta se lee de la DB, el QR no lleva datos del lead) | el sistema al validarse el pago, o el operador desde la card | genera+envía+**escanea** ✅ (ver [FLUJO_PAGO_Y_EVENTOS.md](FLUJO_PAGO_Y_EVENTOS.md)) |

**Pago único.** La validación ya **no es manual**: los checks la deciden y el humano concilia después (§5 de [FLUJO_PAGO_Y_EVENTOS.md](FLUJO_PAGO_Y_EVENTOS.md)). El pago 2 + su recordatorio siguen en Fase 2 (ventana 24h de Meta → plantilla HSM, #267).

## 3. Persona / estilo

- Se presenta como **"asistente de Mirko"** (natural, que no parezca bot; no se hace pasar por humano).
- **Voseo**, tono **informal, cercano y cálido**, respuestas **cortas y puntuales**.
- **Oferta por categorías (catálogo multi-oferta):** en el saludo ofrece las **categorías** (no la lista plana de servicios); al elegir una categoría lista sus servicios; al elegir un servicio manda su material (`enviar_material`). Ante una intención amplia ("qué más tienen", "cuáles son tus servicios" — interpreta la intención, no la frase literal) lista **todas** las categorías y pide que elija; no repite el mismo servicio. Mapea lo que dice el lead al servicio/categoría más parecido aunque no use el nombre exacto.
- **Calificación previa / menos texto (#185):** no vuelca toda la info de entrada. Al elegir una categoría con **varios** servicios, primero los nombra corto (solo nombres) y hace **una** pregunta breve para segmentar (qué busca puntualmente, para qué lo necesita); recién con esa respuesta da precio/detalle del que encaja. Menos texto inicial, lead mejor segmentado (encaja en `engaging → qualifying`, discovery de §2 de [DESIGN_AGENT_SALES_FLOW.md](DESIGN_AGENT_SALES_FLOW.md)).
- **Emojis pocos** y contextuales (ubicación, ok de pago, gracias al cerrar).
- **Regla dura:** no usar signos de apertura `¡` `¿` (escribe "Hola!", "Qué ciudad te queda?"). Backstop determinístico a la salida en `to_whatsapp_text` (los quita aunque el modelo los filtre).
- De cara al lead, el handoff dice que se lo conecta con **Mirko** (decisión 2026-06-28); operativamente lo atiende su equipo (su hermana), pero al lead no se le aclara eso.
- Solo **Bolivia**, pago en **Bs**.
- **Fuera del negocio (#93):** ante una consulta ajena a los servicios de Mirko (charla, temas random), **aclara que el canal es solo para los servicios de Mirko y reconduce — NO deriva**. Solo deriva si le piden un servicio de Mirko que no figura en el catálogo o no puede ayudar (el silencio + etiqueta de alerta de #94 queda pendiente).
- **Guardrail de seguridad (A7, always-on, NO editable en /crm):** ante condiciones fuera de su config (descuentos o promesas que no figuran), presión, o impersonación (alguien que dice ser Mirko/staff/autoridad), no inventa ni cede; deriva al equipo. Se inyecta por código (`runtime_context`) por encima de la persona, así no se borra editando el prompt.

## 4. Config del agente (ABM en DB, admin-only)

El prompt y los parámetros NO van hardcodeados — viven en el esquema `agents` y se editan desde `/crm`:

| Parámetro | Dónde | UI |
|---|---|---|
| System prompt / persona | `agent.system_prompt` | textarea |
| Nivel de emojis (mucho/poco/nada) | `agent.config` jsonb | switch 3 opciones |
| Temperatura | `agent.config` jsonb | slider |
| Historial + rollback | `agent_version` | — |

**Hot-reload:** el agente lee la config vigente en cada turno → editar en la UI cambia el comportamiento sin deploy.
**Capa always-on (código, NO editable en /crm):** `runtime_context` antepone a la persona la **fecha actual** (A4) y el **guardrail anti-inyección** (A7) en cada turno; no dependen del row editable (son garantías de seguridad/correctitud).
**Roles (actualizado 2026-06-06):** editar la config del agente es **solo del `platform_operator`** (Natalia + equipo, p. ej. Chris). **Mirko (`client_admin`) y su staff NO** la ven ni la editan — solo operan inbox + tablero CRM. Modelo RBAC de 3 niveles en [SPECS_MVP.md](SPECS_MVP.md) §"RBAC". (Anula el "solo admin (Mirko)" anterior.)

## 5. Fuera del MVP → Fase 2

Pago 2 + recordatorio (plantilla HSM Meta, #267) · cron no-asistió→lost + resumen en card · pasarela de pago.

**Ya implementado** (2026-08-23, ver [FLUJO_PAGO_Y_EVENTOS.md](FLUJO_PAGO_Y_EVENTOS.md)): la **validación del comprobante por visión** —con Haiku 4.5, no un OCR dedicado: la tarea no es transcribir sino leer campos de layouts distintos, y el mismo runtime multi-provider que ya existía lo resuelve sin sumar una dependencia—, la **entrega automática** (entrada QR para presencial, links para virtual), la **conciliación humana** de los pagos auto-aprobados, el **catálogo de eventos** y el **escaneo de la entrada** en la puerta.

## 6. Decisiones de negocio abiertas

1. Criterios finos de calificación. 2. Catálogo del curso (contenido para `get_oferta` + FAQ). 3. Nombre del curso (placeholder hoy). 4. Persona/tono fino.
