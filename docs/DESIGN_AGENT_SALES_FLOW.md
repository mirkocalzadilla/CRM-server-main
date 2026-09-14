# Diseño — Agente de ventas sobre WhatsApp (calificación → handoff humano)

> **Fecha:** 2026-05-30 · **Estado:** propuesta para reaccionar (no implementado)
> **Alcance:** runtime del agente de IA que gestiona el lead entrante de WhatsApp hasta entregarlo calificado a un humano. Cruza backend (runtime + tools) y frontend (inbox/takeover).
> **Relacionados:** [ESTADO_Y_RUNBOOK.md](ESTADO_Y_RUNBOOK.md).

---

## 1. Objetivo y restricciones

Lead entra por WhatsApp → un agente de IA **acotado al dominio del curso** lo saluda, responde dudas, lo **califica**, y cuando está caliente **lo entrega a un humano** que cierra. El agente nunca cobra ni cierra.

| Restricción | Origen | Implicación |
|---|---|---|
| Agente **purpose-bound**, no chatbot general-purpose | Política Meta vigente 15-ene-2026 ([respond.io](https://respond.io/blog/whatsapp-general-purpose-chatbots-ban)) | System prompt de dominio + tools restringidas. Prohibido exponer "Claude conversacional" crudo. |
| **Cierra un humano** | Decisión negocio (Natalia, 2026-05-30) | Sin tools de pago. Acción terminal del agente = `handoff_to_human`. |
| **Califica + handoff**, no autónomo | Decisión negocio | El agente no marca venta; entrega lead calificado. |
| **MVP, pero escalable a empresas + adaptable a tech emergente** | Decisión negocio | Dos costuras reemplazables (§3). Nada de lock-in prematuro. |

> Aparte: el bloqueo *"Business is not allowed to claim App"* es de verificación de ownership en Meta Business Manager, **distinto** de la política de IA. Christian lo destraba por separado; no afecta este diseño.

## 2. Modelo de producto — funnel + handoff

State machine explícita. El LLM **conversa y propone** transiciones; el código las **valida** (el LLM no puede saltarse estados ni inventar un cierre).

| Estado | Actúa | Entra cuando | Transiciona a |
|---|---|---|---|
| `new` | agente | primer mensaje del lead | `engaging` |
| `engaging` | agente | da info del curso, responde dudas | `qualifying` / `disqualified` |
| `qualifying` | agente | hace discovery (intención, timing, fit) | `qualified` / `disqualified` |
| `qualified` | agente | cumple criterios → dispara handoff | `handed_off` |
| `handed_off` | **humano** | agente se silencia, notifica al humano + pasa resumen | (humano cierra fuera de sistema) |
| `disqualified` | — | no interesado / no fit | terminal |

**Guardrails del agente:** no promete precios fuera de catálogo, no pide datos de pago, no cierra venta. Su "cierre" es el handoff. `handed_off` **silencia el runner** (no vuelve a responder hasta que un humano lo reactive desde el inbox).

## 3. Decisiones de arquitectura — las dos costuras

El núcleo del "escalable sin overkill + adaptable": dos límites de abstracción, ambos in-process hoy, ambos swappables.

| Costura | MVP (hoy) | Interfaz | Se reemplaza por… (cuando aplique §6) |
|---|---|---|---|
| **Runtime** | Messages API + loop `tool_use` propio en FastAPI (control total, tx fina, multi-tenant scoping) | `AgentRunner` (protocolo) | Claude Agent SDK (subagentes/autonomía) sin tocar el dominio |
| **Herramientas** | function calling **in-process** detrás de un `ToolRegistry` | `Tool` (protocolo: name/schema/`run(ctx)`) | MCP server compartido tenant-scoped → factory aislada |

Por qué NO Agent SDK ni MCP ahora: 1 tenant, 1 canal, set de tools chico → el "context tax" y el descubrimiento dinámico de MCP no se pagan, y el runtime del Agent SDK agrega autonomía amplia que este funnel acotado no necesita ([Anthropic](https://www.anthropic.com/engineering/building-agents-with-the-claude-agent-sdk), [Portkey](https://portkey.ai/blog/mcp-vs-function-calling/)). La interfaz deja la puerta abierta sin costo.

## 4. Componentes a construir

```
WhatsApp ─▶ webhook (firma HMAC ✓)        [Christian — inbound]
                  │  persiste Message(user) + enqueue(conversation_id)
                  ▼  200 OK rápido a Meta
            cola (Redis / BackgroundTasks al inicio)
                  │
            AgentRunner (tool_use loop)    [propuesto: Natalia]
            ├─ system prompt de dominio (vendedor del curso, tono definido)
            ├─ historial de Conversation (scoped por tenant)
            └─ ToolRegistry:
                 • get_course_info()          (read catálogo)
                 • set_lead_stage(stage, reason)  ← valida state machine §2
                 • handoff_to_human(summary)  ← terminal: silencia + notifica
                  │
            persiste Message(assistant) + lead.stage en 1 transacción
                  │
            WhatsAppSender.send_text()       [ya existe — coordinar cableado]
```

**Frontend (Fase 1 UI, converge con [ESTADO_Y_RUNBOOK.md](ESTADO_Y_RUNBOOK.md)):** inbox de conversaciones con badge `🔥 handoff` cuando `handed_off`; el humano **toma** la conversación y responde por el thread (envío manual vía sender); toggle **IA on/off** por conversación. Esto es justo lo que hoy es placeholder en `/crm/conversations` e `/crm/inbox`.

## 5. Fuera del scope MVP (explícito)

| No entra ahora | Por qué | Vuelve cuando |
|---|---|---|
| Tools de pago / cierre autónomo | el humano cierra | nunca, salvo cambio de negocio |
| MCP / MCP factory | overkill a 1 tenant | §6 |
| Tabla `meta_integrations` (multi-tenant Meta) | Mirko usa 1 número (env var ok) | 2º cliente con su número |
| Email real de notificación de handoff | DevOps pendiente | empieza como flag en inbox + log |
| Queue robusta (arq/celery) | `BackgroundTasks` alcanza | cuando duela el volumen |

## 6. Umbrales de escala (cuándo cruzar cada uno)

| Umbral | Señal para cruzarlo | Qué se introduce |
|---|---|---|
| Runtime | querés subagentes (calificador/objeciones) o autonomía amplia | `AgentRunner` → Claude Agent SDK |
| Tools | 2º cliente reusa las mismas tools, o >~15 tools, o añadir sin deploy | `ToolRegistry` → MCP server **compartido** tenant-scoped |
| Aislamiento | tools de alto riesgo (pagos) / compliance por cliente | proceso MCP **aislado** por tenant (la "factory") ([Pravin Kumar](https://www.pravinkumar.co/blog/single-mcp-server-multi-client-webflow-2026)) |
| Credenciales Meta | 2º cliente con su propio número | tabla `meta_integrations` |

## 7. Coordinación y decisiones pendientes

**División con Christian:** él = inbound (webhook + HMAC + storage). Propuesto yo = runtime + tools + state machine + handoff + frontend inbox. **Boundary a acordar:** el webhook, tras persistir el `Message(user)`, encola `conversation_id`; el `AgentRunner` lo consume. No cablear el sender al runner sin acordar esta interfaz (ver finding #3).

**Decisiones de negocio que faltan para escribir el spec:**
1. **Criterios de calificación** — ¿qué preguntas de discovery y qué umbral hacen a un lead `qualified`? (lo necesito del negocio).
2. **Notificación de handoff** — ¿flag en inbox CRM, email, o WhatsApp directo a Mirko?
3. **Catálogo del curso** — contenido, formato, precio (para el system prompt / `get_course_info`).
4. **Persona/tono** del agente.

## 8. Plan por fases

- **F1 (MVP, Mirko):** HMAC + enqueue + `AgentRunner` (loop propio) + `ToolRegistry` (get_course_info, set_lead_stage, handoff_to_human) + state machine + flag de handoff + inbox frontend con takeover. Single-tenant por env var.
- **F2:** estado `nurturing` (lead "no ahora"), agendado, email real de handoff, tests e2e.
- **F3 (escala):** según umbrales §6.
