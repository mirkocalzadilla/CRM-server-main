# Cómo contribuir

Flujo de PRs de este repo. Lo esencial está en la tabla; el resto son detalles.

## Flujo de un cambio

| Paso | Quién | Detalle |
|---|---|---|
| 1. Rama | autor | Sale siempre de `main` actualizado: `git fetch && git switch -c <tipo>/<desc>-<fecha> origin/main` |
| 2. PR | autor | Abrir PR contra `main`. **Crear el PR no lo mergea.** |
| 3. CI | — | Esperar el check en verde. |
| 4. Merge | **autor** | **El autor mergea su propio PR** en cuanto CI está verde. No esperes a que otro lo mergee. |
| 5. Deploy | automático | El merge a `main` dispara el deploy pull-based en el VPS (systemd timer). |

## Regla de oro

**Crear un PR ≠ mergearlo.** Un PR abierto no llega a producción hasta que **vos, el autor, le das merge.** Si lo dejás abierto, queda en silencio: nadie más está esperando para mergearlo.

> Todos los miembros son `admin` en este repo, así que cualquiera mergea su propio PR sin pedir permisos.

## Guardrail

La Action `stale-pr-reminder` comenta en tu PR si lleva más de 12 h abierto sin cambios, para que no se te olvide mergearlo.

## Convenciones

- Commits y títulos de PR: estilo conventional commits (`feat:`, `fix:`, `docs:`, `chore:`...).
- Una rama por tarea; no apilar PRs.
