FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=on

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Dependencias desde uv.lock (server#288): la imagen corre EXACTAMENTE las versiones
# que CI testeó (`uv sync` en ci-backend.yml). Antes `pip install -e .` resolvía lo
# último de PyPI en cada build — así entró anthropic 1.x sin pasar por los tests.
# Capa propia: solo se invalida cuando cambian los pins, no con cada cambio de código.
COPY pyproject.toml uv.lock ./
RUN pip install uv \
    && uv export --frozen --no-dev --no-emit-project -o /tmp/requirements.txt \
    && pip install --no-deps -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY . /app/
RUN pip install -e . --no-deps

# SHA del commit del build (lo inyecta el CI con --build-arg). Al final, para no invalidar
# la capa de `pip install`. Se expone en /health para validar qué versión corre en prod.
ARG GIT_SHA=unknown
ENV GIT_SHA=$GIT_SHA

CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000"]
