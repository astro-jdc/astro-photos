"""Configuración por entorno de astro-photos.

**Toda** la diferencia entre `staging` y `prod` vive aquí y en ningún otro sitio:
tamaños de Aurora, rango de tareas Fargate, instancias de Batch, ciclos de vida de
S3, presupuesto mensual y si hay WAF. Los stacks solo leen de `EnvConfig`; si un
stack contiene un `if env == "prod"` es un bug.

Ver `docs/branching.md` para la tabla de entornos que esto implementa.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

import aws_cdk as cdk

EnvName = Literal["staging", "prod"]

PROJECT = "astro-photos"
"""Prefijo de todos los nombres físicos y valor del tag `Project`."""

#: Tipo de cambio usado solo para traducir el presupuesto (que razonamos en euros)
#: a la unidad que acepta AWS Budgets. Ajustar aquí si cambia la divisa de la cuenta.
EUR_TO_USD = 1.10

#: Cuenta ficticia usada únicamente para que `cdk synth` funcione sin credenciales.
#: Un `cdk deploy` real siempre resuelve la cuenta de verdad (CDK_DEFAULT_ACCOUNT,
#: `-c account=...` o las credenciales del rol OIDC).
SYNTH_PLACEHOLDER_ACCOUNT = "000000000000"


@dataclass(frozen=True)
class AuroraConfig:
    """Aurora Serverless v2 PostgreSQL 16 (PostGIS + pgvector + citext + pgcrypto)."""

    #: Capacidad mínima en ACU. **Ojo**: para que la auto-pausa funcione de verdad
    #: AWS exige `min_acu = 0` (con 0,5 el cluster nunca llega a pausarse y se
    #: facturan ~43 €/mes en reposo). `docs/branching.md` dice "0,5–2 ACU con
    #: auto-pausa": aquí se implementa como 0–2, que es lo que hace lo que el
    #: documento quiere decir — 0,5 ACU es el primer escalón al despertar.
    min_acu: float
    max_acu: float
    #: Minutos de inactividad antes de auto-pausar (escala a 0 ACU). `None` = nunca.
    auto_pause_minutes: int | None
    #: Réplicas de lectura en otra AZ. 0 = single-AZ (staging, barato).
    readers: int
    backup_retention_days: int
    deletion_protection: bool
    performance_insights: bool
    #: Umbral de la alarma de CPU del cluster (%).
    cpu_alarm_threshold: int = 80


@dataclass(frozen=True)
class FargateConfig:
    """Servicio de la API (FastAPI) en ECS Fargate."""

    cpu: int
    memory_mib: int
    desired_count: int
    min_tasks: int
    max_tasks: int
    #: Objetivo de utilización de CPU para el autoescalado.
    cpu_target_percent: int
    #: Mensajes visibles en la cola `ingest` por tarea antes de escalar.
    queue_messages_per_task: int
    #: Peso de Fargate Spot en el worker de ingesta (0 = solo on-demand).
    #: La API va siempre en capacidad on-demand: un corte de spot en medio de un
    #: despliegue blue/green es justo lo que no queremos.
    spot_weight: int
    #: Umbral de la alarma de latencia p99 (segundos) que dispara el rollback.
    p99_latency_seconds: float
    #: Nº de 5xx en 5 minutos que dispara el rollback.
    alb_5xx_threshold: int


@dataclass(frozen=True)
class BatchConfig:
    """AWS Batch: reconstrucciones y entrenamiento en GPU spot."""

    #: Familias de instancia GPU aceptables, en orden de preferencia.
    instance_types: tuple[str, ...]
    #: Techo de vCPU del compute environment. `min_vcpus` es SIEMPRE 0.
    max_vcpus: int
    #: % del precio on-demand que estamos dispuestos a pagar en spot.
    spot_bid_percentage: int
    job_vcpus: int
    job_memory_mib: int
    job_gpus: int
    #: Timeout duro por job. Un job colgado no puede quemar el presupuesto.
    job_timeout_hours: int
    #: Reintentos ante interrupción de spot.
    retry_attempts: int


@dataclass(frozen=True)
class StorageConfig:
    """Ciclo de vida de los tres buckets. Ver `docs/branching.md`."""

    #: Días que sobrevive un objeto en `uploads/staging/` antes de expirar.
    uploads_expiration_days: int
    #: Días antes de mover un original frío a Glacier Instant Retrieval.
    originals_glacier_ir_days: int
    #: `None` = los originales no expiran nunca (prod). En staging se borran.
    originals_expiration_days: int | None
    #: Días antes de mover derivados fríos a Infrequent Access.
    derived_ia_days: int
    #: Días que se guardan las versiones no actuales del bucket de originales.
    noncurrent_version_days: int
    #: Retención de los logs de acceso / CloudFront.
    access_log_retention_days: int


@dataclass(frozen=True)
class EnvConfig:
    """Configuración completa de un entorno."""

    name: EnvName
    account: str
    region: str

    # --- dominios -----------------------------------------------------------
    domain_name: str
    #: Subdominio de la API detrás del ALB (CloudFront enruta `/api/*` aquí).
    api_domain_name: str
    #: Id de la zona hospedada de Route 53. `None` → no se crean registros DNS y
    #: la validación de ACM se hace a mano (permite `cdk synth` sin credenciales).
    hosted_zone_id: str | None
    hosted_zone_name: str | None

    # --- tamaños ------------------------------------------------------------
    aurora: AuroraConfig
    fargate: FargateConfig
    batch: BatchConfig
    storage: StorageConfig

    # --- red ----------------------------------------------------------------
    max_azs: int
    #: NAT gateways. Uno solo en staging: son ~32 €/mes cada uno.
    nat_gateways: int

    # --- seguridad / observabilidad ----------------------------------------
    waf_enabled: bool
    log_retention_days: int
    #: Presupuesto mensual objetivo. Dispara alarma de AWS Budgets al 80 % y al 100 %.
    monthly_budget_eur: int
    alert_emails: tuple[str, ...]

    # --- ciclo de vida de los recursos --------------------------------------
    retain_data_on_delete: bool
    cost_center: str

    tags: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------ util
    @property
    def is_prod(self) -> bool:
        return self.name == "prod"

    @property
    def prefix(self) -> str:
        """Prefijo de nombres físicos: `astro-photos-staging`."""
        return f"{PROJECT}-{self.name}"

    @property
    def monthly_budget_usd(self) -> float:
        return round(self.monthly_budget_eur * EUR_TO_USD, 2)

    @property
    def removal_policy(self) -> cdk.RemovalPolicy:
        return cdk.RemovalPolicy.RETAIN if self.retain_data_on_delete else cdk.RemovalPolicy.DESTROY

    @property
    def auto_delete_objects(self) -> bool:
        """Vaciar los buckets al destruir el stack (solo donde no retenemos datos)."""
        return not self.retain_data_on_delete

    @property
    def cdk_env(self) -> cdk.Environment:
        return cdk.Environment(account=self.account, region=self.region)

    @property
    def us_east_1_env(self) -> cdk.Environment:
        """CloudFront exige certificado ACM y WAF en us-east-1."""
        return cdk.Environment(account=self.account, region="us-east-1")

    def stack_name(self, component: str) -> str:
        """`AstroPhotos-staging-Network`."""
        return f"AstroPhotos-{self.name}-{component}"

    def resource_name(self, *parts: str) -> str:
        """`astro-photos-staging-uploads`."""
        return "-".join((self.prefix, *parts))

    def base_tags(self) -> dict[str, str]:
        return {
            "Project": PROJECT,
            "Environment": self.name,
            "ManagedBy": "cdk",
            "CostCenter": self.cost_center,
            **self.tags,
        }


# --------------------------------------------------------------------------- #
# Definición de los dos entornos
# --------------------------------------------------------------------------- #

_STAGING = dict(
    name="staging",
    domain_name="dev.astrophotos.app",
    api_domain_name="api.dev.astrophotos.app",
    hosted_zone_id=None,
    hosted_zone_name="astrophotos.app",
    aurora=AuroraConfig(
        min_acu=0,  # 0 = puede pausarse; ver la nota en AuroraConfig
        max_acu=2,
        auto_pause_minutes=15,
        readers=0,
        backup_retention_days=1,
        deletion_protection=False,
        performance_insights=False,
    ),
    fargate=FargateConfig(
        cpu=512,
        memory_mib=1024,
        desired_count=1,
        min_tasks=1,
        max_tasks=2,
        cpu_target_percent=70,
        queue_messages_per_task=50,
        spot_weight=100,
        p99_latency_seconds=3.0,
        alb_5xx_threshold=10,
    ),
    batch=BatchConfig(
        instance_types=("g5.xlarge",),
        max_vcpus=4,
        spot_bid_percentage=60,
        job_vcpus=4,
        job_memory_mib=15000,
        job_gpus=1,
        job_timeout_hours=2,
        retry_attempts=3,
    ),
    storage=StorageConfig(
        uploads_expiration_days=7,
        originals_glacier_ir_days=30,
        originals_expiration_days=30,  # staging no guarda datos reales
        derived_ia_days=30,
        noncurrent_version_days=7,
        access_log_retention_days=14,
    ),
    max_azs=2,
    nat_gateways=1,
    waf_enabled=False,
    log_retention_days=14,
    monthly_budget_eur=30,
    retain_data_on_delete=False,
    cost_center="platform-staging",
)

_PROD = dict(
    name="prod",
    domain_name="astrophotos.app",
    api_domain_name="api.astrophotos.app",
    hosted_zone_id=None,
    hosted_zone_name="astrophotos.app",
    aurora=AuroraConfig(
        min_acu=1,
        max_acu=16,
        auto_pause_minutes=None,  # prod nunca se pausa
        readers=1,  # Multi-AZ
        backup_retention_days=14,
        deletion_protection=True,
        performance_insights=True,
        cpu_alarm_threshold=75,
    ),
    fargate=FargateConfig(
        cpu=1024,
        memory_mib=2048,
        desired_count=2,
        min_tasks=2,
        max_tasks=10,
        cpu_target_percent=60,
        queue_messages_per_task=100,
        spot_weight=0,
        p99_latency_seconds=1.5,
        alb_5xx_threshold=5,
    ),
    batch=BatchConfig(
        instance_types=("g6.xlarge", "g6.2xlarge", "g5.xlarge", "g5.2xlarge"),
        max_vcpus=64,
        spot_bid_percentage=80,
        job_vcpus=8,
        job_memory_mib=30000,
        job_gpus=1,
        job_timeout_hours=6,
        retry_attempts=4,
    ),
    storage=StorageConfig(
        uploads_expiration_days=14,
        originals_glacier_ir_days=90,
        originals_expiration_days=None,  # los originales no se borran jamás
        derived_ia_days=60,
        noncurrent_version_days=90,
        access_log_retention_days=90,
    ),
    max_azs=3,
    nat_gateways=2,
    waf_enabled=True,
    log_retention_days=90,
    monthly_budget_eur=300,
    retain_data_on_delete=True,
    cost_center="platform-prod",
)

_ENVS: dict[str, dict] = {"staging": _STAGING, "prod": _PROD}


def _context(app: cdk.App, key: str) -> str | None:
    value = app.node.try_get_context(key)
    return str(value) if value not in (None, "") else None


def resolve_account(app: cdk.App) -> str:
    """Cuenta AWS: `-c account=` → `CDK_DEFAULT_ACCOUNT` → `AWS_ACCOUNT_ID` → placeholder.

    El placeholder existe para que `cdk synth` (y por tanto los tests y el CI) no
    necesiten credenciales. Cualquier despliegue real tiene una cuenta resuelta.
    """
    return (
        _context(app, "account")
        or os.environ.get("CDK_DEFAULT_ACCOUNT")
        or os.environ.get("AWS_ACCOUNT_ID")
        or SYNTH_PLACEHOLDER_ACCOUNT
    )


def resolve_region(app: cdk.App) -> str:
    return (
        _context(app, "region")
        or os.environ.get("CDK_DEFAULT_REGION")
        or os.environ.get("AWS_REGION")
        or "eu-west-1"
    )


def load(app: cdk.App) -> EnvConfig:
    """Construye la `EnvConfig` a partir del contexto del `cdk.App`.

    Contexto soportado::

        -c env=staging|prod          (obligatorio)
        -c account=123456789012      opcional, si no se toma del entorno
        -c region=eu-west-1          opcional
        -c hosted_zone_id=Z0123...   opcional, activa los registros de Route 53
        -c domain_name=...           opcional, para entornos de prueba
        -c alert_email=a@b.c         opcional, destino de las alarmas
    """
    env_name = _context(app, "env")
    if env_name is None:
        raise ValueError("Falta el contexto obligatorio: usa `cdk synth -c env=staging|prod`")
    if env_name not in _ENVS:
        raise ValueError(f"Entorno desconocido {env_name!r}; usa uno de {sorted(_ENVS)}")

    overrides: dict[str, object] = {
        "account": resolve_account(app),
        "region": resolve_region(app),
    }
    for key in ("domain_name", "api_domain_name", "hosted_zone_id", "hosted_zone_name"):
        value = _context(app, key)
        if value is not None:
            overrides[key] = value

    alert_email = _context(app, "alert_email")
    overrides["alert_emails"] = (alert_email,) if alert_email else ()

    return EnvConfig(**{**_ENVS[env_name], **overrides})  # type: ignore[arg-type]
