# `infra/` — infraestructura de astro-photos (AWS CDK v2, Python)

Un solo árbol de stacks parametrizado por entorno. Toda la diferencia entre
`staging` y `prod` vive en **`config.py`**; si algún stack tiene un
`if env == "prod"`, es un bug.

```
app.py                     cdk.App, lee -c env=staging|prod y compone los stacks
config.py                  EnvConfig: dominios, tamaños, límites, presupuesto
common.py                  utilidades (retención de logs, zona de Route 53)
stacks/
  network_stack.py         VPC, subredes, VPC endpoints (S3, ECR, SQS, Secrets, Logs)
  ecr_stack.py             repositorios de imágenes (se despliega el primero)
  data_stack.py            Aurora Serverless v2, 4 buckets, SQS + DLQ, secretos
  auth_stack.py            Cognito User Pool, cliente web, dominio, grupos
  api_stack.py             ECS Fargate, ALB, autoescalado, blue/green, migraciones
  edge_stack.py            CloudFront + OAC + WAF + ACM + Route 53 (2 stacks)
  compute_stack.py         AWS Batch GPU spot, Lambda dispatcher, Lambda verify
  observability_stack.py   log groups, dashboard, alarmas, SNS, AWS Budgets
lambdas/                   código de las dos Lambdas (sin dependencias externas)
scripts/enable_extensions.sql   PostGIS, pgvector, citext, pgcrypto
tests/                     pytest sobre la plantilla sintetizada
```

Stacks resultantes: `AstroPhotos-<env>-{Network,Ecr,Data,Auth,Compute,Api,EdgeGlobal,Edge,Observability}`.

---

## Puesta en marcha

```bash
make setup-infra
# equivale a:
#   /usr/bin/python3.12 -m venv infra/.venv
#   infra/.venv/bin/pip install -U pip
#   infra/.venv/bin/pip install -r infra/requirements.txt
```

Con eso queda todo, CLI incluido: `requirements.txt` trae `aws-cdk-cli`, que es
el paquete oficial de PyPI del CLI de CDK (empaqueta su propio Node, ~220 MB).
No hace falta npm ni tener Node instalado, y local y CI usan exactamente la misma
versión porque está fijada.

```bash
cd infra
export PATH="$PWD/.venv/bin:$PATH"

cdk synth -c env=staging
cdk diff  -c env=prod --all
python -m pytest tests -q

# o desde la raíz, como dice el Makefile:
make synth
```

Contexto que acepta la app:

| contexto | por defecto | para qué |
|---|---|---|
| `env` | — (**obligatorio**) | `staging` o `prod` |
| `account` | `CDK_DEFAULT_ACCOUNT`, o `000000000000` | cuenta AWS |
| `region` | `CDK_DEFAULT_REGION`, o `eu-west-1` | región. **Ojo**: el CLI sobrescribe `CDK_DEFAULT_REGION` con lo que resuelva el SDK, así que sin credenciales hay que pasar `-c region=` |
| `hosted_zone_id` | ninguno | si se pasa, se crean los registros de Route 53 y la validación de ACM es automática |
| `backend_image_tag` | `latest` | etiqueta de la imagen del backend |
| `models_image_tag` | `latest` | etiqueta de la imagen de `models/` |
| `alert_email` | ninguno | destino de las alarmas y del aviso de presupuesto |

La cuenta placeholder existe para que `cdk synth` (y por tanto el CI y los tests)
funcione **sin credenciales**. Ningún despliegue real la usa. Por el mismo motivo
`stacks/base.py` deriva las zonas de disponibilidad del nombre de la región en
lugar de preguntárselas a EC2: un *context lookup* exigiría credenciales y haría
que el `cdk.out` dependiera de la cuenta.

## Bootstrap de la cuenta

Una sola vez por cuenta y por región. Hacen falta **dos** regiones: la principal
y `us-east-1`, porque el certificado y el WAF de CloudFront viven allí
obligatoriamente.

```bash
cd infra
export CDK_NEW_BOOTSTRAP=1
cdk bootstrap aws://<CUENTA>/eu-west-1 aws://<CUENTA>/us-east-1 \
  --cloudformation-execution-policies arn:aws:iam::aws:policy/AdministratorAccess
```

## Despliegue

En el día a día no se despliega a mano: lo hacen
`.github/workflows/deploy-staging.yml` (push a `develop`) y `deploy-prod.yml`
(push a `main`, con aprobación manual). El orden importa y es este:

1. `cdk deploy AstroPhotos-<env>-Ecr` — los repositorios tienen que existir antes
   de que haya imágenes que subir.
2. `docker build` + push de `backend/` y `models/`, etiquetadas con el sha.
3. `cdk deploy --all -c backend_image_tag=... -c models_image_tag=...`.
4. **Migraciones**: `aws ecs run-task` sobre la task definition
   `astro-photos-<env>-migrations` y esperar a que termine. Corre el SQL de
   extensiones y después `alembic upgrade head`. Nunca al arrancar la API.
