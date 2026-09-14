# Spec — El bot consume el catálogo + envía materiales (FASE 2)

> **Estado:** ✅ **Completado**. **Fase 2** — **NO implementar hasta validar al retomar**, y después de Fase 1. Ver [HANDOFF_catalogo_materiales.md](HANDOFF_catalogo_materiales.md). **Depende de [SPEC_admin_catalogo_kb.md](SPEC_admin_catalogo_kb.md) (Fase 1)**: la data del catálogo y los PDFs los carga el operador desde la UI **primero**; acá el bot los **consume**. Ref: [FLUJO_AGENTE.md](FLUJO_AGENTE.md) §1–§4, [SPECS_MVP.md](SPECS_MVP.md), [DESIGN_AGENT_ARCHITECTURE.md](DESIGN_AGENT_ARCHITECTURE.md). Material fuente: `material 20-06-2026`.
>
> **Origen:** Mirko actualizó su portafolio (2026-06-20). Ya **no es "un curso"** sino un **catálogo**. El bot resume breve, manda el PDF al elegir, el QR al decidir, despeja dudas con el catálogo y deriva a humano si lo piden.

## 1. Intent — flujo del bot (simple y uniforme)

```
lead pregunta qué hay
  → bot da un RESUMEN BREVE del catálogo (poco texto)
lead elige una oferta
  → bot le ENVÍA EL PDF de esa oferta
lead se decide
  → bot le manda el QR DE PAGO + "pagá y mandame el comprobante"  (= flujo curso actual)
  → set qualified → al llegar el comprobante: handoff(payment_validation)
lead tiene dudas
  → el bot las DESPEJA según lo que dice el catálogo (no inventa)
lead quiere hablar con alguien
  → DERIVACIÓN HUMANA (handoff explicit_request)
```

- **Poco texto:** respuestas cortas; el detalle va en el PDF, no en el chat.
- **Cierre uniforme = `pago_qr`** (QR al decidir) para todas las ofertas por defecto. Una oferta puede marcarse `handoff_consultivo` desde la UI (Fase 1) si negocio prefiere no mandar QR (p. ej. alto ticket → solo PDF + derivar).
- **Dudas** = grounded en el catálogo (resumen + detalle de cada oferta cargado en Fase 1).
- La data (textos, precios, PDFs) **la carga el operador desde la UI** (Fase 1), no la hardcodeamos.

## 2. Material analizado (resumen + inconsistencias)

### 2.1 Abstract de cada archivo

| Archivo | De qué trata (resumen para el bot) | Precio | Mapea a |
|---|---|---|---|
| **Portafolio_Cursos_Mirko_Calzadilla_Actualizado.pdf** | Índice maestro. 3 ofertas: (1) Curso Gral. de Creación de Contenido y Edición; (2) Capacitación Personalizada 1 a 1; (3) Marca Personal de Alto Impacto | — | índice / selección |
| **Paquetes de Producción Audiovisual.pdf** (20-jun, deck premium) | Servicios de producción y edición: producción audiovisual 2 niveles (Estándar/Premium), edición mensual (CapCut/After Effects), servicios individuales (video, cinematográfico IA, ediciones IA), y plan estrella Marca de Alto Impacto | varios (Bs) | catálogo de **servicios** |
| **Marcas Premium.pdf** (12 slides) | Deck completo del programa **Marca de Alto Impacto**: filosofía, para quién, proceso, entregables mensuales, roadmap 12 meses, equipo/fierro, inversión | **$1.800/mes, mín. 3 meses** | detalle de **Marca de Alto Impacto** |
| **Paquetes Exclusivos MC ….pdf** (30-may, B/N) | Versión **vieja** del deck de producción/edición. Precios de edición **distintos** a los del 20-jun | (desactualizado) | **descartar** (ver 2.2) |
| **qr-pagos.jpeg** | QR de pago bancario | — | ya usado en flujo de pago |

### 2.2 Catálogo canónico (DECIDIDO 2026-06-20)

