# Arquitectura

## Vista general

```
                        ┌──────────────── Route 53 + ACM ────────────────┐
                        │                                                │
   navegador  ──────▶  CloudFront  ──┬──▶  S3  (Nuxt prerenderizado + assets)
                          │          │
                          │          ├──▶  S3  (previews y thumbs, OAC)
                          │          │
                          │          └──▶  ALB  ──▶  ECS Fargate  (FastAPI)
                          │                              │
                          │                              ├──▶ Aurora Serverless v2
                          │                              │      PostgreSQL 16
                          │                              │      + PostGIS + pgvector
                          │                              │
                          │                              ├──▶ S3 originals (privado)
                          │                              ├──▶ SQS ingest
                          │                              └──▶ SQS reconstruct
                          │
   subida directa  ───────┴──▶  S3 uploads  ──(evento)──▶  Lambda verify  ──▶ SQS ingest

   SQS ingest       ──▶  ECS Fargate worker "ingest"
                          EXIF/XMP → previews → plate solve (ASTAP) →
                          métricas de calidad → embedding → status=ready

   SQS reconstruct  ──▶  Lambda dispatcher ──▶ AWS Batch (spot g5/g6, GPU)
                          contenedor models/ → resultado a S3 → webhook al backend

   Cognito User Pool ──▶ JWT ──▶ FastAPI (validación por JWKS)
```

## Componentes

### `frontend/` — Nuxt 4 (Vue 3, TypeScript)

- Renderizado híbrido: las páginas de galería, objeto y foto se **prerenderizan**
  (ISR vía CloudFront) porque son públicas e indexables; el panel de usuario y el
  constructor de reconstrucciones son SPA cliente.
- Estado con Pinia, datos con `useFetch`/TanStack Query, UI con Tailwind + shadcn-vue.
- Visor de imagen astronómica propio sobre WebGL (`<AstroViewer>`): zoom profundo por
  tiles, estiramiento no lineal (asinh/STF) en el shader, control de blanco/negro/gamma,
  y superposición del WCS (rejilla RA/Dec y nombres de estrellas) cuando la foto está
  resuelta astrométricamente.
- Mapa de cobertura con MapLibre GL: dónde y cuándo se ha fotografiado un objeto.
- Tipos de la API generados desde OpenAPI; ningún tipo de red escrito a mano.
- i18n es/en desde el día uno.

### `backend/` — FastAPI (Python 3.12)

Estructura hexagonal ligera:

```
app/
  api/v1/            routers finos: validan, delegan, serializan
  domain/            reglas puras y testeables sin IO
    licensing.py       resolve_output_license()  ← una sola fuente de verdad
    quality.py         quality_score(), airmass(), moon geometry
    selection.py       elección de los mejores N frames para un job
  services/          orquestación con IO (S3, SQS, DB)
  repositories/      SQLAlchemy 2.0 async
  models/            tablas SQLAlchemy
  schemas/           Pydantic v2 (entrada/salida)
  workers/           consumidores de SQS: ingest, reconstruct_callback
  core/              config, seguridad, logging estructurado, errores
migrations/          Alembic
tests/               unit / integration (testcontainers) / contract
```

- `asyncpg` + SQLAlchemy 2.0 async, `alembic` para migraciones.
- Logging JSON estructurado, `X-Request-ID` propagado, tracing OpenTelemetry → X-Ray.
- Idempotencia en todos los POST que crean trabajos (`Idempotency-Key`).
- Nada de binarios por el backend: S3 presignado en ambos sentidos.

### `models/` — pipelines de reconstrucción (Python 3.12)

Paquete `astrostack`, ejecutable **sin AWS y sin GPU** desde la línea de comandos:

```
models/
  astrostack/
    io/            lectura de RAW/FITS/TIFF, normalización a float32 lineal
    calibrate/     darks, flats, bias, cosmic rays, gradiente de fondo
    align/         detección de estrellas (sep), astroalign, reproject por WCS
    stack/         media/mediana, sigma-clip, winsorized, drizzle, wavelet
    enhance/       deconvolución (Richardson-Lucy, Wiener), realce de detalle
    sr/            super-resolución multi-frame aprendida (torch)
    metrics/       FWHM, SNR, excentricidad, PSNR/SSIM contra referencia
    pipelines/     grafos declarativos YAML → ejecución
    cli.py         `astrostack run <pipeline.yaml> --inputs ... --out ...`
  configs/         pipelines versionados (classical-stack-v1.yaml, …)
  training/        entrenamiento, datasets, evaluación
  weights/         .gitignore'd; los pesos van a S3 + DVC
  notebooks/       exploración
  Dockerfile       imagen para AWS Batch (CUDA) y local (CPU/XPU)
```

Regla dura: **todo pipeline es reproducible**. Misma entrada + mismo
`pipeline_version` + mismos `params` → misma salida bit a bit. Semillas fijas,
sin `random` global, sin dependencias de orden de listado del sistema de ficheros.

### `infra/` — AWS CDK en Python

Un stack por entorno, mismo código:

```
infra/
  app.py                    cdk.App, lee -c env=staging|prod
  config.py                 EnvConfig: tamaños, dominios, límites, presupuesto
  stacks/
    network_stack.py        VPC, subredes, endpoints de VPC (S3, SQS, ECR)
    data_stack.py           Aurora Serverless v2, buckets, SQS, Secrets Manager
    auth_stack.py           Cognito User Pool + clientes + dominio hospedado
    api_stack.py            ECS Fargate, ALB, autoescalado, CodeDeploy blue/green
    edge_stack.py           CloudFront, OAC, certificados, WAF, Route 53
    compute_stack.py        AWS Batch: compute env spot GPU, colas, job definitions
    observability_stack.py  alarmas, dashboards, budget alerts
```

### Coste

Diseñado para que **staging cueste casi nada cuando nadie lo usa**: Aurora Serverless
se auto-pausa, Fargate baja a 1 tarea mínima, Batch solo arranca instancias GPU spot
cuando hay un job en cola y las apaga al terminar. Alarma de presupuesto de AWS a
30 €/mes en staging y a un límite configurable en prod.

## Decisiones registradas

Ver `docs/adr/`. Las importantes: 0001 monorepo, 0002 Postgres+PostGIS frente a
DynamoDB, 0003 subida presignada directa a S3, 0004 Batch spot frente a SageMaker,
0005 Nuxt frente a SPA, 0006 licencia de salida = combinación más restrictiva.
