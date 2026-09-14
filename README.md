# Marketing Services — Backend (`server`)

Backend FastAPI para la plataforma de marketing con IA. Multi-tenant, asíncrono, con PostgreSQL + Redis.

## Stack

- **Framework:** FastAPI + Uvicorn
- **DB:** PostgreSQL 16 (asyncpg) + SQLAlchemy 2.0 (async) + Alembic
- **Cache / colas:** Redis
- **Auth:** JWT (python-jose) + bcrypt (passlib)
- **Logging:** structlog
- **Lint / typing:** Ruff + MyPy (strict)

## Requisitos

- Python **3.12** (recomendado). Si usas 3.13/3.14 puede haber wheels que aún no estén disponibles para todas las dependencias nativas.
- Docker Desktop (para Postgres + Redis locales) **o** una instancia de Postgres y Redis accesibles localmente.

## Setup local

```bash
# 1) Levantar Postgres y Redis
docker compose up -d

# 2) Instalar dependencias en modo editable
pip install -e ".[dev]"

# 3) Aplicar migraciones
alembic upgrade head

# 4) Levantar el dev server
uvicorn server.main:app --reload
```

Swagger queda en <http://localhost:8000/docs>.

## Estructura

Ver [`CLAUDE.md`](CLAUDE.md) para las convenciones del proyecto.

```
src/server/
├── main.py              # Entrypoint FastAPI
├── config.py            # Settings (pydantic-settings)
├── shared/              # Cross-cutting (DB, security, logger, exceptions)
└── modules/
    └── core/            # Tenants, Users, Auth (Fase 1)
        ├── domain/      # SQLAlchemy + Pydantic
        ├── repositories/
        ├── services/
        └── api/
```

## Migraciones

```bash
# Crear migración nueva (autogenerate)
alembic revision --autogenerate -m "mensaje"

# Aplicar
alembic upgrade head

# Revertir una
alembic downgrade -1
```

## Tests

```bash
pytest
```
