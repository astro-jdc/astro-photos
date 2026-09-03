# Architecture Decision Records

Una decisión no obvia = un ADR. Formato: contexto, decisión, consecuencias,
alternativas descartadas. Los ADR no se editan una vez aceptados: se **sustituyen**
por uno nuevo que los marca como `Superseded by NNNN`.

| # | título | estado |
|---|---|---|
| [0001](0001-monorepo.md) | Monorepo con un venv por componente | Aceptado |
| [0002](0002-postgres-postgis.md) | PostgreSQL + PostGIS + pgvector frente a DynamoDB | Aceptado |
| [0003](0003-presigned-uploads.md) | Subida directa a S3 por URL presignada | Aceptado |
| [0004](0004-batch-spot-gpu.md) | AWS Batch sobre spot GPU frente a SageMaker | Aceptado |
| [0005](0005-nuxt-frontend.md) | Nuxt 4 con renderizado híbrido frente a SPA | Aceptado |
| [0006](0006-derived-license.md) | La licencia de salida es la combinación más restrictiva | Aceptado |
| [0007](0007-no-aperture-synthesis.md) | No prometemos síntesis de apertura | Aceptado |
| [0008](0008-wcs-guided-alignment.md) | Alineación guiada por WCS en lugar de flujo óptico aprendido | Aceptado |
