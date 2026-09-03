"""Datos: Aurora Serverless v2, los tres buckets, las colas y los secretos.

Reglas duras que este stack materializa:

* **Buckets privados sin excepción.** `BLOCK_ALL`, cifrado en reposo, TLS obligado
  por política de bucket. Nada de ACLs.
* **Versionado en `originals`** y transición a Glacier Instant Retrieval: un
  original es la aportación irrepetible de un observador, no se pierde.
* **Staging casi gratis en reposo**: Aurora con auto-pausa a 0 ACU y ciclos de
  vida agresivos (`docs/branching.md`).
* Cada cola tiene su DLQ. Un mensaje envenenado nunca se descarta en silencio.
"""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_rds as rds
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from common import log_retention
from config import EnvConfig
from stacks.base import BaseStack

#: Aurora PostgreSQL 16.8 — la auto-pausa de Serverless v2 (escalar a 0 ACU)
#: necesita 16.3 o superior.
AURORA_PG_VERSION = rds.AuroraPostgresEngineVersion.of(
    "16.8", "16", serverless_v2_auto_pause_supported=True
)

DB_NAME = "astrophotos"
DB_USER = "astro_admin"

#: Prefijo del bucket de uploads donde el cliente sube con POST presignado y que
#: dispara la Lambda de verificación (ver `docs/api.md`).
UPLOADS_STAGING_PREFIX = "staging/"

EXTENSIONS_SQL = Path(__file__).resolve().parent.parent / "scripts" / "enable_extensions.sql"