Clasificado en **3 categorías + 1 programa general**. Descripción base del **Portafolio**; detalles del deck respectivo.

**A — Formación**

| Oferta | slug | Resumen corto | Inversión | Cierre | PDF |
|---|---|---|---|---|---|
| Curso de Creación de Contenido y Edición | `curso-contenido-edicion` | Curso híbrido en 4 módulos: producción audiovisual (presencial), CapCut básico y avanzado, e IA aplicada al contenido (virtual por Zoom) | 650 Bs | `pago_qr` | Portafolio |
| Capacitación Personalizada 1 a 1 | `capacitacion-1a1` | Programa a medida por rubro (producción, edición, estrategia, marketing, marca personal, IA) | 1.200 USD (tipo de cambio paralelo) | `handoff_consultivo` | Portafolio |

**B — Producción Audiovisual** (PDF: Paquetes de Producción Audiovisual)

| Oferta | slug | Resumen corto | Inversión | Cierre |
|---|---|---|---|---|
| Producción Estándar | `produccion-estandar` | Rodaje cámara Sony a6400, edición CapCut. 5 videos + 12 fotos, 2 días | 5.500 Bs | `handoff_consultivo` |
| Producción Premium / Cine | `produccion-premium` | Rodaje cine (Sony FX3/A7 IV, gimbal, drone), edición AE/Premiere/DaVinci + IA. 5 videos + 12 fotos, 2 días | 9.500 Bs | `handoff_consultivo` |

**C — Edición de videos** (PDF: Paquetes de Producción Audiovisual)

| Oferta | slug | Resumen corto | Inversión | Cierre |
|---|---|---|---|---|
| Edición mensual CapCut | `edicion-capcut` | Edición accesible: 5 o 10 videos/mes | 1.800 / 3.000 Bs | `handoff_consultivo` |
| Edición mensual After Effects | `edicion-ae` | Edición premium (AE/Premiere/DaVinci): 5 o 10 videos/mes; +150 Bs/video con IA | 3.700 / 6.000 Bs | `handoff_consultivo` |
| Servicios individuales | `servicios-individuales` | Piezas sueltas: video individual, cinematográfico con IA, ediciones IA | desde 3.800 / 2.800 / 450 Bs | `handoff_consultivo` |

**G — Programa general (estrella)** (PDF: Marcas Premium)

| Oferta | slug | Resumen corto | Inversión | Cierre |
|---|---|---|---|---|
| Marca de Alto Impacto | `marca-alto-impacto` | Programa integral mes a mes con equipo de cine: estrategia, producción, edición, fotografía, manejo de redes y reportes. Engloba producción + edición | **$1.800 USD/mes · contrato mín. 3 meses** (tipo de cambio paralelo) | `handoff_consultivo` |

### 2.3 Decisiones de origen aplicadas

1. **Marca de Alto Impacto — precio canónico = $1.800 USD/mes, contrato mín. 3 meses** (de Marcas Premium). La "1.200 USD" del Portafolio queda **superada**. Descripción base del Portafolio + detalle de Marcas Premium. Nombre unificado: **"Marca de Alto Impacto"**.
2. **Catálogo canónico = el deck "Paquetes de Producción Audiovisual"** (producción + edición + individuales) + curso y capacitación del Portafolio + el programa estrella. Es el catálogo completo, no solo las 3 del portafolio.
3. **"Paquetes Exclusivos MC" (30-may) DESCARTADO** (precios de edición viejos 7.000/12.000). Manda el de junio.
4. **Capacitación 1 a 1:** sin deck propio → el bot envía el **Portafolio** y deriva (consultivo).
5. **Moneda USD:** el bot **sí** menciona que es **"al tipo de cambio paralelo (variable actual)"** como referencia; aclara que el equipo confirma el monto exacto en Bs. No es un precio fijo en Bs.

## 3. Estado actual (de qué partimos) — verificado en código

