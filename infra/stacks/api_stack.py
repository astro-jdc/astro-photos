"""ECS Fargate + ALB + CodeDeploy blue/green + tarea de migraciones.

Puntos que no son negociables (`.claude/agents/infra-dev.md`):

* **Las migraciones son una tarea previa**, no el arranque del contenedor. Aquí se
  define `MigrationsTaskDefinition`; el workflow de despliegue la corre con
  `aws ecs run-task` y espera a que termine ANTES de mover tráfico.
* **Blue/green con rollback automático**: dos target groups, un listener de
  producción y otro de pruebas, y las alarmas de 5xx y de latencia p99 conectadas
  al deployment group. Si saltan durante el despliegue, CodeDeploy revierte.
* El ALB solo habla HTTPS; el 80 redirige.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_applicationautoscaling as appscaling
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_codedeploy as codedeploy
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as route53_targets
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from common import hosted_zone, log_retention
from config import EnvConfig
from stacks.base import BaseStack
from stacks.data_stack import DB_NAME, EXTENSIONS_SQL

#: Puerto del contenedor de FastAPI (uvicorn).
CONTAINER_PORT = 8000
#: Listener de pruebas del despliegue blue/green: CodeDeploy valida aquí la
#: versión nueva antes de moverle el tráfico real.
TEST_LISTENER_PORT = 8443

DEFAULT_IMAGE_TAG = "latest"

#: Imagen usada solo para ejecutar el SQL de extensiones. Es un cliente psql, no
#: hay servidor: no cuesta nada y evita meter psql en la imagen del backend.
PSQL_IMAGE = "public.ecr.aws/docker/library/postgres:16-alpine"


class ApiStack(BaseStack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cfg: EnvConfig,
        vpc: ec2.IVpc,
        database: rds.IDatabaseCluster,
        db_secret: secretsmanager.ISecret,
        app_secret: secretsmanager.ISecret,
        uploads_bucket: s3.IBucket,
        originals_bucket: s3.IBucket,
        derived_bucket: s3.IBucket,
        ingest_queue: sqs.IQueue,
        reconstruct_queue: sqs.IQueue,
        user_pool: cognito.IUserPool,
        user_pool_client: cognito.IUserPoolClient,
        backend_repository: ecr.IRepository,
        db_extensions_parameter: ssm.IStringParameter,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, cfg=cfg, **kwargs)

        image_tag = str(self.node.try_get_context("backend_image_tag") or DEFAULT_IMAGE_TAG)
        self.image = ecs.ContainerImage.from_ecr_repository(backend_repository, image_tag)

        # ------------------------------------------------------------ cluster
        self.cluster = ecs.Cluster(
            self,
            "Cluster",
            cluster_name=cfg.resource_name("cluster"),
            vpc=vpc,
            container_insights_v2=(
                ecs.ContainerInsights.ENABLED if cfg.is_prod else ecs.ContainerInsights.DISABLED
            ),
            enable_fargate_capacity_providers=True,
        )

        self.log_group = logs.LogGroup(
            self,
            "ApiLogs",
            log_group_name=f"/astro-photos/{cfg.name}/api",
            retention=log_retention(cfg.log_retention_days),
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # -------------------------------------------------------------- roles
        self.task_role = iam.Role(
            self,
            "TaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="Rol de la API de astro-photos",
        )
        uploads_bucket.grant_read_write(self.task_role)
        originals_bucket.grant_read_write(self.task_role)
        derived_bucket.grant_read(self.task_role)
        ingest_queue.grant_send_messages(self.task_role)
        reconstruct_queue.grant_send_messages(self.task_role)
        db_secret.grant_read(self.task_role)
        app_secret.grant_read(self.task_role)
        # El backend solo lee la configuración del pool para validar JWT.
        self.task_role.add_to_policy(
            iam.PolicyStatement(
                actions=["cognito-idp:AdminGetUser", "cognito-idp:ListUsersInGroup"],
                resources=[user_pool.user_pool_arn],
            )
        )
        # Tracing OpenTelemetry -> X-Ray (`docs/architecture.md`).
        self.task_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("AWSXRayDaemonWriteAccess")
        )

        self.container_environment = {
            "ENVIRONMENT": cfg.name,
            "LOG_LEVEL": "INFO" if cfg.is_prod else "DEBUG",
            "AUTH_MODE": "cognito",
            "S3_BUCKET_UPLOADS": uploads_bucket.bucket_name,
            "S3_BUCKET_ORIGINALS": originals_bucket.bucket_name,
            "S3_BUCKET_DERIVED": derived_bucket.bucket_name,
            "S3_REGION": self.region,
            "SQS_QUEUE_INGEST": ingest_queue.queue_url,
            "SQS_QUEUE_RECONSTRUCT": reconstruct_queue.queue_url,
            "COGNITO_USER_POOL_ID": user_pool.user_pool_id,
            "COGNITO_CLIENT_ID": user_pool_client.user_pool_client_id,
            "COGNITO_REGION": self.region,
            "DB_HOST": database.cluster_endpoint.hostname,
            "DB_PORT": str(database.cluster_endpoint.port),
            "DB_NAME": DB_NAME,
            "PUBLIC_BASE_URL": f"https://{cfg.domain_name}",
        }
        self.container_secrets = {
            "DB_USER": ecs.Secret.from_secrets_manager(db_secret, "username"),
            "DB_PASSWORD": ecs.Secret.from_secrets_manager(db_secret, "password"),
            "APP_SECRET_KEY": ecs.Secret.from_secrets_manager(app_secret, "app_secret_key"),
        }

        self._create_service(cfg, vpc)
        self._create_load_balancer(cfg, vpc)
        self._create_alarms(cfg)
        self._create_blue_green(cfg)
        self._create_autoscaling(cfg, ingest_queue)
        self._create_worker(cfg, vpc, ingest_queue)
        self._create_migrations_task(cfg, db_extensions_parameter)
        self._outputs()

    # ---------------------------------------------------------------- API --
    def _create_service(self, cfg: EnvConfig, vpc: ec2.IVpc) -> None:
        fc = cfg.fargate

        self.task_definition = ecs.FargateTaskDefinition(
            self,
            "ApiTask",
            family=cfg.resource_name("api"),
            cpu=fc.cpu,
            memory_limit_mib=fc.memory_mib,
            task_role=self.task_role,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )
        self.task_definition.add_container(
            "api",
            image=self.image,
            essential=True,
            environment=self.container_environment,
            secrets=self.container_secrets,
            port_mappings=[ecs.PortMapping(container_port=CONTAINER_PORT, name="http")],
            logging=ecs.LogDrivers.aws_logs(stream_prefix="api", log_group=self.log_group),
            health_check=ecs.HealthCheck(
                # /healthz es liveness: no toca la base de datos ni S3.
                command=[
                    "CMD-SHELL",
                    f"curl -fsS http://localhost:{CONTAINER_PORT}/healthz || exit 1",
                ],
                interval=cdk.Duration.seconds(30),
                timeout=cdk.Duration.seconds(5),
                retries=3,
                start_period=cdk.Duration.seconds(30),
            ),
        )

        self.service_security_group = ec2.SecurityGroup(
            self,
            "ApiSg",
            vpc=vpc,
            security_group_name=cfg.resource_name("api"),
            description="Tareas Fargate de la API",
            allow_all_outbound=True,
        )

        self.service = ecs.FargateService(
            self,
            "ApiService",
            service_name=cfg.resource_name("api"),
            cluster=self.cluster,
            task_definition=self.task_definition,
            desired_count=cfg.fargate.desired_count,
            assign_public_ip=False,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[self.service_security_group],
            # Blue/green: quien orquesta el despliegue es CodeDeploy, no ECS.
            deployment_controller=ecs.DeploymentController(
                type=ecs.DeploymentControllerType.CODE_DEPLOY
            ),
            health_check_grace_period=cdk.Duration.seconds(60),
            enable_execute_command=not cfg.is_prod,
            min_healthy_percent=100,
            max_healthy_percent=200,
        )
        # La salida del SG de la API está abierta y el SG de Aurora ya admite el
        # CIDR de la VPC en el 5432 (network_stack): no hace falta tocar el SG de
        # otro stack, que solo añadiría acoplamiento entre despliegues.

    # -------------------------------------------------------------- ALB ----
    def _create_load_balancer(self, cfg: EnvConfig, vpc: ec2.IVpc) -> None:
        self.zone = hosted_zone(self, cfg)

        self.api_certificate = acm.Certificate(
            self,
            "ApiCertificate",
            domain_name=cfg.api_domain_name,
            validation=(
                acm.CertificateValidation.from_dns(self.zone)
                if self.zone is not None
                # Sin zona configurada la validación es manual: ACM publica los
                # registros CNAME a crear y el stack espera. Ver infra/README.md.
                else acm.CertificateValidation.from_dns()
            ),
        )

        self.load_balancer = elbv2.ApplicationLoadBalancer(
            self,
            "Alb",
            load_balancer_name=cfg.resource_name("alb")[:32],
            vpc=vpc,
            internet_facing=True,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            idle_timeout=cdk.Duration.seconds(120),
            drop_invalid_header_fields=True,
            deletion_protection=cfg.is_prod,
        )

        target_group_props = dict(
            vpc=vpc,
            port=CONTAINER_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            deregistration_delay=cdk.Duration.seconds(30),
            health_check=elbv2.HealthCheck(
                # /readyz comprueba DB + S3 + cola: es lo que decide si una tarea
                # nueva puede recibir tráfico real (`docs/api.md`).
                path="/readyz",
                healthy_http_codes="200",
                interval=cdk.Duration.seconds(15),
                timeout=cdk.Duration.seconds(5),
                healthy_threshold_count=2,
                unhealthy_threshold_count=3,
            ),
        )
        self.blue_target_group = elbv2.ApplicationTargetGroup(
            self,
            "BlueTargetGroup",
            target_group_name=cfg.resource_name("blue")[:32],
            **target_group_props,
        )
        self.green_target_group = elbv2.ApplicationTargetGroup(
            self,
            "GreenTargetGroup",
            target_group_name=cfg.resource_name("green")[:32],
            **target_group_props,
        )

        self.load_balancer.add_listener(
            "HttpRedirect",
            port=80,
            protocol=elbv2.ApplicationProtocol.HTTP,
            default_action=elbv2.ListenerAction.redirect(
                protocol="HTTPS", port="443", permanent=True
            ),
        )

        self.https_listener = self.load_balancer.add_listener(
            "Https",
            port=443,
            protocol=elbv2.ApplicationProtocol.HTTPS,
            certificates=[self.api_certificate],
            ssl_policy=elbv2.SslPolicy.TLS13_RES,
            default_target_groups=[self.blue_target_group],
        )
        self.test_listener = self.load_balancer.add_listener(
            "HttpsTest",
            port=TEST_LISTENER_PORT,
            protocol=elbv2.ApplicationProtocol.HTTPS,
            certificates=[self.api_certificate],
            ssl_policy=elbv2.SslPolicy.TLS13_RES,
            default_target_groups=[self.green_target_group],
            open=False,
        )

        self.service.attach_to_application_target_group(self.blue_target_group)

        if self.zone is not None:
            route53.ARecord(
                self,
                "ApiAliasRecord",
                zone=self.zone,
                record_name=cfg.api_domain_name,
                target=route53.RecordTarget.from_alias(
                    route53_targets.LoadBalancerTarget(self.load_balancer)
                ),
                comment="astro-photos API (origen de CloudFront para /api/*)",
            )

    # ----------------------------------------------------------- alarmas ---
    def _create_alarms(self, cfg: EnvConfig) -> None:
        fc = cfg.fargate

        # 5xx del propio ALB + 5xx del target: si el despliegue nuevo se cae,
        # cualquiera de las dos lo ve.
        self.alarm_5xx = cloudwatch.Alarm(
            self,
            "Alb5xxAlarm",
            alarm_name=cfg.resource_name("alb-5xx"),
            alarm_description="5xx en el ALB de la API; dispara el rollback de CodeDeploy",
            metric=cloudwatch.MathExpression(
                expression="elb5xx + target5xx",
                using_metrics={
                    "elb5xx": self.load_balancer.metrics.http_code_elb(
                        elbv2.HttpCodeElb.ELB_5XX_COUNT, statistic="Sum"
                    ),
                    "target5xx": self.load_balancer.metrics.http_code_target(
                        elbv2.HttpCodeTarget.TARGET_5XX_COUNT, statistic="Sum"
                    ),
                },
                period=cdk.Duration.minutes(1),
                label="5xx totales",
            ),
            threshold=fc.alb_5xx_threshold,
            evaluation_periods=2,
            datapoints_to_alarm=2,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

        self.alarm_latency = cloudwatch.Alarm(
            self,
            "ApiLatencyAlarm",
            alarm_name=cfg.resource_name("api-p99"),
            alarm_description="Latencia p99 de la API por encima del objetivo",
            metric=self.load_balancer.metrics.target_response_time(
                statistic="p99", period=cdk.Duration.minutes(1)
            ),
            threshold=fc.p99_latency_seconds,
            evaluation_periods=3,
            datapoints_to_alarm=2,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

        self.alarm_unhealthy_hosts = cloudwatch.Alarm(
            self,
            "UnhealthyHostsAlarm",
            alarm_name=cfg.resource_name("api-unhealthy"),
            alarm_description="Tareas de la API fuera de servicio en el target group activo",
            metric=self.blue_target_group.metrics.unhealthy_host_count(
                period=cdk.Duration.minutes(1), statistic="Maximum"
            ),
            threshold=0,
            evaluation_periods=3,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

    # ------------------------------------------------------- blue / green --
    def _create_blue_green(self, cfg: EnvConfig) -> None:
        application = codedeploy.EcsApplication(
            self, "CodeDeployApp", application_name=cfg.resource_name("api")
        )

        self.deployment_group = codedeploy.EcsDeploymentGroup(
            self,
            "BlueGreen",
            deployment_group_name=cfg.resource_name("api"),
            application=application,
            service=self.service,
            blue_green_deployment_config=codedeploy.EcsBlueGreenDeploymentConfig(
                blue_target_group=self.blue_target_group,
                green_target_group=self.green_target_group,
                listener=self.https_listener,
                test_listener=self.test_listener,
                # Ventana para abortar a mano antes de mover el trafico.
                deployment_approval_wait_time=cdk.Duration.minutes(0),
                # La version antigua sigue viva 10 minutos: es la ventana en la
                # que las alarmas pueden disparar el rollback (docs/branching.md).
                termination_wait_time=cdk.Duration.minutes(10),
            ),
            deployment_config=(
                codedeploy.EcsDeploymentConfig.CANARY_10_PERCENT_5_MINUTES
                if cfg.is_prod
                else codedeploy.EcsDeploymentConfig.ALL_AT_ONCE
            ),
            alarms=[self.alarm_5xx, self.alarm_latency, self.alarm_unhealthy_hosts],
            auto_rollback=codedeploy.AutoRollbackConfig(
                failed_deployment=True,
                stopped_deployment=True,
                deployment_in_alarm=True,
            ),
        )

    # -------------------------------------------------------- autoescalado --
    def _create_autoscaling(self, cfg: EnvConfig, ingest_queue: sqs.IQueue) -> None:
        fc = cfg.fargate

        self.scaling = self.service.auto_scale_task_count(
            min_capacity=fc.min_tasks, max_capacity=fc.max_tasks
        )
        self.scaling.scale_on_cpu_utilization(
            "CpuScaling",
            target_utilization_percent=fc.cpu_target_percent,
            scale_in_cooldown=cdk.Duration.minutes(5),
            scale_out_cooldown=cdk.Duration.minutes(1),
        )
        # Escalado por profundidad de cola: cuando la ingesta se acumula, la API
        # también recibe más callbacks y más polling del frontend.
        self.scaling.scale_on_metric(
            "QueueDepthScaling",
            metric=ingest_queue.metric_approximate_number_of_messages_visible(
                statistic="Maximum", period=cdk.Duration.minutes(1)
            ),
            scaling_steps=[
                appscaling.ScalingInterval(upper=0, change=-1),
                appscaling.ScalingInterval(lower=fc.queue_messages_per_task, change=+1),
                appscaling.ScalingInterval(lower=fc.queue_messages_per_task * 5, change=+2),
            ],
            adjustment_type=appscaling.AdjustmentType.CHANGE_IN_CAPACITY,
            cooldown=cdk.Duration.minutes(2),
        )

    # ------------------------------------------------- worker de ingesta ----
    def _create_worker(self, cfg: EnvConfig, vpc: ec2.IVpc, ingest_queue: sqs.IQueue) -> None:
        """Consumidor de la cola `ingest` (EXIF, previews, plate solve, embedding).

        Despliegue rolling normal: no hay tráfico de usuario que cortar, así que
        no necesita blue/green. En staging va en Fargate Spot.
        """
        fc = cfg.fargate

        worker_task = ecs.FargateTaskDefinition(
            self,
            "IngestWorkerTask",
            family=cfg.resource_name("ingest-worker"),
            cpu=fc.cpu,
            memory_limit_mib=fc.memory_mib,
            task_role=self.task_role,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )
        worker_task.add_container(
            "worker",
            image=self.image,
            essential=True,
            command=["python", "-m", "app.workers.ingest"],
            environment=self.container_environment,
            secrets=self.container_secrets,
            logging=ecs.LogDrivers.aws_logs(stream_prefix="ingest", log_group=self.log_group),
        )

        capacity_provider_strategies = [
            ecs.CapacityProviderStrategy(
                capacity_provider="FARGATE_SPOT", weight=fc.spot_weight
            ),
            ecs.CapacityProviderStrategy(
                capacity_provider="FARGATE", weight=max(100 - fc.spot_weight, 0), base=0
            ),
        ]

        self.worker_service = ecs.FargateService(
            self,
            "IngestWorkerService",
            service_name=cfg.resource_name("ingest-worker"),
            cluster=self.cluster,
            task_definition=worker_task,
            desired_count=1,
            assign_public_ip=False,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[self.service_security_group],
            capacity_provider_strategies=capacity_provider_strategies,
            # Sin esto un despliegue con el contenedor roto tarda hasta 3 h en
            # darse por fallido.
            circuit_breaker=ecs.DeploymentCircuitBreaker(enable=True, rollback=True),
            min_healthy_percent=0,
            max_healthy_percent=200,
            enable_execute_command=not cfg.is_prod,
        )
        worker_scaling = self.worker_service.auto_scale_task_count(
            min_capacity=1, max_capacity=fc.max_tasks
        )
        worker_scaling.scale_on_metric(
            "IngestQueueScaling",
            metric=ingest_queue.metric_approximate_number_of_messages_visible(
                statistic="Maximum", period=cdk.Duration.minutes(1)
            ),
            scaling_steps=[
                appscaling.ScalingInterval(upper=0, change=-1),
                appscaling.ScalingInterval(lower=fc.queue_messages_per_task, change=+1),
                appscaling.ScalingInterval(lower=fc.queue_messages_per_task * 4, change=+3),
            ],
            adjustment_type=appscaling.AdjustmentType.CHANGE_IN_CAPACITY,
            cooldown=cdk.Duration.minutes(2),
        )

    # ------------------------------------------------------- migraciones ----
    def _create_migrations_task(
        self, cfg: EnvConfig, db_extensions_parameter: ssm.IStringParameter
    ) -> None:
        """Tarea de un solo uso: extensiones + `alembic upgrade head`.

        Dos contenedores en la misma task definition:

        1. `db-bootstrap` (imagen `postgres:16-alpine`) aplica el SQL idempotente
           de `infra/scripts/enable_extensions.sql`. `essential=False` para poder
           declarar el `dependsOn ... COMPLETE`.
        2. `migrate` (imagen del backend) corre Alembic cuando el anterior termina.

        No hay servicio: la lanza el workflow con `aws ecs run-task` y espera.
        """
        self.migrations_log_group = logs.LogGroup(
            self,
            "MigrationsLogs",
            log_group_name=f"/astro-photos/{cfg.name}/migrations",
            retention=log_retention(cfg.log_retention_days),
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        self.migrations_task_definition = ecs.FargateTaskDefinition(
            self,
            "MigrationsTask",
            family=cfg.resource_name("migrations"),
            cpu=512,
            memory_limit_mib=1024,
            task_role=self.task_role,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )

        bootstrap = self.migrations_task_definition.add_container(
            "db-bootstrap",
            image=ecs.ContainerImage.from_registry(PSQL_IMAGE),
            essential=False,
            environment={**self.container_environment, "PGCONNECT_TIMEOUT": "10"},
            secrets=self.container_secrets,
            command=[
                "sh",
                "-lc",
                # PGPASSWORD/PGUSER se derivan del secreto ya inyectado.
                'PGPASSWORD="$DB_PASSWORD" psql -v ON_ERROR_STOP=1 '
                '-h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" '
                f"-c \"{self._inline_sql()}\"",
            ],
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="bootstrap", log_group=self.migrations_log_group
            ),
        )

        migrate = self.migrations_task_definition.add_container(
            "migrate",
            image=self.image,
            essential=True,
            environment=self.container_environment,
            secrets=self.container_secrets,
            command=["alembic", "upgrade", "head"],
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="migrate", log_group=self.migrations_log_group
            ),
        )
        migrate.add_container_dependencies(
            ecs.ContainerDependency(
                container=bootstrap, condition=ecs.ContainerDependencyCondition.COMPLETE
            )
        )

        # El SQL también queda en SSM para poder aplicarlo a mano desde un bastion.
        db_extensions_parameter.grant_read(self.task_role)

    @staticmethod
    def _inline_sql() -> str:
        """El SQL de extensiones en una línea, apto para `psql -c`."""
        lines = [
            line.strip()
            for line in EXTENSIONS_SQL.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("--")
        ]
        return " ".join(lines).replace('"', "'")

    # ---------------------------------------------------------- outputs ----
    def _outputs(self) -> None:
        outputs = {
            "AlbDnsName": self.load_balancer.load_balancer_dns_name,
            "ClusterName": self.cluster.cluster_name,
            "ApiServiceName": self.service.service_name,
            "WorkerServiceName": self.worker_service.service_name,
            "MigrationsTaskDefinitionArn": self.migrations_task_definition.task_definition_arn,
            "MigrationsSubnets": ",".join(
                self.cluster.vpc.select_subnets(
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
                ).subnet_ids
            ),
            "MigrationsSecurityGroup": self.service_security_group.security_group_id,
            "CodeDeployApplication": self.deployment_group.application.application_name,
            "CodeDeployDeploymentGroup": self.deployment_group.deployment_group_name,
        }
        for name, value in outputs.items():
            cdk.CfnOutput(self, name, value=value)
