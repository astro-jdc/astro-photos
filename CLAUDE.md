# astro-photos — convenciones del proyecto

Repositorio colaborativo de astrofotografía: los usuarios suben tomas con metadata
rica (tiempo, GPS, óptica, licencia CC) y el sistema combina muchas tomas de muchos
observadores en una sola imagen más profunda y mejor muestreada.

## Lo primero que hay que leer

- `docs/research/multi-image-astro-reconstruction.md` — **qué es físicamente posible y
  qué no**. Fija el roadmap Tier A/B/C. Obligatorio antes de tocar `models/`.
- `docs/data-model.md`, `docs/api.md` — contratos compartidos. Si cambias uno, el PR
  toca backend, frontend y tests a la vez.
- `docs/branching.md` — `main` = producción, `develop` = staging.

## Reglas duras

1. **No prometemos lo que la física no da.** Combinar fotos de observadores separados
   **no** sintetiza una apertura (no es interferometría, no es el EHT). Ganamos SNR,
   muestreo sub-píxel, rango dinámico y dominio temporal. No ganamos resolución
   angular más allá del límite de difracción de la mejor óptica contribuyente.
   Cualquier texto de producto que sugiera lo contrario es un bug.
2. **Nada generado sin etiquetar.** En astronomía una fuente alucinada es un falso
   descubrimiento, no un defecto estético. Toda salida de un modelo aprendido lleva
   mapa de incertidumbre y etiqueta visible.
3. **Reproducibilidad bit a bit.** Misma entrada + `pipeline_version` + `params` →
   misma salida. Semillas explícitas, nada de `random` global, nada de depender del
   orden del sistema de ficheros.
4. **Procedencia siempre.** Cada foto que entra en una reconstrucción deja fila en
   `reconstruction_inputs` con su peso y la licencia vigente en ese momento.
5. **La licencia de salida es la combinación más restrictiva de las entradas**, y esa
   lógica vive en un único sitio: `backend/app/domain/licensing.py`. Ver `docs/licensing.md`.
6. **El binario nunca pasa por el backend.** Subidas y descargas por URL presignada.
7. **Los tipos del frontend se generan del OpenAPI.** Ningún tipo de red a mano.

## Entornos y comandos

Python **3.12** en todas partes (`/usr/bin/python3.12`), venv por componente.

```bash
make setup        # crea venvs, instala deps de backend/models/infra y pnpm del front
make dev          # levanta postgis+minio (podman-compose) + backend + frontend
make test         # todos los tests
make lint         # ruff + mypy + eslint + prettier
make migrate      # alembic upgrade head
make seed         # datos sintéticos de desarrollo
```

Por componente:

```bash
backend/.venv/bin/pytest backend/tests -q
models/.venv/bin/pytest models/tests -q
cd frontend && pnpm test && pnpm lint
cd infra && ../infra/.venv/bin/cdk diff -c env=staging
```

## Cómo se trabaja aquí

- Ramas `feature/*` salen de `develop`, nunca de `main`.
- Un PR = un cambio coherente. Si toca el contrato, actualiza `docs/api.md` en el mismo PR.
- Antes de terminar cualquier cambio: `make lint && make test`.
- Migraciones de Alembic siempre compatibles hacia atrás (expand → migrate → contract).
- Los pesos de modelos **no** van a git: van a S3, referenciados desde la tabla `models`.

## Agentes

`.claude/agents/` define los agentes del proyecto: `planner`, `backend-dev`,
`frontend-dev`, `astro-ml`, `infra-dev`, `qa-tester`. Cada uno tiene su alcance de
ficheros; respétalo para que puedan trabajar en paralelo sin pisarse.
