# Spec — Carga y gestión del catálogo + materiales desde UI (FASE 1)

> **Estado:** ✅ **Completado**. Ver [HANDOFF_catalogo_materiales.md](HANDOFF_catalogo_materiales.md). **Va primero** (decisión Natalia): la capacidad de **cargar/editar el catálogo y subir los PDFs desde una UI** se construye **antes** de que el bot lo consuma ([SPEC_catalogo_y_materiales.md](SPEC_catalogo_y_materiales.md) = Fase 2). Ref: [DESIGN_AGENT_ARCHITECTURE.md](DESIGN_AGENT_ARCHITECTURE.md), RBAC 3 niveles, [agents.schema.sql](reference/agents.schema.sql).
>
> **Por qué primero:** alimentar el bot y cambiar su data va a ser **muy común**. No la hardcodeamos en un seed ni se edita por `PUT` de JSON crudo: el operador la carga desde una UI. Así la data del bot la maneja negocio, no ingeniería, y queda lista para que el bot la consuma en Fase 2.

## 1. Intent

El **`platform_operator`** (Natalia/equipo) gestiona desde una UI:
1. El **catálogo de ofertas**: alta/baja/edición (nombre, resumen corto, precio, moneda, categoría, flujo de cierre, orden).
2. Los **materiales**: sube el **PDF** de cada oferta (y lo reemplaza cuando cambie).
3. Sin tocar JSON ni hacer deploy; con **versionado y rollback**.

Esto es la **knowledge base del bot por ahora**: el catálogo **estructurado y curado**. El bot responde dudas "de acuerdo a lo que dice el catálogo" leyendo esta data. El **RAG vectorial** (embeddings sobre documentos libres) **se difiere** a Fase 3 (§7).

## 2. De qué parte (verificado)

- **Web CRM ya existe** (shell Next.js en Vercel, auth + tablero) → la UI de catálogo es una pantalla nueva dentro de ese shell, no una app nueva.
- **Server modular** (FastAPI, Repository→Service→Router) con `agent`/`crm`/`core`. Auth + tenant-scoping + RBAC ya operativos.
- **Hosting de archivos probado:** `media_root` servido estático + `media_base_url` (lo usa el QR en prod). Sirve para los PDFs.
- **`kb_chunk` + pgvector existen** pero **sin uso** → quedan para Fase 3 (RAG vectorial). No se tocan ahora.
- **Catálogo canónico ya curado** en [SPEC_catalogo_y_materiales.md §2.2](SPEC_catalogo_y_materiales.md) → es la data inicial a cargar.

## 3. Alcance

### IN (Fase 1)
1. **Modelo de datos:** tablas `offer` + `asset` (§4) + migración Alembic.
2. **API CRUD** de ofertas + **subida de PDF** (multipart) → `asset` (§5).
3. **UI para el operador:** pantalla de catálogo (lista por categoría, editor, orden, activar/desactivar, subir PDF), **preview** (cómo lo verá el lead) y **publicar** (§6).
4. **Versionado + rollback** del catálogo (patrón `agent_version`).
5. **Carga inicial** del catálogo curado (§2.2 del doc Fase 2) vía la UI/seed limpio.

### OUT (borde duro)
- **El consumo por el bot** (resumir, mandar PDF, QR, dudas, handoff) → **Fase 2**, [SPEC_catalogo_y_materiales.md](SPEC_catalogo_y_materiales.md).
- **RAG vectorial** (chunk + embeddings + `consultar_kb`) y **OCR** de documentos-imagen → **Fase 3** (§7). El `kb_chunk` no se usa todavía.
- **Acceso de `client_admin`/staff** a esta UI → no (solo operador, §8).
- **Object storage** → se queda en `media_root` por ahora; `asset.storage_ref` abstrae el backend para migrar después.

## 4. Modelo de datos

Multi-tenant: todo scoped por `organization_id`. Identificadores en inglés (CLAUDE.md). Migración encadena tras el head actual.

### `offer` (nueva)

| Columna | Tipo | Notas |
|---|---|---|
| `id` | UUID PK | |
| `organization_id` | UUID FK | scope tenant |
| `agent_id` | UUID FK → `agent.id` | catálogo del agente |
| `slug` | text | único por agente (`curso-contenido-edicion`, …) |
| `nombre` | text | |
| `categoria` | text | `formacion` \| `produccion` \| `edicion` \| `general` |
| `resumen` | text | **resumen corto** que usa el bot (1–2 líneas) |
| `detalle` | text | opcional: datos extra para despejar dudas (lo que dice el catálogo) |
| `precio` | text | display ("650 Bs", "$1.800 USD/mes"); no se calcula |
| `moneda` | text | `BOB` \| `USD` |
| `flujo_cierre` | text | `pago_qr` (default) \| `handoff_consultivo` |
| `asset_id` | UUID FK → `asset.id` | PDF a enviar (nullable) |
| `orden` | int | orden de presentación |
| `is_active` | bool | baja lógica |
| `created_at` / `updated_at` | timestamptz | trigger `set_updated_at` |

