# Referencia de infraestructura

> **Actualizado 2026-06-05.** Solo información estructural — sin secretos. Credenciales, tokens y contraseñas están en el gestor de secretos del equipo (no en git).

## Frontend

| Ítem | Detalle |
|---|---|
| Plataforma | Vercel (Hobby, cuenta personal de Natalia) |
| Organización GitHub | `funnelops-marketing-services` (pública) |
| Repo | `funnelops-marketing-services/web` |
| Dominio | `mirkocalzadilla.com` — DNS gestionado en Hostinger, apuntado a Vercel |
| Deploy | Automático en push a `main` (CI verde) |
| CRM | `mirkocalzadilla.com/crm` (sin subdominio separado) |

## Backend

| Ítem | Detalle |
|---|---|
| Organización GitHub | `funnelops-marketing-services` |
| Repo | `funnelops-marketing-services/server` |
| Estado deploy | Pendiente — aún no está en producción |
| VPS | Hostinger KVM 2 · IP `2.24.101.201` · Ubuntu 24.04 |
| Subdominio futuro | `api.mirkocalzadilla.com` |
| Correo | Gestionado en Hostinger |

## Local (dev)

```
Postgres (pgvector): localhost:5433  ← env var DATABASE_URL de un proyecto externo (printshop); no borrar
Redis:               localhost:6379
API (Docker):        http://localhost:8000
Front (Next.js):     http://localhost:3000
```

> El backend **debe correr en Docker** en Windows (no con venv local): asyncpg desde el host hacia Postgres del contenedor falla con WinError 64. Ver [ESTADO_Y_RUNBOOK.md](../ESTADO_Y_RUNBOOK.md) para el runbook completo.

## WhatsApp (Meta Cloud API)

| Ítem | Detalle |
|---|---|
| Integración | Meta Cloud API (oficial) — webhook + HMAC-SHA256 + send via `graph.facebook.com` |
| Número de prueba | A definir con Mirko (el número real va en seed, no en código) |
| Ventana 24h | Aplica — mensajes proactivos fuera de ventana requieren HSM templates (Fase 2) |

## Repos

| Repo | URL |
|---|---|
| Backend | `https://github.com/funnelops-marketing-services/server` |
| Frontend | `https://github.com/funnelops-marketing-services/web` |
