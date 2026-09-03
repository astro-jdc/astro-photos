# syntax=docker/dockerfile:1.7
# Imagen del backend de astro-photos. Multi-stage: el compilador y las cabeceras
# de desarrollo se quedan en la etapa de build y nunca llegan a producción.

# --------------------------------------------------------------------------- #
# Etapa 1 — build de las dependencias en un venv aislado
# --------------------------------------------------------------------------- #
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# `build-essential` solo hace falta si alguna rueda no está precompilada para
# linux/arm64; se descarta con la etapa.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Se copia primero solo el pyproject para que la capa de dependencias se cachee
# y no se reinstale con cada cambio de código.
COPY pyproject.toml README.md ./
RUN mkdir -p app && touch app/__init__.py \
 && pip install --upgrade pip setuptools wheel \
 && pip install .

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
RUN pip install --no-deps .

# --------------------------------------------------------------------------- #
# Etapa 2 — runtime mínimo
# --------------------------------------------------------------------------- #
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PATH="/opt/venv/bin:$PATH" \
    ENVIRONMENT=prod

# curl para el HEALTHCHECK; libpq no hace falta porque asyncpg no usa libpq.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 astro \
 && useradd --system --uid 10001 --gid astro --home-dir /app --shell /usr/sbin/nologin astro

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=astro:astro app ./app
COPY --chown=astro:astro migrations ./migrations
COPY --chown=astro:astro alembic.ini ./

USER astro
EXPOSE 8000

# El proceso responde a /healthz sin tocar base de datos ni S3: es liveness pura.
# La readiness (DB + S3 + cola) la consulta el ALB en /readyz.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/v1/healthz || exit 1

# Un worker por contenedor: el escalado es horizontal (ECS/Fargate), no por
# procesos dentro del contenedor.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]