5. Frontend: build, `aws s3 sync` al bucket del sitio, invalidación de CloudFront.
6. Smoke test contra `/api/v1/readyz`.

A mano, si hace falta:

```bash
cd infra && export PATH="$PWD/.venv/bin:$PATH"
cdk deploy --all -c env=staging \
  -c backend_image_tag=sha-abc123456789 \
  -c models_image_tag=sha-abc123456789 \
  --require-approval never
```

### Extensiones de PostgreSQL

Aurora trae los binarios de PostGIS, pgvector, citext y pgcrypto pero no crea las
extensiones. El SQL idempotente está en `scripts/enable_extensions.sql` y se usa
en dos sitios:

* el contenedor `db-bootstrap` de la task definition de migraciones lo ejecuta
  antes de Alembic;
* se publica también en el parámetro de SSM
  `/astro-photos/<env>/db/enable-extensions-sql`, para poder aplicarlo a mano.

---

## Coste

El diseño se apoya en tres decisiones, y las tres tienen test en
`tests/test_cost_guardrails.py`:

* **Batch con `minvCpus = 0`.** Nunca hay una instancia GPU encendida sin trabajo
  en cola. Un `g5.xlarge` on-demand cuesta ~1 €/h: dejarlo encendido se come el
  presupuesto de staging en un día.
* **Aurora Serverless v2 con auto-pausa.** En staging el cluster escala a 0 ACU
  tras 15 minutos sin actividad. Ojo: AWS exige `MinCapacity = 0` para que la
  pausa ocurra de verdad; con 0,5 ACU el cluster nunca se pausa y son ~43 €/mes.
  Por eso `docs/branching.md` dice "0,5–2 ACU con auto-pausa" y aquí está
  implementado como 0–2: 0,5 es el primer escalón al despertar.
* **Un solo NAT gateway en staging** (~32 €/mes cada uno) más VPC endpoints para
  S3, ECR, SQS, Secrets Manager y CloudWatch Logs, que es lo que evita pagar
  tránsito NAT por cada pull de imagen y cada objeto.

### Estimación con tráfico bajo

Precios de referencia de `eu-west-1`, sin IVA, en euros al mes.

**staging** (nadie usándolo salvo el CI):

| concepto | € / mes |
|---|---|
| NAT gateway (1) | 32 |
| VPC endpoints interface (5 × 2 AZ) | 14 |
| Fargate 1 tarea 0,5 vCPU / 1 GB (API) | 13 |
| Fargate 1 tarea worker de ingesta (Spot) | 4 |
| ALB | 17 |
| Aurora Serverless v2 pausada + 20 GB | 4 |
| S3, SQS, Cognito, CloudWatch, ECR | 4 |
| CloudFront | ~0 |
| Batch (0 instancias en reposo) | 0 |
| **total en reposo** | **≈ 88** |

Es decir: **el objetivo de 30 €/mes de `docs/branching.md` no se cumple con esta
topología**, y conviene decirlo en vez de maquillarlo. El suelo lo ponen el NAT
gateway, el ALB y los VPC endpoints, que son fijos. Para bajar de 30 €:

* quitar los 5 VPC endpoints de interfaz en staging (−14 €; a cambio, más
  tránsito NAT, que con poco uso sale más barato);
* apagar el NAT gateway y sacar las tareas a subredes públicas con IP pública
  (−32 €, peor postura de seguridad), o sustituirlo por una instancia NAT `t4g.nano`
  (−28 €);
* sustituir el ALB por un Application Load Balancer compartido entre entornos, o
  exponer la API por CloudFront → Lambda Function URL (−17 €);
* apagar staging fuera de horario (`desired_count = 0` por la noche y fines de
  semana) con una regla de EventBridge.

Con las tres primeras, staging queda en **≈ 25 €/mes**. La decisión no es técnica
sino de producto, así que está documentada aquí y no aplicada por defecto.

**prod** con tráfico bajo (≈ 50 k visitas/mes, 2 000 fotos nuevas, 100
reconstrucciones):

| concepto | € / mes |
|---|---|
| NAT gateways (2) | 64 |
| VPC endpoints interface (5 × 3 AZ) | 21 |
| Fargate 2 tareas 1 vCPU / 2 GB (API) | 53 |
| Fargate worker de ingesta | 26 |
| ALB | 20 |
| Aurora Serverless v2 (1–16 ACU, ~1,5 ACU medio, Multi-AZ) | 130 |
| Almacenamiento Aurora + backups (100 GB) | 12 |
| S3 (2 TB originales en Glacier IR + 200 GB derivados) | 45 |
| CloudFront (500 GB + peticiones) | 45 |
| WAF (ACL + reglas gestionadas + peticiones) | 12 |
| Batch spot GPU (100 jobs × 20 min en `g6.xlarge` spot ≈ 0,35 €/h) | 12 |
| Cognito (plan PLUS, ~2 000 MAU) | 40 |
| CloudWatch, ECR, SNS, Secrets Manager | 25 |
| **total** | **≈ 505** |

