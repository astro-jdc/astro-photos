<!--
Un PR = un cambio coherente. Si toca el contrato, actualiza docs/api.md en el
mismo PR (CLAUDE.md).
-->

## Qué cambia y por qué

<!-- Dos o tres frases. El "por qué" importa más que el "qué": el diff ya dice el qué. -->

Closes #

## Alcance

- [ ] `backend/`
- [ ] `frontend/`
- [ ] `models/`
- [ ] `infra/` o `.github/`
- [ ] `docs/`

## Comprobaciones

- [ ] `make lint && make test` en verde en local
- [ ] La rama sale de `develop` (o de `main` si es `hotfix/*`)
- [ ] No hay ningún secreto, credencial ni endpoint privado en el diff

## Contrato de datos y API

- [ ] No cambia el contrato
- [ ] Cambia: `docs/api.md` y/o `docs/data-model.md` actualizados en este mismo PR
- [ ] Hay migración de Alembic y es **compatible hacia atrás** (expand → migrate → contract)
- [ ] Los tipos del frontend se han regenerado del OpenAPI (`pnpm run gen:api`)

## Si toca `models/`

- [ ] Misma entrada + `pipeline_version` + `params` → misma salida (semillas fijas, sin `random` global)
- [ ] Toda salida de un modelo aprendido lleva mapa de incertidumbre y etiqueta visible
- [ ] No se promete resolución angular por encima del límite de difracción de la mejor óptica de entrada

## Si toca `infra/`

- [ ] El `cdk diff` está comentado en el PR y lo he leído entero
- [ ] Ningún bucket público; cifrado en reposo
- [ ] El coste en reposo de staging no sube (Batch a 0, Aurora pausable)
- [ ] Los recursos nuevos llevan `Project`, `Environment`, `ManagedBy` y `CostCenter`

## Cómo se prueba

<!-- Pasos concretos para que otra persona lo reproduzca. -->

## Riesgo y vuelta atrás

<!-- Qué pasa si esto sale mal en producción y cómo se revierte. -->
