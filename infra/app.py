#!/usr/bin/env python3
"""Punto de entrada del CDK de astro-photos.

Un único árbol de stacks parametrizado por entorno::

    cdk synth  -c env=staging
    cdk deploy -c env=prod --all

Los stacks se llaman `AstroPhotos-<env>-<Componente>`; `docs/branching.md` se refiere
al conjunto como `AstroPhotos-staging` / `AstroPhotos-prod`.

Orden de dependencias::

    Network ─┬─ Data ──┬─ Api ──┐
             │         │        ├─ Observability
             ├─ Compute┘        │
    Ecr ─────┘                  │
    Auth ───────────────────────┤
    EdgeGlobal (us-east-1) ── Edge
"""

from __future__ import annotations

import aws_cdk as cdk

import config
from stacks.api_stack import ApiStack
from stacks.auth_stack import AuthStack
from stacks.compute_stack import ComputeStack
from stacks.data_stack import DataStack
from stacks.ecr_stack import EcrStack
from stacks.edge_stack import EdgeGlobalStack, EdgeStack
from stacks.network_stack import NetworkStack
from stacks.observability_stack import ObservabilityStack

app = cdk.App()
cfg = config.load(app)

common = {"env": cfg.cdk_env, "cfg": cfg}

# --- base ------------------------------------------------------------------ #
network = NetworkStack(
    app,
    cfg.stack_name("Network"),
    description=f"VPC, subredes y VPC endpoints de astro-photos ({cfg.name})",
    **common,
)

# Repositorios de imágenes. Stack aparte y desplegado el primero porque el CI
# construye y sube las imágenes ANTES de desplegar el resto (ver deploy-*.yml).
ecr = EcrStack(
    app,
    cfg.stack_name("Ecr"),
    description=f"Repositorios ECR de backend y models ({cfg.name})",
    **common,
)

data = DataStack(
    app,
    cfg.stack_name("Data"),
    description=f"Aurora Serverless v2, buckets S3, colas SQS y secretos ({cfg.name})",
    vpc=network.vpc,
    **common,
)

auth = AuthStack(
    app,
    cfg.stack_name("Auth"),
    description=f"Cognito User Pool, cliente y grupos ({cfg.name})",
    **common,
)

# --- cómputo --------------------------------------------------------------- #
compute = ComputeStack(
    app,
    cfg.stack_name("Compute"),
    description=f"AWS Batch GPU spot + Lambdas de dispatch y verificación ({cfg.name})",
    vpc=network.vpc,
    uploads_bucket=data.uploads_bucket,
    originals_bucket=data.originals_bucket,
    derived_bucket=data.derived_bucket,
    ingest_queue=data.ingest_queue,
    reconstruct_queue=data.reconstruct_queue,
    models_repository=ecr.models_repository,
    **common,
)

api = ApiStack(
    app,
    cfg.stack_name("Api"),
    description=f"ECS Fargate + ALB + CodeDeploy blue/green + migraciones ({cfg.name})",
    vpc=network.vpc,
    database=data.database,
    db_secret=data.db_secret,
    app_secret=data.app_secret,
    uploads_bucket=data.uploads_bucket,
    originals_bucket=data.originals_bucket,
    derived_bucket=data.derived_bucket,
    ingest_queue=data.ingest_queue,
    reconstruct_queue=data.reconstruct_queue,
    user_pool=auth.user_pool,
    user_pool_client=auth.user_pool_client,
    backend_repository=ecr.backend_repository,
    db_extensions_parameter=data.db_extensions_parameter,
    **common,
)

# --- borde ----------------------------------------------------------------- #
# Certificado de CloudFront y WAF viven obligatoriamente en us-east-1.
edge_global = EdgeGlobalStack(
    app,
    cfg.stack_name("EdgeGlobal"),
    description=f"Certificado ACM y WAF de CloudFront en us-east-1 ({cfg.name})",
    env=cfg.us_east_1_env,
    cfg=cfg,
    cross_region_references=True,
)

edge = EdgeStack(
    app,
    cfg.stack_name("Edge"),
    description=f"CloudFront multi-origen, OAC, cabeceras de seguridad y DNS ({cfg.name})",
    derived_bucket_name=data.derived_bucket.bucket_name,
    logs_bucket_name=data.logs_bucket.bucket_name,
    certificate=edge_global.certificate,
    web_acl_arn=edge_global.web_acl_arn,
    cross_region_references=True,
    **common,
)

# --- observabilidad -------------------------------------------------------- #
ObservabilityStack(
    app,
    cfg.stack_name("Observability"),
    description=f"Log groups, dashboard, alarmas, SNS y AWS Budgets ({cfg.name})",
    database=data.database,
    queues=[data.ingest_queue, data.reconstruct_queue],
    dead_letter_queues=[data.ingest_dlq, data.reconstruct_dlq],
    load_balancer=api.load_balancer,
    target_group=api.blue_target_group,
    service=api.service,
    job_queue_name=compute.job_queue.job_queue_name,
    distribution=edge.distribution,
    **common,
)

# --- tags globales --------------------------------------------------------- #
for key, value in cfg.base_tags().items():
    cdk.Tags.of(app).add(key, value)

app.synth()
