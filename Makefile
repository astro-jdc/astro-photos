PY312 ?= /usr/bin/python3.12
COMPOSE ?= podman-compose

.DEFAULT_GOAL := help
.PHONY: help setup setup-backend setup-models setup-infra setup-frontend \
        dev up down logs migrate seed test test-backend test-models test-frontend \
        e2e lint fmt synth clean test-cross test-all

help: ## Muestra esta ayuda
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	 awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: setup-backend setup-models setup-infra setup-frontend ## Instala todo

setup-backend:
	$(PY312) -m venv backend/.venv
	backend/.venv/bin/pip install -q -U pip
	backend/.venv/bin/pip install -q -e "backend[dev]"

setup-models:
	$(PY312) -m venv models/.venv
	models/.venv/bin/pip install -q -U pip
	models/.venv/bin/pip install -q -e "models[dev]"

setup-infra:
	$(PY312) -m venv infra/.venv
	infra/.venv/bin/pip install -q -U pip
	infra/.venv/bin/pip install -q -r infra/requirements.txt

setup-frontend:
	cd frontend && pnpm install

up: ## Levanta postgis + minio + elasticmq
	$(COMPOSE) -f docker-compose.dev.yml up -d

down: ## Para los servicios locales
	$(COMPOSE) -f docker-compose.dev.yml down

logs:
	$(COMPOSE) -f docker-compose.dev.yml logs -f

dev: up migrate ## Levanta todo el entorno de desarrollo
	@echo "backend  -> http://localhost:8000/docs"
	@echo "frontend -> http://localhost:3000"
	@echo "minio    -> http://localhost:9001"
	@( cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000 & \
	   cd frontend && pnpm dev )

migrate: ## Aplica las migraciones de Alembic
	cd backend && .venv/bin/alembic upgrade head

seed: ## Datos sintéticos de desarrollo
	backend/.venv/bin/python scripts/seed_dev.py

test: test-backend test-models test-frontend ## Todos los tests por componente

test-backend:
	backend/.venv/bin/pytest backend/tests -q

test-models:
	models/.venv/bin/pytest models/tests -q

test-frontend:
	cd frontend && pnpm test

test-cross: ## Tests transversales (contrato, integración e invariantes). Necesita `make up` + backend en marcha
	backend/.venv/bin/pytest tests -q

test-all: test test-cross ## Todo, incluidos los transversales

e2e: ## Playwright contra el stack local
	cd frontend && pnpm exec playwright test

lint: ## ruff + mypy + eslint
	backend/.venv/bin/ruff check backend
	backend/.venv/bin/ruff format --check backend
	backend/.venv/bin/mypy backend/app
	models/.venv/bin/ruff check models
	cd frontend && pnpm lint && pnpm typecheck

fmt: ## Formatea todo
	backend/.venv/bin/ruff format backend
	models/.venv/bin/ruff format models
	cd frontend && pnpm format

synth: ## cdk synth de staging
	cd infra && ../infra/.venv/bin/cdk synth -c env=staging > /dev/null && echo "cdk synth OK"

clean:
	rm -rf backend/.venv models/.venv infra/.venv frontend/node_modules \
	       infra/cdk.out .pytest_cache
