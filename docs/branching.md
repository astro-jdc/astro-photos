# Ramas, entornos y despliegue

## Ramas

```
main      ──●────────────●────────────●──────▶   PRODUCCIÓN  (astrophotos.app)
             ╲          ╱ ╲          ╱
develop   ────●──●──●──●───●──●──●──●───────▶   STAGING      (dev.astrophotos.app)
               ╲    ╱       ╲    ╱
feature/*       ●──●         ●──●                efímero, PR review app
```

- **`main`** — solo producción. Protegida: nada de push directo, PR obligatorio desde
  `develop` (o `hotfix/*`), 1 aprobación, todos los checks en verde, historial lineal
  (squash merge). Cada merge a `main` etiqueta `v<AÑO>.<MES>.<N>` y despliega a prod.
- **`develop`** — rama de integración, es la base de todos los PRs de feature.
  Cada merge despliega automáticamente al entorno **staging**, que es una copia
  completa e independiente de la infraestructura (su propia VPC, RDS, buckets,
  User Pool y distribución de CloudFront). Staging tiene datos sintéticos generados
  por `scripts/seed_dev.py`, nunca datos reales de usuarios.
- **`feature/<slug>`, `fix/<slug>`, `chore/<slug>`** — salen de `develop` y vuelven a
  `develop` por PR. CI corre lint + tipos + tests + build, y `cdk diff` comenta el
  cambio de infraestructura en el PR.
- **`hotfix/<slug>`** — sale de `main`, vuelve a `main` **y** a `develop` (el workflow
  abre el PR de back-merge automáticamente para que develop nunca se quede atrás).
- **`model/<slug>`** — entrenamientos. No despliegan nada: publican el artefacto de
  pesos y el informe de métricas, y el modelo se activa a mano con `POST /models/{id}/activate`.

## Entornos

| | dev (local) | staging | prod |
|---|---|---|---|
| rama | cualquiera | `develop` | `main` |
| dominio | `localhost:3000` | `dev.astrophotos.app` | `astrophotos.app` |
| infra | podman-compose | cuenta AWS, stack `AstroPhotos-staging` | cuenta AWS, stack `AstroPhotos-prod` |
| DB | postgis en contenedor | Aurora Serverless v2, 0.5–2 ACU, auto-pausa | Aurora Serverless v2, 1–16 ACU, Multi-AZ |
| storage | MinIO | S3, ciclo de vida agresivo (borra a 30 días) | S3 + Glacier IR para originales |
| cómputo | proceso local | ECS Fargate 1 tarea + Batch spot g5 (máx 1) | ECS Fargate 2–10 tareas + Batch spot g5/g6 |
| auth | Cognito local mock | User Pool `astro-photos-staging` | User Pool `astro-photos-prod` |
| modelos | pesos locales `models/weights/` | último modelo `is_active` de staging | modelo promovido explícitamente |
| coste objetivo | 0 € | < 30 €/mes | escala con uso |

Un único stack de CDK parametrizado por `-c env=staging|prod`. Las diferencias viven
en `infra/config.py`, no en ramas de código.

## Promoción

1. PRs de feature → `develop`. Se despliega staging solo.
2. Cuando staging está estable, PR `develop` → `main` con el changelog generado.
   El propio PR ejecuta `cdk diff` contra prod y publica un plan de migración de DB.
3. Merge → tag → deploy a prod con **blue/green** en ECS (CodeDeploy) y rollback
   automático si las alarmas de CloudWatch (5xx, latencia p99) saltan en 10 min.
4. Las migraciones de Alembic corren como una **tarea de ECS previa** al deploy, y
   deben ser siempre compatibles hacia atrás (expand → migrate → contract en 3 PRs).

## Reglas de protección (se aplican con `scripts/setup_repo.sh`)

- `main`: PR obligatorio, 1 review, checks `ci/backend`, `ci/frontend`, `ci/infra`,
  `ci/models`, sin force-push, sin borrado, conversaciones resueltas.
- `develop`: PR obligatorio, checks obligatorios, sin force-push.
- Secretos por entorno en GitHub Environments (`staging`, `production`); `production`
  exige aprobación manual de un revisor antes de correr el job de deploy.
- AWS se autentica por **OIDC** (`aws-actions/configure-aws-credentials`), sin claves
  de larga duración guardadas en el repo.
