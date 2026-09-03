# 0001 — Monorepo con un venv por componente

Estado: Aceptado · 2026-09-03

## Contexto

El sistema tiene cuatro componentes con ciclos de vida distintos (backend, frontend,
infraestructura, modelos) pero un único contrato compartido: el modelo de datos y la
API. Cambiar ese contrato obliga a tocar los cuatro a la vez.

Además, `models/` tiene dependencias pesadas y conflictivas con el backend: torch,
astropy, rawpy y CUDA no tienen por qué convivir con FastAPI y asyncpg.

## Decisión

Un solo repositorio, con **un entorno virtual independiente por componente Python**
(`backend/.venv`, `models/.venv`, `infra/.venv`) y `node_modules` propio en el
frontend. El `Makefile` de la raíz orquesta los cuatro.

## Consecuencias

- Un cambio de contrato es **un solo PR** que toca backend, frontend, docs y tests a
  la vez, y CI lo verifica entero. Es la razón principal de esta decisión.
- CI corre los cuatro componentes en paralelo con cachés separadas.
- Nadie instala torch para arreglar un endpoint.
- El repo es más pesado de clonar y los agentes deben respetar su territorio de
  ficheros para no pisarse; se documenta en `.claude/agents/`.

## Alternativas descartadas

- **Cuatro repos separados.** Sincronizar el contrato entre repos con paquetes
  publicados es más ceremonia de la que este proyecto puede pagar en esta fase.
- **Un único venv para todo.** El primer conflicto entre `numpy` de astropy y el de
  otra dependencia lo habría hecho insostenible.
