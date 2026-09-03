"""AWS Batch (GPU spot) y las dos Lambdas de la ruta de ingesta.

Por qué Batch y no SageMaker: ADR 0004. Lo que importa aquí es el coste — un
`g5.xlarge` on-demand son ~1 €/h, así que:

* `min_vcpus = 0`: **nunca** hay una instancia GPU encendida sin trabajo en cola.
  Esto no es negociable y hay un test que lo comprueba.
* `spot=True` con puja limitada y reintento ante `SPOT_INSTANCE_RECLAIMED`.
* `timeout` duro por job: un pipeline colgado no puede quemar el presupuesto.

La Lambda de verificación se engancha por **EventBridge** en lugar de con una
notificación S3 directa: la notificación se materializa en el stack del bucket
(`DataStack`), y como la Lambda vive aquí eso crearía una dependencia circular
entre stacks. El evento es el mismo `s3:ObjectCreated` y el filtro de prefijo
`staging/` se aplica en el patrón de la regla.
"""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_batch as batch
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as lambda_events
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from common import log_retention
from config import EnvConfig
from stacks.base import BaseStack
from stacks.data_stack import UPLOADS_STAGING_PREFIX

LAMBDAS_DIR = Path(__file__).resolve().parent.parent / "lambdas"

#: Etiqueta de la imagen de `models/` a usar. El CI la sobrescribe con el sha del
#: commit (`-c models_image_tag=sha-abc1234`) para que el despliegue sea exacto.
DEFAULT_IMAGE_TAG = "latest"