class DataStack(BaseStack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cfg: EnvConfig,
        vpc: ec2.IVpc,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, cfg=cfg, **kwargs)

        self._create_buckets(cfg)
        self._create_queues(cfg)
        self._create_database(cfg, vpc)
        self._create_secrets(cfg)
        self._outputs()

    # ------------------------------------------------------------------ S3 --
    def _create_buckets(self, cfg: EnvConfig) -> None:
        st = cfg.storage

        # Bucket de logs de acceso (S3 + ALB + CloudFront). Objeto barato y con
        # expiración: los logs no son un archivo histórico.
        self.logs_bucket = s3.Bucket(
            self,
            "LogsBucket",
            bucket_name=cfg.resource_name("logs"),
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_PREFERRED,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-logs",
                    expiration=cdk.Duration.days(st.access_log_retention_days),
                    abort_incomplete_multipart_upload_after=cdk.Duration.days(3),
                )
            ],
            removal_policy=cfg.removal_policy,
            auto_delete_objects=cfg.auto_delete_objects,
        )

        # 1) uploads — zona de aterrizaje. El cliente sube aquí directamente con
        #    un POST presignado; nada de esto se sirve nunca al público.
        self.uploads_bucket = s3.Bucket(
            self,
            "UploadsBucket",
            bucket_name=cfg.resource_name("uploads"),
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=False,
            # La Lambda de verificación se engancha por EventBridge (ver
            # compute_stack): evita la dependencia circular que provoca la
            # notificación directa entre stacks.
            event_bridge_enabled=True,
            cors=[
                s3.CorsRule(
                    allowed_methods=[s3.HttpMethods.POST, s3.HttpMethods.PUT],
                    allowed_origins=[f"https://{cfg.domain_name}"]
                    + ([] if cfg.is_prod else ["http://localhost:3000"]),
                    allowed_headers=["*"],
                    exposed_headers=["ETag", "x-amz-request-id"],
                    max_age=3000,
                )
            ],
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-staged-uploads",
                    prefix=UPLOADS_STAGING_PREFIX,
                    # Una vez ingerida, la foto vive en `originals`. Lo que quede
                    # aquí es basura de subidas abandonadas.
                    expiration=cdk.Duration.days(st.uploads_expiration_days),
                ),
                s3.LifecycleRule(
                    id="abort-incomplete-multipart",
                    abort_incomplete_multipart_upload_after=cdk.Duration.days(1),
                ),
            ],
            removal_policy=cfg.removal_policy,
            auto_delete_objects=cfg.auto_delete_objects,
            server_access_logs_bucket=self.logs_bucket,
            server_access_logs_prefix="s3/uploads/",
        )

        # 2) originals — el archivo. Inmutable, versionado, nunca servido directo.
        originals_rules = [
            s3.LifecycleRule(
                id="archive-cold-originals",
                transitions=[
                    s3.Transition(
                        storage_class=s3.StorageClass.GLACIER_INSTANT_RETRIEVAL,
                        transition_after=cdk.Duration.days(st.originals_glacier_ir_days),
                    )
                ],
                noncurrent_version_expiration=cdk.Duration.days(st.noncurrent_version_days),
                abort_incomplete_multipart_upload_after=cdk.Duration.days(1),
            )
        ]
        if st.originals_expiration_days is not None:
            # Solo staging: los datos son sintéticos y se tiran.
            originals_rules.append(
                s3.LifecycleRule(
                    id="expire-synthetic-originals",
                    expiration=cdk.Duration.days(st.originals_expiration_days),
                )
            )

        self.originals_bucket = s3.Bucket(
            self,
            "OriginalsBucket",
            bucket_name=cfg.resource_name("originals"),
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=True,
            lifecycle_rules=originals_rules,
            removal_policy=cfg.removal_policy,
            auto_delete_objects=cfg.auto_delete_objects,
            server_access_logs_bucket=self.logs_bucket,
            server_access_logs_prefix="s3/originals/",
        )

        # 3) derived — previews, thumbs, tiles y resultados de reconstrucción.
        #    Público de cara al mundo SOLO a través de CloudFront con OAC.
        self.derived_bucket = s3.Bucket(
            self,
            "DerivedBucket",
            bucket_name=cfg.resource_name("derived"),
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=False,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="cool-down-derived",
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.INFREQUENT_ACCESS,
                            transition_after=cdk.Duration.days(st.derived_ia_days),
                        )
                    ],
                    abort_incomplete_multipart_upload_after=cdk.Duration.days(1),
                )
            ],
            removal_policy=cfg.removal_policy,
            auto_delete_objects=cfg.auto_delete_objects,
            server_access_logs_bucket=self.logs_bucket,
            server_access_logs_prefix="s3/derived/",
        )

        # Lectura desde CloudFront con Origin Access Control. La política se
        # escribe aquí, con el ARN de la distribución comodín, en lugar de dejar
        # que la cree el origen de CloudFront: si la creara `EdgeStack`, este
        # stack dependería de él y el de él de este (ciclo). El bucket sigue
        # siendo privado — solo CloudFront de ESTA cuenta puede leerlo.
        self.derived_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCloudFrontOacRead",
                principals=[iam.ServicePrincipal("cloudfront.amazonaws.com")],
                actions=["s3:GetObject"],
                resources=[self.derived_bucket.arn_for_objects("*")],
                conditions={
                    "StringLike": {
                        "AWS:SourceArn": f"arn:aws:cloudfront::{self.account}:distribution/*"
                    }
                },
            )
        )

    # ----------------------------------------------------------------- SQS --
    def _create_queues(self, cfg: EnvConfig) -> None:
        retention = cdk.Duration.days(14)

        self.ingest_dlq = sqs.Queue(
            self,
            "IngestDlq",
            queue_name=cfg.resource_name("ingest-dlq"),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            retention_period=retention,
        )
        self.ingest_queue = sqs.Queue(
            self,
            "IngestQueue",
            queue_name=cfg.resource_name("ingest"),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            # EXIF + previews + plate solving + embedding: minutos, no segundos.
            visibility_timeout=cdk.Duration.minutes(15),
            retention_period=retention,
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=5, queue=self.ingest_dlq),
        )

        self.reconstruct_dlq = sqs.Queue(
            self,
            "ReconstructDlq",
            queue_name=cfg.resource_name("reconstruct-dlq"),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            retention_period=retention,
        )
        self.reconstruct_queue = sqs.Queue(
            self,
            "ReconstructQueue",
            queue_name=cfg.resource_name("reconstruct"),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            # La Lambda dispatcher solo hace SubmitJob: es rápida.
            visibility_timeout=cdk.Duration.minutes(5),
            retention_period=retention,
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=self.reconstruct_dlq),
        )

    # ------------------------------------------------------------- Aurora --
    def _create_database(self, cfg: EnvConfig, vpc: ec2.IVpc) -> None:
        ac = cfg.aurora

        # El SG del cluster se crea aquí, en el mismo stack que la base de datos:
        # la rotación del secreto le añade su propia regla de entrada y eso, desde
        # otro stack, sería una dependencia circular.
        self.db_security_group = ec2.SecurityGroup(
            self,
            "DatabaseSg",
            vpc=vpc,
            security_group_name=cfg.resource_name("db"),
            description="Aurora Serverless v2 de astro-photos",
            allow_all_outbound=False,
        )
        self.db_security_group.add_ingress_rule(
            peer=ec2.Peer.ipv4(vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(5432),
            description="PostgreSQL desde dentro de la VPC (ECS, Batch, Lambda)",
        )

        parameter_group = rds.ParameterGroup(
            self,
            "ClusterParameters",
            engine=rds.DatabaseClusterEngine.aurora_postgres(version=AURORA_PG_VERSION),
            description=f"astro-photos {cfg.name}",
            parameters={
                # PostGIS y pgvector necesitan estar en la lista de precarga.
                "shared_preload_libraries": "pg_stat_statements",
                "log_min_duration_statement": "1000",
                "log_statement": "ddl",
            },
        )

        self.db_credentials = rds.Credentials.from_generated_secret(
            DB_USER,
            secret_name=f"/{cfg.prefix}/db/credentials",
        )

        writer = rds.ClusterInstance.serverless_v2(
            "writer",
            enable_performance_insights=ac.performance_insights,
            auto_minor_version_upgrade=True,
        )
        readers = [
            rds.ClusterInstance.serverless_v2(
                f"reader{i}",
                scale_with_writer=True,
                enable_performance_insights=ac.performance_insights,
            )
            for i in range(ac.readers)
        ]

        self.database = rds.DatabaseCluster(
            self,
            "Database",
            cluster_identifier=cfg.resource_name("db"),
            engine=rds.DatabaseClusterEngine.aurora_postgres(version=AURORA_PG_VERSION),
            parameter_group=parameter_group,
            credentials=self.db_credentials,
            default_database_name=DB_NAME,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            security_groups=[self.db_security_group],
            writer=writer,
            readers=readers,
            serverless_v2_min_capacity=ac.min_acu,
            serverless_v2_max_capacity=ac.max_acu,
            # Auto-pausa: en staging el cluster baja a 0 ACU y deja de facturar
            # computo si nadie lo toca. En prod es None (nunca se pausa).
            serverless_v2_auto_pause_duration=(
                cdk.Duration.minutes(ac.auto_pause_minutes)
                if ac.auto_pause_minutes is not None
                else None
            ),
            backup=rds.BackupProps(
                retention=cdk.Duration.days(ac.backup_retention_days),
                preferred_window="03:00-04:00",
            ),
            preferred_maintenance_window="Mon:04:30-Mon:05:30",
            storage_encrypted=True,
            deletion_protection=ac.deletion_protection,
            iam_authentication=True,
            cloudwatch_logs_exports=["postgresql"],
            cloudwatch_logs_retention=log_retention(cfg.log_retention_days),
            removal_policy=cfg.removal_policy,
        )

        assert self.database.secret is not None
        self.db_secret = self.database.secret

        # El SQL de extensiones se publica en SSM para que la tarea de bootstrap
        # lo lea sin tenerlo dentro de la imagen del backend.
        self.db_extensions_parameter = ssm.StringParameter(
            self,
            "DbExtensionsSql",
            parameter_name=f"/{cfg.prefix}/db/enable-extensions-sql",
            description="SQL idempotente que habilita PostGIS, pgvector, citext y pgcrypto",
            string_value=EXTENSIONS_SQL.read_text(encoding="utf-8"),
            tier=ssm.ParameterTier.STANDARD,
        )

    # --------------------------------------------------------- Secretos ----
    def _create_secrets(self, cfg: EnvConfig) -> None:
        # Secreto de aplicación: lo que no es la contraseña de la base de datos
        # (firma de URLs de CloudFront, pepper de idempotencia, webhook token).
        self.app_secret = secretsmanager.Secret(
            self,
            "AppSecret",
            secret_name=f"/{cfg.prefix}/app",
            description="Secretos de aplicacion de astro-photos (no incluye la BD)",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"webhook_token":""}',
                generate_string_key="app_secret_key",
                password_length=64,
                exclude_punctuation=True,
            ),
            removal_policy=cfg.removal_policy,
        )

        # Rotación automática de la contraseña de la base de datos en prod.
        if cfg.is_prod:
            self.database.add_rotation_single_user(
                automatically_after=cdk.Duration.days(30),
                exclude_characters=" %+~`#$&*()|[]{}:;<>?!'/@\"\\",
                # La Lambda de rotacion necesita salida a Secrets Manager: va en
                # las subredes con NAT, no en las aisladas del cluster.
                vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            )

    # ------------------------------------------------------------- utils ---
    def grant_data_access(self, grantee: iam.IGrantable) -> None:
        """Permisos de datos de un worker: leer originales, escribir derivados."""
        self.uploads_bucket.grant_read_write(grantee)
        self.originals_bucket.grant_read_write(grantee)
        self.derived_bucket.grant_read_write(grantee)

    def _outputs(self) -> None:
        outputs = {
            "UploadsBucketName": self.uploads_bucket.bucket_name,
            "OriginalsBucketName": self.originals_bucket.bucket_name,
            "DerivedBucketName": self.derived_bucket.bucket_name,
            "IngestQueueUrl": self.ingest_queue.queue_url,
            "ReconstructQueueUrl": self.reconstruct_queue.queue_url,
            "DbClusterEndpoint": self.database.cluster_endpoint.hostname,
            "DbSecretArn": self.db_secret.secret_arn,
            "DbExtensionsParameterName": self.db_extensions_parameter.parameter_name,
        }
        for name, value in outputs.items():
            cdk.CfnOutput(self, name, value=value)