- **Oferta = una sola, inline en `Agent.config` JSONB.** Hoy `config.ofertas = {"cursos": {descripcion, precio, ciudades, fechas}}` (seed `scripts/seed_mirko.py`). El tool **`get_oferta(linea)`** ([agent_tools.py:21](../src/server/modules/agent/domain/agent_tools.py)) lee `config["ofertas"][linea]`; enum `linea ∈ {cursos, servicios}` pero solo `cursos` está poblado. `consultar_faq` es estático (keyword match sobre `config["faq"]`).
- **Tools = `ToolDefinition` puro** ([tools.py](../src/server/modules/agent/domain/tools.py)): handler `(ctx, input) -> dict`. **No producen I/O**: la `AgentService` persiste/envía los side-effects. El loop ([agent_service.py:167](../src/server/modules/agent/services/agent_service.py) `_run_tools`) **ya inspecciona** el resultado para `etapa` (avanza FSM) y `handoff_to_human` (silencia el agente). **Este es el punto de extensión natural** para "enviar documento".
- **Envío de media: la plomería YA existe pero el agente no la usa.** `WhatsAppSender` ([whatsapp_service.py:50](../src/server/modules/agent/services/whatsapp_service.py)) tiene **`send_document(to, link, filename, caption)`** y `send_image(...)` — **nunca se llaman desde el agente**. El **puerto `MessageSender`** ([ports.py:60](../src/server/modules/agent/domain/ports.py)) hoy **solo expone `send_text`**, y `AgentService.process_message` solo manda texto post-turno ([agent_service.py:110](../src/server/modules/agent/services/agent_service.py)).
- **Hosting de archivos: el mismo patrón que el QR** (`EntryService._send_qr_whatsapp`, [entry_service.py:82](../src/server/modules/crm/services/entry_service.py)): guarda en `settings.media_root` y manda `link = f"{media_base_url}/media/{ref}"` (servido estático). **Ojo (UAT 2026-08-30, #297): Meta acepta el envío con 2xx y valida el media DESPUÉS, al descargarlo** — un archivo que no cumpla sus requisitos (imágenes: "8-bit, RGB or RGBA"; tamaños máximos por tipo) muere asíncrono y sin error visible. El mount servía perfecto y aún así la entrada nunca llegó, porque el PNG era 1-bit. Antes de mandar un tipo de archivo nuevo por link, verificar sus requisitos en la doc de media del Cloud API; el camino robusto es upload + `id` (#300), que valida en el upload.
- **Multi-producto: el schema lo soporta, el deploy no lo usa.** `product → agent_template → agent` existe ([agents.schema.sql](reference/agents.schema.sql)), pero solo hay **un** producto seedeado (`cursos-mirko`). `Agent.config` es JSONB libre → admite un catálogo embebido.
- **Edición de config: ya hay camino auditado.** `PUT /agents/{id}` ([config_router.py](../src/server/modules/agent/api/config_router.py)) reemplaza `config` y crea un `agent_version` (snapshot + rollback). **Hot-reload**: el agente lee la config vigente en cada turno.
- **KB/RAG (`kb_chunk` + pgvector): tabla e índice HNSW existen, pero SIN repo, SIN ingesta, SIN query.** Diferido explícitamente. **No** se activa en esta spec.
- **Saludo hardcodeado:** `GREETING_REPLY` en [agent_service.py:38](../src/server/modules/agent/services/agent_service.py) dice "...te interesa el curso de edición y producción?" — **acoplado a un solo curso**; hay que generalizarlo.

## 4. Brecha (qué falta)

| # | Falta | Dónde |
|---|---|---|
| B1 | Estructura de datos para **N ofertas** con su PDF | `Agent.config.ofertas` (hoy 1 oferta sin PDF) |
| B2 | Tool para **listar catálogo** y tool para **enviar el material** de una oferta | `tool_catalogue.py` + `agent_tools.py` |
| B3 | Que un tool pueda **disparar `send_document`** | `_run_tools`/`_Outcome` + puerto `MessageSender` |
| B4 | **PDFs hosteados** en URL pública | `media_root/catalogo/...` + script de carga |
| B5 | **Prompt/persona/saludo** de catálogo (no "el curso") + cierre por tipo de oferta | `system_prompt` (config) + `GREETING_REPLY` + FLUJO_AGENTE |
| B6 | Manejo de **moneda USD** sin cotizar paralelo | prompt + datos de la oferta |

## 5. Diseño propuesto (recomendación del arquitecto)

### 5.1 Origen de los datos — snapshot publicado desde las tablas (Fase 1)

La **fuente de verdad** del catálogo son las tablas **`offer`/`asset`** que carga el operador desde la UI (Fase 1). Al **Publicar**, Fase 1 proyecta el catálogo activo a un **snapshot** que el agente lee en runtime (conserva hot-reload + versionado ya probados). El bot **no** lee la tabla en cada turno: lee el snapshot.

**Forma del snapshot que consume el bot** (`config.ofertas`, proyectado desde `offer`/`asset`):

```jsonc
{
  "ofertas": [
    {
      "slug": "curso-contenido-edicion",
      "nombre": "Curso de Creación de Contenido y Edición",
      "categoria": "formacion",              // formacion | produccion | edicion | general
      "resumen": "Curso híbrido en 4 módulos: producción audiovisual, CapCut básico/avanzado e IA aplicada al contenido.",
      "detalle": "...",                       // datos extra para despejar dudas (opcional)
      "precio": "650 Bs",
      "moneda": "BOB",                        // BOB | USD
      "material_url": "https://<dominio>/media/catalogo/<org>/curso-contenido-edicion.pdf",
      "material_filename": "Curso de Creación de Contenido - Mirko Calzadilla.pdf",
      "flujo_cierre": "pago_qr"              // pago_qr (default) | handoff_consultivo
    }
    // ... una entrada por oferta activa (§2.2)
  ]
}
```

- El bot lee este snapshot tal como hoy lee `config` → los tools no cambian de mecánica, solo de contenido.
- `moneda=USD` → el prompt da el valor en USD "al tipo de cambio paralelo (variable)" y deriva para el monto exacto en Bs (§2.3 #5).
- `flujo_cierre` lo setea el operador por oferta en la UI; default `pago_qr`.

### 5.2 Tools

| Tool | Cambio | Input | Devuelve |
|---|---|---|---|
| `listar_catalogo` | **nuevo** | — | `{ofertas: [{slug, nombre, tipo, resumen, precio}]}` (sin URLs) — para "qué ofrecés / qué servicios tenés" |
| `get_oferta` | **modificar**: pasa de `linea` enum a `slug` | `{slug}` | detalle de **una** oferta (precio, modalidad, resumen) — sin URL |
| `enviar_material` | **nuevo** | `{slug}` | side-effect: `{ok, enviar_documento: {url, filename, caption}}` — handler **puro**, solo arma el descriptor desde la config |

- `get_oferta`/`enviar_material` validan `slug` contra `config.ofertas` y devuelven `{encontrado: false}` si no existe (mismo patrón que hoy). El prompt lleva el resumen del catálogo, así el LLM conoce los slugs.
- `enviar_material` **no** hace I/O: igual que el resto, devuelve el descriptor y la `AgentService` ejecuta el envío (principio "tools puros, el servicio aplica side-effects").

### 5.3 Loop + puerto (envío del documento)

- `_run_tools` detecta `enviar_documento` en el resultado (igual que ya detecta `etapa`/`motivo`) y acumula los envíos pendientes.
- `_Outcome` suma un campo `documents: tuple[DocSend, ...]`.
- `process_message`: tras `send_text(reply)`, itera y llama `self._sender.send_document(...)` por cada documento. **Orden:** primero el texto (encuadre del LLM), luego el/los PDF.
- **Puerto `MessageSender`**: agregar `async def send_document(to, link, filename, caption) -> None`. `WhatsAppSender` ya lo implementa; el **stub de consola** (smoke M4) y cualquier fake de test deben sumar el método.
- **Ventana 24h:** `send_document` lanza `OutsideWindowError` igual que el QR; capturar y loguear (no romper el turno), como en `entry_service`.
- **Anti-spam:** enviar el material **una sola vez por oferta y conversación** (no re-mandar el mismo PDF si el lead vuelve a preguntar). Guardia simple por historial/estado — a precisar en build.

### 5.4 Hosting de los PDFs

- Reusar `media_root` + servido estático (igual que el QR). Ruta: `media_root/catalogo/{org_id}/{slug}.pdf` → URL `{media_base_url}/media/catalogo/{org_id}/{slug}.pdf`.
- **Carga:** script idempotente (estilo `seed_mirko`) que copia los PDFs acordados a `media_root/catalogo/{org}/` y deja la config apuntando a esas URLs. (Renombrar a slugs ASCII — los nombres actuales tienen acentos/espacios/elipsis que rompen rutas.)
- **Prod:** `media_base_url` debe ser el dominio público HTTPS (hoy `http://localhost:8000` en default — en prod ya está el dominio real, lo usa el QR).
- **Privacidad:** los PDF son **material de marketing público-por-URL** (aceptable). No poner nada sensible. Alternativa (subir a Meta como media-ID, privado, expira ~30 días → re-subir) queda como Fase 2 si se requiere privacidad.

### 5.5 Prompt / persona / FLUJO

- Generalizar `GREETING_REPLY` (hoy acoplado a "el curso") y el `system_prompt` a **catálogo**: el agente saluda, pregunta qué le interesa y da **resúmenes breves** (poco texto; el detalle va en el PDF).
- **Cierre uniforme:** al decidir → QR + "pagá y mandame el comprobante" → handoff `payment_validation` (= flujo actual). Para ofertas marcadas `handoff_consultivo` → enviar PDF + derivar a humano sin QR (decidir en build si se agrega motivo `sales_consult` o se reusa `explicit_request`).
- **Dudas:** el bot responde **solo con lo que dice el catálogo** (resumen + `detalle`); si no está, deriva en vez de inventar.
- **USD/paralelo (DECIDIDO):** ofertas `moneda=USD` → valor en USD "al tipo de cambio paralelo (variable actual)" como referencia; el equipo confirma el monto exacto en Bs.
- Actualizar **FLUJO_AGENTE.md** (catálogo, resumen breve, envío de material, QR al decidir, dudas-desde-catálogo, handoff) — canónico del comportamiento; **se actualiza antes de implementar**.

## 6. Alcance

### IN
1. El bot consume el **snapshot publicado** del catálogo (§5.1) — producido por Fase 1.
2. Tools `listar_catalogo`, `get_oferta(slug)`, `enviar_material(slug)` (§5.2).
3. Side-effect de envío de documento en el loop + `send_document` en el puerto `MessageSender` (§5.3).
4. Prompt/persona/saludo + FLUJO_AGENTE: resumen breve, PDF al elegir, QR al decidir, dudas-desde-catálogo, handoff (§5.5).
5. E2E del flujo completo.

### OUT (borde duro)
- **Carga/edición del catálogo + subida de PDFs + tablas `offer`/`asset`** → es **Fase 1** ([SPEC_admin_catalogo_kb.md](SPEC_admin_catalogo_kb.md)), va **antes**.
- **RAG vectorial / `kb_chunk` / embeddings / OCR** — Fase 3, diferido. Las dudas se responden con el catálogo estructurado, no con vector search.
- **Pasarela de pago / OCR de comprobante / pago 2 / recordatorio HSM** — fuera (FLUJO §5).
- No tocar auth/RBAC, CRM, ni el contrato de handoff salvo agregar (si se decide) el motivo `sales_consult`.

## 7. Criterios de aceptación (DoD)

- Lead pregunta "qué ofrecés" → `listar_catalogo` → el bot resume el catálogo acordado (no solo el curso).
- Lead pide detalle de una oferta → `get_oferta(slug)` → resumen + precio correctos (en la moneda de la oferta, sin inventar paralelo).
- Lead muestra interés en una oferta → el bot **envía el PDF correcto** por WhatsApp (`send_document`), una sola vez, con caption.
- Oferta `pago_qr` (curso) → mantiene el flujo actual (QR + handoff `payment_validation`).
- Oferta `handoff_consultivo` → envía PDF + deriva a humano (sin QR).
- `media_base_url` mal configurado / fuera de ventana 24h → se loguea, **no** rompe el turno.
- Editar el catálogo vía `PUT /agents/{id}` se refleja **sin deploy** (hot-reload) y queda versionado.
- `ruff` / `ruff format --check` / `mypy --strict` / `pytest` verdes; archivos <200 líneas, funciones <50.
- **Prueba e2e** (estructura correcta, console o WhatsApp real) del flujo "pregunta → resume → envía PDF → cierre".
- FLUJO_AGENTE.md actualizado **antes** de cerrar.

## 8. No-funcionales / constraints

- **Ruteo determinístico primero, IA solo para generar** (regla dura de costo). `listar_catalogo`/`get_oferta`/`enviar_material` son lookups baratos en config; no agregan llamadas LLM extra más allá del loop ya existente.
- **Multi-tenant:** el catálogo cuelga del `agent` (scoped por org); el `org_id` va en la ruta del PDF. `tenant_id` **nunca** entra al contexto del LLM.
- **Tamaño de PDF:** Meta limita documentos a 100 MB; los actuales (≤13 MB) entran. El de 13 MB ("Paquetes Exclusivos", viejo) probablemente se descarta (§2.2 #3).
- **Model IDs pineados**, runtime detrás de `LLMPort` — sin cambios.

## 9. Plan por capas (Fase 2 — detalle de implementación en /plan)

Precondición: **Fase 1 entregada** (tablas `offer`/`asset`, UI de carga, PDFs subidos, catálogo publicado).

1. **Tools:** `listar_catalogo`, `get_oferta(slug)`, `enviar_material(slug)` leyendo el snapshot + tests de handler.
2. **Loop + puerto:** `send_document` en `MessageSender` (+ stub), side-effect en `_run_tools`/`_Outcome`/`process_message` + tests.
3. **Comportamiento:** prompt/persona/saludo + QR al decidir + dudas-desde-catálogo + FLUJO_AGENTE.
4. **E2E:** flujo completo (resumen → PDF → QR → handoff); verificación de envío real.

Corte de PRs: (1+2) tools+plomería de envío, (3+4) comportamiento+e2e. Rama desde `main`.

## 10. Secuencia global del feature

| Fase | Qué | Doc |
|---|---|---|
| **Fase 1 (primero)** | Carga/gestión del catálogo + PDFs desde UI (tablas `offer`/`asset`, CRUD, upload, publish) | [SPEC_admin_catalogo_kb.md](SPEC_admin_catalogo_kb.md) |
| **Fase 2 (este doc)** | El bot consume el catálogo: resumen breve → PDF → QR → dudas → handoff | este doc |
| **Fase 3 (después)** | RAG vectorial (`kb_chunk` + embeddings + `consultar_kb`) + OCR para docs-imagen | spec aparte |

## 11. Decisiones de negocio — RESUELTAS (2026-06-20)

Todas las de §2.2/§2.3 quedaron cerradas (ver §2.3). Resumen:
1. ✅ Marca de Alto Impacto = **$1.800 USD/mes, mín. 3 meses** (Marcas Premium); descripción base del Portafolio.
2. ✅ Catálogo = **completo** (deck Producción Audiovisual + curso + capacitación + programa estrella), 3 categorías + 1 general.
3. ✅ "Paquetes Exclusivos MC" (30-may) **descartado**.
4. ✅ Capacitación 1 a 1 → envía Portafolio + handoff consultivo.
5. ✅ USD → el bot menciona "tipo de cambio paralelo (variable)" como referencia; el equipo confirma Bs.

**Abierta menor (técnica, no bloquea):** ¿se agrega motivo de handoff `sales_consult`, o las consultivas reusan `explicit_request`? → se decide en /plan o build.