class ComputeStack(BaseStack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cfg: EnvConfig,
        vpc: ec2.IVpc,
        uploads_bucket: s3.IBucket,
        originals_bucket: s3.IBucket,
        derived_bucket: s3.IBucket,
        ingest_queue: sqs.IQueue,
        reconstruct_queue: sqs.IQueue,
        models_repository: ecr.IRepository,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, cfg=cfg, **kwargs)

        image_tag = self.node.try_get_context("models_image_tag") or DEFAULT_IMAGE_TAG

        self._create_batch(
            cfg, vpc, originals_bucket, derived_bucket, models_repository, str(image_tag)
        )
        self._create_dispatcher(cfg, reconstruct_queue, originals_bucket, derived_bucket)
        self._create_verifier(cfg, uploads_bucket, ingest_queue)
        self._outputs()

    # --------------------------------------------------------------- Batch --
    def _create_batch(
        self,
        cfg: EnvConfig,
        vpc: ec2.IVpc,
        originals_bucket: s3.IBucket,
        derived_bucket: s3.IBucket,
        models_repository: ecr.IRepository,
        image_tag: str,
    ) -> None:
        bc = cfg.batch

        self.batch_security_group = ec2.SecurityGroup(
            self,
            "BatchSg",
            vpc=vpc,
            security_group_name=cfg.resource_name("batch"),
            description="Instancias GPU de AWS Batch",
            allow_all_outbound=True,
        )

        self.compute_environment = batch.ManagedEc2EcsComputeEnvironment(
            self,
            "GpuSpot",
            compute_environment_name=cfg.resource_name("gpu-spot"),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[self.batch_security_group],
            instance_types=[ec2.InstanceType(t) for t in bc.instance_types],
            # Sin esto Batch añadiría toda la familia "optimal" (C/M/R) y podría
            # levantar instancias sin GPU para un job que pide `gpu=1`.
            use_optimal_instance_classes=False,
            images=[batch.EcsMachineImage(image_type=batch.EcsMachineImageType.ECS_AL2_NVIDIA)],
            spot=True,
            spot_bid_percentage=bc.spot_bid_percentage,
            allocation_strategy=batch.AllocationStrategy.SPOT_CAPACITY_OPTIMIZED,
            # ===== la regla de oro del coste =====
            minv_cpus=0,
            maxv_cpus=bc.max_vcpus,
            replace_compute_environment=False,
            update_to_latest_image_version=True,
            enabled=True,
        )

        self.job_queue = batch.JobQueue(
            self,
            "JobQueue",
            job_queue_name=cfg.resource_name("reconstruct"),
            priority=1,
            compute_environments=[
                batch.OrderedComputeEnvironment(
                    compute_environment=self.compute_environment, order=1
                )
            ],
        )

        # Rol del contenedor: solo los buckets que necesita, nada más.
        self.job_role = iam.Role(
            self,
            "JobRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="Rol del contenedor de reconstruccion (models/)",
        )
        originals_bucket.grant_read(self.job_role)
        derived_bucket.grant_read_write(self.job_role)

        self.job_log_group = logs.LogGroup(
            self,
            "BatchLogs",
            log_group_name=f"/aws/batch/{cfg.prefix}",
            retention=log_retention(cfg.log_retention_days),
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        image = ecs.ContainerImage.from_ecr_repository(models_repository, image_tag)
        common_container = dict(
            image=image,
            cpu=bc.job_vcpus,
            memory=cdk.Size.mebibytes(bc.job_memory_mib),
            gpu=bc.job_gpus,
            job_role=self.job_role,
            logging=ecs.LogDrivers.aws_logs(stream_prefix="job", log_group=self.job_log_group),
            environment={
                "ENVIRONMENT": cfg.name,
                "ORIGINALS_BUCKET": originals_bucket.bucket_name,
                "DERIVED_BUCKET": derived_bucket.bucket_name,
                # Reproducibilidad bit a bit: sin hilos no deterministas.
                "OMP_NUM_THREADS": "1",
                "PYTHONHASHSEED": "0",
            },
        )

        self.job_definition = batch.EcsJobDefinition(
            self,
            "ReconstructJob",
            job_definition_name=cfg.resource_name("reconstruct"),
            container=batch.EcsEc2ContainerDefinition(
                self, "ReconstructContainer", **common_container
            ),
            retry_attempts=bc.retry_attempts,
            retry_strategies=[
                # Que nos quiten la spot es normal y se reintenta.
                batch.RetryStrategy.of(batch.Action.RETRY, batch.Reason.SPOT_INSTANCE_RECLAIMED),
                # Que la imagen no exista no se arregla reintentando.
                batch.RetryStrategy.of(batch.Action.EXIT, batch.Reason.CANNOT_PULL_CONTAINER),
                batch.RetryStrategy.of(batch.Action.EXIT, batch.Reason.NON_ZERO_EXIT_CODE),
            ],
            timeout=cdk.Duration.hours(bc.job_timeout_hours),
            propagate_tags=True,
        )

        # Entrenamiento: mismo compute environment, otra definición (más memoria,
        # timeout más largo). La lanza `.github/workflows/train-model.yml`.
        training_container = dict(common_container)
        training_container["memory"] = cdk.Size.mebibytes(int(bc.job_memory_mib))
        self.training_job_definition = batch.EcsJobDefinition(
            self,
            "TrainingJob",
            job_definition_name=cfg.resource_name("train"),
            container=batch.EcsEc2ContainerDefinition(
                self, "TrainingContainer", **training_container
            ),
            retry_attempts=1,
            retry_strategies=[
                batch.RetryStrategy.of(batch.Action.RETRY, batch.Reason.SPOT_INSTANCE_RECLAIMED)
            ],
            timeout=cdk.Duration.hours(bc.job_timeout_hours * 4),
            propagate_tags=True,
        )

    # ---------------------------------------------------------- dispatcher --
    def _create_dispatcher(
        self,
        cfg: EnvConfig,
        reconstruct_queue: sqs.IQueue,
        originals_bucket: s3.IBucket,
        derived_bucket: s3.IBucket,
    ) -> None:
        self.dispatcher = lambda_.Function(
            self,
            "Dispatcher",
            function_name=cfg.resource_name("dispatcher"),
            description="Consume la cola reconstruct y lanza el job de AWS Batch",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="index.handler",
            code=lambda_.Code.from_asset(str(LAMBDAS_DIR / "dispatcher")),
            timeout=cdk.Duration.seconds(30),
            memory_size=256,
            log_group=logs.LogGroup(
                self,
                "DispatcherLogs",
                log_group_name=f"/aws/lambda/{cfg.resource_name('dispatcher')}",
                retention=log_retention(cfg.log_retention_days),
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
            environment={
                "ENVIRONMENT": cfg.name,
                "BATCH_JOB_QUEUE": self.job_queue.job_queue_arn,
                "BATCH_JOB_DEFINITION": self.job_definition.job_definition_arn,
                "ORIGINALS_BUCKET": originals_bucket.bucket_name,
                "DERIVED_BUCKET": derived_bucket.bucket_name,
                "LOG_LEVEL": "INFO",
            },
        )
        self.dispatcher.add_to_role_policy(
            iam.PolicyStatement(
                actions=["batch:SubmitJob", "batch:TagResource"],
                resources=[
                    self.job_queue.job_queue_arn,
                    self.job_definition.job_definition_arn,
                    # Batch versiona las job definitions: hay que permitir todas.
                    f"{self.job_definition.job_definition_arn}:*",
                ],
            )
        )
        self.dispatcher.add_event_source(
            lambda_events.SqsEventSource(
                reconstruct_queue,
                batch_size=1,
                report_batch_item_failures=False,
                max_concurrency=2 if not cfg.is_prod else 10,
            )
        )

    # ------------------------------------------------------------ verifier --
    def _create_verifier(
        self, cfg: EnvConfig, uploads_bucket: s3.IBucket, ingest_queue: sqs.IQueue
    ) -> None:
        self.verifier = lambda_.Function(
            self,
            "UploadVerifier",
            function_name=cfg.resource_name("verify-upload"),
            description="Verifica tamano y magic bytes de cada objeto nuevo en uploads/staging/",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="index.handler",
            code=lambda_.Code.from_asset(str(LAMBDAS_DIR / "verify")),
            timeout=cdk.Duration.seconds(30),
            memory_size=256,
            log_group=logs.LogGroup(
                self,
                "VerifierLogs",
                log_group_name=f"/aws/lambda/{cfg.resource_name('verify-upload')}",
                retention=log_retention(cfg.log_retention_days),
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
            environment={
                "ENVIRONMENT": cfg.name,
                "INGEST_QUEUE_URL": ingest_queue.queue_url,
                "MAX_UPLOAD_BYTES": str(512 * 1024 * 1024),
                "LOG_LEVEL": "INFO",
            },
        )
        uploads_bucket.grant_read(self.verifier)
        ingest_queue.grant_send_messages(self.verifier)

        self.upload_rule = events.Rule(
            self,
            "UploadCreatedRule",
            rule_name=cfg.resource_name("upload-created"),
            description="s3:ObjectCreated sobre el prefijo staging/ del bucket de uploads",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={
                    # Nombre literal, no la referencia entre stacks: un patron de
                    # eventos con `Fn::ImportValue` funciona pero es ilegible en la
                    # consola y crea un export innecesario. El nombre es
                    # determinista porque lo fija `EnvConfig`.
                    "bucket": {"name": [cfg.resource_name("uploads")]},
                    "object": {"key": [{"prefix": UPLOADS_STAGING_PREFIX}]},
                },
            ),
            targets=[
                events_targets.LambdaFunction(
                    self.verifier,
                    retry_attempts=2,
                )
            ],
        )

    # ------------------------------------------------------------- outputs --
    def _outputs(self) -> None:
        outputs = {
            "BatchJobQueueArn": self.job_queue.job_queue_arn,
            "BatchJobQueueName": self.job_queue.job_queue_name,
            "ReconstructJobDefinitionArn": self.job_definition.job_definition_arn,
            "TrainingJobDefinitionArn": self.training_job_definition.job_definition_arn,
            "DispatcherFunctionName": self.dispatcher.function_name,
            "VerifierFunctionName": self.verifier.function_name,
        }
        for name, value in outputs.items():
            cdk.CfnOutput(self, name, value=value)
