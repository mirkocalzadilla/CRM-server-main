---
description: Cierre pre-commit (backend) — valida diff contra el diseño, registra en BITACORA, crea branch, pushea y abre PR a main
---

Ejecutá el cierre pre-commit de este repo (backend). Seguí los pasos EN ORDEN. No saltear ninguno.

## 1. Diff

Mostrá `git status --short`, `git diff` y `git diff --staged`. Listá los archivos modificados/nuevos/borrados.

## 2. Resumen del cambio

En 2–4 líneas: qué cambió y por qué. Incluí el módulo afectado.

## 3. Clasificación del cambio

Determiná el tipo para el nombre del branch:
- `feat/` — funcionalidad nueva (M2, M3, M4, M-Config, M5, herramientas nuevas)
- `fix/` — corrección de bug
- `docs/` — solo documentación (sin cambios de código)
- `chore/` — infraestructura, CI, dependencias, housekeeping

Formá el nombre: `<tipo>/<slug-del-cambio>-YYYYMMDD` (ej. `feat/funnel-fsm-20260605`).

## 4. Verificación contra el paradigma — STOP si hay desviación

Leé `CLAUDE.md` + `docs/SPECS_MVP.md` + `docs/DESIGN_AGENT_ARCHITECTURE.md` + `docs/FLUJO_AGENTE.md`.

Verificá que el cambio respeta **todos** estos invariantes:

| Invariante | Qué verificar |
|---|---|
| Esquema `agents` | modelos en `domain/models.py` siguen la jerarquía `product→template→agent→version→instance→conversation` |
| Orquestación hand-coded | NO hay imports de `langchain`, `langgraph`, `langchain-anthropic` |
| Runtime vía adapter propio | toda llamada LLM pasa por `LLMPort`, nunca directa a Anthropic SDK |
| Modelos pineados | `claude-haiku-4-5-20251001` en loop, `claude-sonnet-4-6` solo en `handoff_summary` |
| Tools como dataclass | `ToolDefinition` dataclass, no `dict[str, Callable]` |
| Funnel validado en código | transiciones solo vía `validate_transition()`, nunca el LLM decide directamente |
| Ruteo determinístico primero | el router corre reglas/keywords antes de llamar al LLM |
| Multi-tenant | cada query filtra por `tenant_id`/`organization_id`; `tenant_id` NO aparece en contexto LLM |
| Tamaño | archivos <200 líneas, funciones <50 líneas |
| Idioma | código en inglés; comentarios mínimos en inglés; docs en español |

**Si el cambio DESVÍA de alguno de estos puntos:**
- **DETENÉ aquí.**
- Explicá exactamente qué invariante rompe y por qué.
- Si es una mejora intencional al diseño (nueva arquitectura acordada, refactor consensuado), hay que actualizar primero el doc de diseño correspondiente y acordarlo. **No commitear contra el diseño.**
- Si es un refactor que mejora el flujo sin cambiar el paradigma: apuntalo explícitamente en la bitácora (ver paso 5) con el contexto "por qué" y "qué spec respeta".

## 5. Tests — no commitear si falla

Corré en orden:

```bash
ruff check src/ tests/
ruff format --check src/ tests/
mypy src/ --ignore-missing-imports
```

Si la pieza está integrada: `pytest` (o el subconjunto relevante). Si no está integrada: corrée las pruebas locales definidas en `docs/SPECS_MVP.md` §"Prueba local" para ese módulo.

Reportá los resultados reales (output del comando). Si algo falla: **no continuar**, corregir primero.

## 6. Bitácora

Agregá la entrada **directamente en `BITACORA.md`**, arriba de todo bajo `## Entradas` (la más nueva primero), con este formato exacto:

```
### YYYY-MM-DD · <autor> · <módulo/pieza>
- Qué cambió:
- Por qué:
- Spec/decisión que respeta:   (ref a DESIGN_* / SPECS_MVP / memoria)
- Prueba local:
- Commit:   (completar después del commit)
```

Editá `BITACORA.md` directo sin miedo a conflictos: `.gitattributes` marca `BITACORA.md merge=union`, así que si otro PR agrega una entrada en paralelo, git conserva las dos automáticamente al mergear. (Ya no se usan fragmentos en `.bitacora/` ni el bot que pusheaba a main.)

Si el cambio incluye una mejora de flujo (refactor consensuado, extensión de un módulo existente): agregá una línea extra:
```
- Mejora de flujo: <descripción — qué mejoró y por qué es coherente con el diseño>
```

## 7. Branch + commit

1. **Siempre partí de `main` actualizado — no stackees ramas:** `git checkout main` → `git pull origin main` → `git checkout -b <nombre-del-branch>` (del paso 3). Si el cambio depende de otra pieza aún no mergeada, primero se mergea esa pieza a `main`; recién después se ramifica la siguiente.
2. Stagea los archivos relevantes (**no `git add .` ciego** — revisá qué va y qué no, asegurándote de incluir `BITACORA.md` con tu entrada, y excluyendo `.env`, `__pycache__`, `*.pyc`).
3. Armá el commit. Mostrámelo **antes de ejecutarlo** para confirmación.
   - Formato: `<tipo>(<módulo>): <descripción corta en inglés>` (ej. `feat(agent): add funnel_fsm with validate_transition`)
   - Autora = la usuaria del repo. **Sin co-author.**

Esperá confirmación antes de ejecutar el commit.

## 8. Push + PR

Después de confirmar el commit:

1. `git push origin <nombre-del-branch>`
2. Crear PR a `main`:
   ```bash
   gh pr create \
     --base main \
     --title "<tipo>(<módulo>): <descripción>" \
     --body "$(cat <<'EOF'
   ## Qué cambia
   <resumen del paso 2>

   ## Módulo
   <módulo afectado>

   ## Spec que respeta
   <referencia al doc de diseño / SPECS_MVP>

   ## Tests
   <resultado de ruff/mypy/pytest del paso 5>
   EOF
   )"
   ```
3. Mostrá la URL del PR.

## 9. Completar la bitácora

Volvé a `BITACORA.md` y completá el campo `Commit:` con el hash del commit creado.

$ARGUMENTS