El presupuesto de prod está puesto en 300 €/mes en `config.py`: **la alarma
saltará antes de llegar aquí**, que es justo para lo que sirve. Ajústalo cuando
se conozca el tráfico real, o baja el plan de Cognito a `ESSENTIALS` (−25 €) y
usa un solo NAT gateway (−32 €).

Estos números son órdenes de magnitud para decidir, no una factura. La cifra
buena la da la etiqueta `Project=astro-photos` en Cost Explorer, que funciona
porque **todo** lleva los cuatro tags obligatorios.

---

## Destruir un entorno

```bash
cd infra && export PATH="$PWD/.venv/bin:$PATH"
cdk destroy --all -c env=staging
```

Staging se borra entero: `removal_policy = DESTROY` y `auto_delete_objects`, así
que los buckets se vacían solos.

**Prod no se puede destruir así, a propósito.** Sus buckets, la base de datos y
el User Pool tienen `RemovalPolicy.RETAIN` y `deletion_protection = True`; el
`destroy` fallará y dejará los datos donde están. Para desmontar prod de verdad
hay que, en este orden y a mano: quitar la protección de borrado del cluster,
vaciar los buckets, borrar el User Pool y solo entonces destruir los stacks. Que
sea incómodo es la funcionalidad.

Al destruir queda huérfano lo que CloudFormation no gestiona: las imágenes de
ECR (si el repositorio se retiene), los logs ya escritos y los certificados de
ACM validados a mano.

---

## Lo que hay que configurar a mano

Nada de esto puede ir en el repositorio ni en el CDK:

1. **Cuenta AWS y bootstrap** en `eu-west-1` y `us-east-1`.
2. **Dominio y zona de Route 53.** `astrophotos.app` tiene que existir como zona
   hospedada; su id se pasa con `-c hosted_zone_id=Z...` (o por la variable
   `HOSTED_ZONE_ID` del environment de GitHub). Sin él no se crean registros DNS
   y la validación de los certificados ACM es manual.
3. **Proveedor OIDC de GitHub** (`token.actions.githubusercontent.com`) y dos
   roles, uno por entorno, con confianza limitada a
   `repo:astro-jdc/astro-photos:ref:refs/heads/develop` y `...:refs/heads/main`.
   Sus ARNs van al secreto `AWS_DEPLOY_ROLE_ARN` de cada environment.
4. **Verificación de SES o del remitente de Cognito** si se quiere que los
   correos de verificación salgan de un dominio propio (por defecto los envía
   Cognito, con límite de 50 al día).
5. **Suscripción al tema SNS de alarmas**: `-c alert_email=...` en el despliegue,
   o confirmar la suscripción a mano.
6. **Límites de servicio (quotas).** Los que se topan primero:
   - vCPU de instancias Spot G/VT — por defecto **0** en cuentas nuevas: hay que
     pedir al menos 4 (staging) o 64 (prod) o Batch nunca arrancará nada;
   - Elastic IPs por región (5) si se añaden más NAT gateways;
   - certificados ACM por región;
   - `AWS::Cognito` no tiene cuota relevante, pero el plan PLUS se factura por
     usuario activo: revisarlo antes de abrir el registro.
7. **AWS Budgets con SNS**: el tema tiene ya la política que permite publicar a
   `budgets.amazonaws.com`, pero la primera notificación por email exige
   confirmar la suscripción.
8. **Equipos de GitHub** referenciados en `.github/CODEOWNERS`
   (`@astro-jdc/{maintainers,backend,frontend,astro-ml,platform}`).

---

## Decisiones que sorprenden al leer el código

* **`ecr_stack.py` no está en el diagrama de `docs/architecture.md`.** Existe
  porque el CI sube las imágenes *antes* de desplegar el resto de stacks, y los
  repositorios tienen que preexistir. Es un stack de tres recursos.
* **La Lambda de verificación se dispara por EventBridge, no por notificación S3
  directa.** El evento es el mismo (`s3:ObjectCreated` con prefijo `staging/`),
  pero una notificación directa se materializa en el stack del bucket y, estando
  la Lambda en otro stack, crearía un ciclo `Data ↔ Compute`.
* **`EdgeStack` importa los buckets de datos por nombre**, no como referencia
  entre stacks, por la misma razón: si el origen con OAC escribiera la política
  del bucket, `Data` dependería de `Edge` y `Edge` de `Data`. La política de
  lectura con OAC se escribe explícitamente en `data_stack.py`, con la condición
  `AWS:SourceArn` limitada a las distribuciones de esta cuenta.
* **El security group de Aurora se crea en `data_stack.py`** y no en la red: la
  rotación del secreto le añade su propia regla de entrada, y desde otro stack
  eso también sería un ciclo.
* **La API va siempre en Fargate on-demand**, nunca en Spot. El worker de ingesta
  sí usa Spot en staging: que lo interrumpan a mitad solo significa que el
  mensaje vuelve a la cola.