### `asset` (nueva)

| Columna | Tipo | Notas |
|---|---|---|
| `id` | UUID PK | |
| `organization_id` | UUID FK | scope tenant |
| `kind` | text | `pdf` \| `image` |
| `filename` | text | nombre visible al lead (sanitizado, ASCII) |
| `storage_ref` | text | path en `media_root` (key de object storage en el futuro) |
| `public_url` | text | URL HTTPS pública (la que recibe Meta `send_document`) |
| `bytes` | int | validar ≤ 100 MB (límite Meta) |
| `created_at` | timestamptz | |

> **El agente lee de un snapshot publicado**, no de la tabla en cada turno: al **Publicar** se proyecta el catálogo activo a la config/versión del agente (conserva el hot-reload y el versionado ya probados). Decisión en §9.

## 5. API (REST, auth + tenant-scoped, solo `platform_operator`)

| Método | Ruta | Hace |
|---|---|---|
| `GET` | `/api/v1/agents/{agent_id}/offers` | lista catálogo |
| `POST` | `/api/v1/agents/{agent_id}/offers` | crea oferta |
| `PUT` | `/api/v1/offers/{offer_id}` | edita oferta |
| `DELETE` | `/api/v1/offers/{offer_id}` | baja lógica |
| `POST` | `/api/v1/assets` (multipart) | sube PDF → `asset` (valida MIME + tamaño) |
| `POST` | `/api/v1/agents/{agent_id}/catalog/publish` | proyecta catálogo activo → versión del agente |

Mantiene **Repository → Service → API Router**. Reusa el guard de auth/RBAC existente.

## 6. UI (operador)

- **Pantalla Catálogo** dentro del CRM: tabla de ofertas agrupada por categoría; editor lateral (nombre, resumen, detalle, precio/moneda, categoría, flujo de cierre, subir/reemplazar PDF); drag para `orden`; toggle activo.
- **Preview**: cómo presenta el bot el catálogo y cada oferta al lead.
- **Publicar**: aplica los cambios al agente (sin deploy) + crea versión.
- **Historial**: ver versiones y **revertir**.

## 7. Fases (sequencing global del feature)

| Fase | Qué | Doc |
|---|---|---|
| **F1 — AHORA** | Datos (`offer`/`asset`) + API CRUD + subida PDF + UI operador + carga inicial | **este doc** |
| **F2** | El bot consume el catálogo: resumen breve → PDF → QR → dudas desde catálogo → handoff | [SPEC_catalogo_y_materiales.md](SPEC_catalogo_y_materiales.md) |
| **F3** (después) | RAG vectorial: subir documentos libres → chunk + embeddings (OpenAI `text-embedding-3-small`, 1536-dim = encaja con `kb_chunk`) → `consultar_kb`; OCR para PDFs-imagen (Gemini Flash/dedicado, no Claude); object storage | spec aparte |

## 8. RBAC
Solo **`platform_operator`**. `client_admin` (Mirko) y staff **sin acceso** (igual que la config del agente hoy, FLUJO_AGENTE §4). Enforcement en el deps/guard de auth.

## 9. No-funcionales / decisiones
- **Subida:** validar MIME (`application/pdf`) y tamaño (≤100 MB). **Sanitizar el filename** (los actuales tienen acentos/espacios/elipsis → romperían rutas y el `send_document`).
- **Lectura por el agente:** snapshot proyectado al publicar (recomendado) vs lectura directa de tablas → §9 decisión: **snapshot** (menos riesgo, reusa hot-reload + versionado).
- **Storage:** `media_root` ahora; `storage_ref` abstrae para migrar a object storage sin tocar el resto.
- Multi-tenant en cada query; `tenant_id` nunca al LLM.

## 10. Criterios de aceptación (DoD)
- Operador crea/edita/da de baja una oferta desde la UI y **sube su PDF** → persiste en `offer`/`asset`.
- **Publicar** refleja el catálogo en lo que lee el agente **sin deploy**, versionado y reversible.
- Subir un archivo no-PDF o >100 MB → rechazado con mensaje claro; filename sanitizado.
- Solo `platform_operator` accede; otros roles → 403.
- Migración aplica sobre el head real y revierte; identificadores en inglés.
- `ruff` / `mypy --strict` / `pytest` verdes; archivos <200 líneas. Web: lint/build verdes.
- Catálogo curado inicial cargado.

## 11. Decisiones abiertas
1. **Snapshot proyectado vs lectura directa** por el agente (recomiendo snapshot).
2. **`flujo_cierre` por oferta:** default `pago_qr` para todas; ¿alguna oferta de alto ticket arranca como `handoff_consultivo`? (se setea por oferta en la UI; no bloquea).
