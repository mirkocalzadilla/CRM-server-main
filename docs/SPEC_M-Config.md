# Spec — M-Config: ABM del agente + users/roles (platform_operator-only)

> **Estado:** ✅ **Completado** — ver §8. Pendiente: front (B6) y rollback (Fase 2). Owner: Natalia (server). Dependía de RBAC 3 niveles (`require_platform_operator`, mergeado). Ref: SPECS_MVP §"RBAC", FLUJO §4, esquema `agents` (M0).

## 1. Intent

Dar la superficie de **configuración** restringida a `platform_operator` (Natalia + equipo): (1) **editar el agente** (system_prompt, emojis, temperatura, modelo) generando una **nueva `agent_version`** (auditable, con rollback futuro), y (2) **ABM de users/roles**. El cliente (Mirko) **no** accede a esto. Las ofertas/resumen vienen del **Catálogo de Servicios** (#105), no del form.

## 2. Estado actual (al momento de la spec — histórico)

- Esquema `agents` (M0) ya tiene el versionado: `Agent` (`system_prompt`, `model`, `tools`, `config`, `current_version_id`) + `AgentVersion` (`version_number`, snapshot completo, `change_summary`, `created_by`, `created_at`). El seed crea el agente; **nadie edita todavía** (no hay router de config del agente).
- `core` tiene `UserService`/`TenantService` + auth; ABM parcial de users existe a nivel servicio, sin la superficie de config.
- Front (web): `app/crm/agents` y `app/crm/settings` son **placeholders WIP**.

## 3. Desglose

### A. Editar agente → `agent_version`
- `GET /api/v1/agents/{id}` — config actual (system_prompt, model, config, versión vigente).
- `PUT /api/v1/agents/{id}` — aplica cambios y **versiona**: crea `AgentVersion` nuevo (`version_number = max+1`, snapshot de system_prompt/model/tools/config, `created_by = operator`, `change_summary`), actualiza los campos vivos de `Agent` y `current_version_id`. Atómico (una transacción).
- **Qué es editable (config):** `system_prompt`, **emojis** (switch on/off → vive en `config` JSON, #104), **temperatura**, **modelo** (dropdown del catálogo, #103). **NO editable por el form (#105):** `ofertas`/`resumen` provienen del **Catálogo de Servicios** (`config.services`, escrito por `CatalogService.publish`); `faq` queda deprecado como nodo manual (la futura FAQ será RAG sobre `kb_chunk`). El update **descarta** `ofertas`/`faq`/`resumen` entrantes y **preserva** la key server-owned `services`.
  - **Sub-tarea/decisión:** la **temperatura no existe hoy** en `AnthropicAdapter`/`OpenAIAdapter` (solo `max_tokens`). Agregar `temperature` al `LLMPort.complete`/adapters + leerla del `config` del snapshot = parte de M-Config (o sub-CR). Surface.
- **Servicio** `AgentConfigService.update(agent_id, changes, operator_id)` encapsula el ciclo versionado. Repos: extender el repo de agente (cargar/actualizar + crear version).

### B. ABM users/roles
- `GET/POST/PUT/DELETE /api/v1/users` (+ asignar membresía/rol `client_admin`/`staff` por tenant). Marcar `is_superuser` (platform_operator) = acción de operador.
- Reusa `UserService`/`TenantService` de core; agrega la superficie REST faltante.

### C. RBAC
- **Todo M-Config exige `platform_operator`** (`require_platform_operator`, de la spec RBAC). `client_admin/staff` → 403.

## 4. Contratos (resumen)
- `GET /api/v1/agents/{id}` · `PUT /api/v1/agents/{id}` (→ nueva version)
- `GET/POST/PUT/DELETE /api/v1/users` + asignación de rol/membresía + flag operador

## 5. Criterios de aceptación (DoD)
- `PUT /agents/{id}` con cambios → **nueva fila `agent_version`** (version_number incremental, snapshot, created_by) + `Agent.current_version_id` actualizado + campos vivos reflejan el cambio. Idempotencia/atomicidad verificada.
- No-operador (`client_admin`/`staff`) → **403** en todo M-Config.
- ABM users/roles funciona y es tenant-consistente.
- `ruff`/`mypy`/`pytest` verdes; archivos <200; e2e consola: editar agente sube versión.

## 6. Dependencias
- **Bloqueante:** RBAC 3 niveles (sin `platform_operator` real, el gateo es nominal).
- Front (web): `app/crm/agents` + `settings` consumen estos endpoints (slice de front posterior).
- `temperature` en los adapters (sub-tarea técnica).

## 7. Decisiones ✅ cerradas

| Decisión | Resolución |
|---|---|
| `temperature` en este CR | **Sí** — agregar a `LLMPort.complete` + `AnthropicAdapter` + `OpenAIAdapter` + leer del config snapshot. |
| Editar = nueva versión inmediata | **Sí** — cada `PUT` genera una nueva `agent_version` activa. Sin draft. |
| Rollback | **Fase 2** — no implementar en MVP. |
| ABM users | **Solo cambiar rol de usuarios existentes** (`client_admin` ↔ `staff`). Sin invitación por email. |

## 8. Implementación (PR #36, 2026-06-10)

- **Superficie real** (toda con `require_platform_operator`; no-operador → 403):
  - `GET /api/v1/agents/{id}` — config viva + versión activa (`current_version`). Org-scoped: agente de otra org → 404.
  - `PUT /api/v1/agents/{id}` — body `{system_prompt?, model?, config?, change_summary?}` (al menos un cambio; `config` reemplaza el JSON completo; `config.temperature` validada 0.0–1.0) → devuelve la `AgentVersion` nueva.
  - `GET /api/v1/users` — usuarios del **tenant activo** con su rol de membresía (redefinido: antes listaba users globales sin rol).
  - `PUT /api/v1/users/{user_id}/role` — body `{"role": "client_admin"|"staff"}`; no-miembro del tenant → 404.
- **Piezas:** `agent/api/config_router.py` + `config_schemas.py` · `agent/services/agent_config_service.py` · `agent/repositories/agent_repository.py` · `core/api/user_router.py` (lista con rol + cambio de rol) · `temperature` en `llm_port.py` + ambos adapters (`omit` si None) + `State.temperature` leída en el loop generativo.
- **Validación:** pytest (`test_agent_config.py`, `test_users_roles.py`, temperature en `test_agent_service.py`) + e2e contra Postgres real: PUT ×2 → versiones 1 y 2 verificadas con psql (snapshot, `created_by`, `current_version_id`) + smoke `scripts/a3_temp_smoke.py` (temperature aceptada por el provider real).
- **Fuera de este CR:** rollback (Fase 2) · invitación de usuarios · front `app/crm/agents`/`settings` (B6, consume estos endpoints).
