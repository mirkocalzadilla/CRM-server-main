# Handoff — Catálogo multi-oferta + envío de materiales (PENDIENTE)

> **Estado:** ⏸️ **EN PAUSA — pendiente de validar al retomar** (2026-06-20). Diseño acordado, **nada implementado**. Al retomar: validar y arrancar por Fase 1, slice 1.

## Qué es

El bot de Mirko deja de vender "un curso" y pasa a ofrecer un **catálogo**. Flujo: **resumen breve → al elegir, manda el PDF → al decidir, manda el QR de pago → dudas se aclaran con el catálogo → handoff humano si lo piden**. La data (textos, precios, PDFs) la **carga el operador desde una UI** (no hardcodeada).

## Archivos a leer, en orden

1. Este handoff.
2. [SPEC_admin_catalogo_kb.md](SPEC_admin_catalogo_kb.md) — **Fase 1 (va primero)**: UI + tablas para cargar el catálogo y subir PDFs.
3. [SPEC_catalogo_y_materiales.md](SPEC_catalogo_y_materiales.md) — **Fase 2**: el bot consume el catálogo.
4. [FLUJO_AGENTE.md](FLUJO_AGENTE.md) — comportamiento actual del agente (a actualizar en Fase 2).
5. Material fuente (PDFs de Mirko): `C:\Users\Natalia\Documents\Documentos landing Mirko\material 20-06-2026\`.

## Decisiones CERRADAS (no re-litigar)

- **Catálogo canónico:** 3 categorías (Formación, Producción, Edición) + 1 programa general (Marca de Alto Impacto). Detalle de ofertas/precios/slugs en [SPEC_catalogo_y_materiales.md §2.2](SPEC_catalogo_y_materiales.md).
- **Marca de Alto Impacto:** precio = **$1.800 USD/mes, contrato mín. 3 meses** (de Marcas Premium). Descripción base del Portafolio. La "1.200 USD" del Portafolio queda superada. Nombre unificado: "Marca de Alto Impacto".
- **Fuente del catálogo de servicios:** deck "Paquetes de Producción Audiovisual" (20-jun). **"Paquetes Exclusivos MC" (30-may) descartado** (precios viejos).
- **Capacitación 1 a 1:** sin deck propio → envía el Portafolio + handoff consultivo.
- **Moneda USD:** el bot dice "al tipo de cambio paralelo (variable actual)" como referencia; el equipo confirma el monto exacto en Bs.
- **Flujo del bot:** resumen breve (poco texto) → PDF al elegir → QR al decidir (= flujo curso actual) → dudas desde el catálogo → handoff si pide humano.
- **Cierre default = `pago_qr`** para todas; por oferta se puede marcar `handoff_consultivo` desde la UI.
- **Secuencia:** Fase 1 (carga por UI) **primero**, luego Fase 2 (bot consume), luego Fase 3 (RAG vectorial).
- **RAG vectorial diferido** a Fase 3. La "KB" por ahora = el catálogo estructurado curado.

## Pendiente de VALIDAR al retomar

- El diseño completo de ambos specs (aún no aprobado para implementar).
- **Técnica (no bloquea, decidir en /plan o build):**
  - ¿Agente lee de un **snapshot publicado** vs lectura directa de tablas? (recomendado: snapshot).
  - ¿Se agrega motivo de handoff `sales_consult`, o las consultivas reusan `explicit_request`?
  - ¿Alguna oferta de alto ticket arranca como `handoff_consultivo` en vez de `pago_qr`?
  - Object storage ahora o después (hoy `media_root`).

## Tareas pendientes (en orden, al retomar)

**Fase 1 — carga desde UI** ([SPEC_admin_catalogo_kb.md](SPEC_admin_catalogo_kb.md))
1. Tablas `offer` + `asset` + migración Alembic + repos (server). ← **arrancar acá**
2. API CRUD de ofertas + subida de PDF (multipart) + endpoint `publish` (server).
3. Carga inicial: catálogo curado (§2.2) + los 4 PDFs renombrados a slugs ASCII a `media_root/catalogo/{org}/`.
4. UI de catálogo en el CRM web (lista por categoría, editor, subir PDF, preview, publicar).

**Fase 2 — el bot consume** ([SPEC_catalogo_y_materiales.md](SPEC_catalogo_y_materiales.md))
5. Tools `listar_catalogo`, `get_oferta(slug)`, `enviar_material(slug)` leyendo el snapshot.
6. Envío de documento: `send_document` en el puerto `MessageSender` (ya existe en `WhatsAppSender`) + side-effect en `_run_tools`/`_Outcome`/`process_message`.
7. Prompt/persona/saludo (generalizar `GREETING_REPLY`) + QR al decidir + dudas-desde-catálogo + actualizar **FLUJO_AGENTE.md**.
8. E2E del flujo completo (resumen → PDF → QR → handoff).

**Fase 3 — después:** RAG vectorial (`kb_chunk` + embeddings OpenAI 1536-dim + `consultar_kb`) + OCR para PDFs-imagen.

## Hallazgos técnicos clave (verificados en código, ahorran tiempo)

- **`WhatsAppSender.send_document(to, link, filename, caption)` YA EXISTE** ([whatsapp_service.py:50](../src/server/modules/agent/services/whatsapp_service.py)) — nunca se llama. El puerto `MessageSender` ([ports.py:60](../src/server/modules/agent/domain/ports.py)) solo expone `send_text` → hay que sumarle `send_document`.
- **Hosting de PDFs = mismo patrón que el QR**: `media_root` + `{media_base_url}/media/{ref}` servido estático ([entry_service.py:82](../src/server/modules/crm/services/entry_service.py)). **Ojo**: Meta valida el media al descargarlo, después de aceptar el envío — un archivo fuera de sus requisitos muere asíncrono y sin error (#297; ver la nota en `SPEC_catalogo_y_materiales.md` §"Hosting de archivos").
- **El loop ya inspecciona resultados de tools** para `etapa`/`handoff` ([agent_service.py:167](../src/server/modules/agent/services/agent_service.py) `_run_tools`) → punto de enganche para el side-effect "enviar documento".
- **Schema ya tiene `product`/`agent_template`/`agent` + `kb_chunk`** (pgvector, índice HNSW, `vector(1536)`), sin uso runtime ([agents.schema.sql](reference/agents.schema.sql)).
- **Oferta actual:** un curso inline en `Agent.config.ofertas` (seed `scripts/seed_mirko.py`), tool `get_oferta(linea)`. `GREETING_REPLY` hardcodeado a "el curso" ([agent_service.py:38](../src/server/modules/agent/services/agent_service.py)).

## Notas

- Material fuente con nombres acentuados/elipsis rompe rutas POSIX → al cargar, renombrar a slugs ASCII.
- Regla del repo: actualizar FLUJO_AGENTE.md **antes** de implementar Fase 2 (no se commitea contra el diseño). Rama desde `main`.
