"""Log groups, dashboard, alarmas, SNS y AWS Budgets.

La regla que gobierna este stack: **una alarma que nadie mira es ruido**. Solo hay
alarmas para cosas ante las que alguien haría algo:

* 5xx y latencia p99 → algo se rompió (además dispara el rollback en `api_stack`).
* profundidad de las DLQ > 0 → hay trabajo perdido que hay que reprocesar.
* jobs de Batch fallidos → una reconstrucción no salió y el usuario está esperando.
* CPU de Aurora → la base de datos se está quedando corta.
* presupuesto al 80 % → alguien dejó algo encendido.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as sns_subs
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from common import log_retention
from config import EnvConfig
from stacks.base import BaseStack


class ObservabilityStack(BaseStack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cfg: EnvConfig,
        database: rds.IDatabaseCluster,
        queues: list[sqs.IQueue],
        dead_letter_queues: list[sqs.IQueue],
        load_balancer: elbv2.IApplicationLoadBalancer,
        target_group: elbv2.IApplicationTargetGroup,
        service: ecs.IBaseService,
        job_queue_name: str,
        distribution: cloudfront.IDistribution,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, cfg=cfg, **kwargs)
        self.alarms: list[cloudwatch.Alarm] = []

        self.topic = self._create_topic(cfg)
        self._create_log_groups(cfg)
        self._create_alarms(
            cfg, database, dead_letter_queues, load_balancer, service, job_queue_name
        )
        self._create_dashboard(
            cfg,
            database,
            queues,
            dead_letter_queues,
            load_balancer,
            target_group,
            service,
            distribution,
        )
        self._create_budget(cfg)

        cdk.CfnOutput(self, "AlarmTopicArn", value=self.topic.topic_arn)

    # -------------------------------------------------------------- SNS ----
    def _create_topic(self, cfg: EnvConfig) -> sns.Topic:
        topic = sns.Topic(
            self,
            "AlarmTopic",
            topic_name=cfg.resource_name("alarms"),
            display_name=f"astro-photos {cfg.name}",
            enforce_ssl=True,
        )
        for email in cfg.alert_emails:
            topic.add_subscription(sns_subs.EmailSubscription(email))

        # AWS Budgets publica desde su propio servicio: sin esto la alerta de
        # presupuesto se crea pero nunca llega a nadie.
        topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowBudgetsPublish",
                principals=[iam.ServicePrincipal("budgets.amazonaws.com")],
                actions=["SNS:Publish"],
                resources=[topic.topic_arn],
            )
        )
        return topic

    # -------------------------------------------------------- log groups ---
    def _create_log_groups(self, cfg: EnvConfig) -> None:
        """Log groups transversales; los de cada servicio viven en su stack."""
        self.audit_log_group = logs.LogGroup(
            self,
            "AuditLogs",
            # Auditoría de descargas y cambios de licencia: procedencia, no debug.
            log_group_name=f"/astro-photos/{cfg.name}/audit",
            retention=logs.RetentionDays.ONE_YEAR if cfg.is_prod else logs.RetentionDays.ONE_MONTH,
            removal_policy=cfg.removal_policy,
        )
        self.pipeline_log_group = logs.LogGroup(
            self,
            "PipelineLogs",
            log_group_name=f"/astro-photos/{cfg.name}/pipelines",
            retention=log_retention(cfg.log_retention_days),
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

    # ----------------------------------------------------------- alarmas ---
    def _alarm(self, alarm: cloudwatch.Alarm) -> cloudwatch.Alarm:
        alarm.add_alarm_action(cw_actions.SnsAction(self.topic))
        alarm.add_ok_action(cw_actions.SnsAction(self.topic))
        self.alarms.append(alarm)
        return alarm

    def _create_alarms(
        self,
        cfg: EnvConfig,
        database: rds.IDatabaseCluster,
        dead_letter_queues: list[sqs.IQueue],
        load_balancer: elbv2.IApplicationLoadBalancer,
        service: ecs.IBaseService,
        job_queue_name: str,
    ) -> None:
        # --- API ---
        self._alarm(
            cloudwatch.Alarm(
                self,
                "Api5xxAlarm",
                alarm_name=cfg.resource_name("obs-api-5xx"),
                alarm_description="5xx sostenidos en la API",
                metric=load_balancer.metrics.http_code_target(
                    elbv2.HttpCodeTarget.TARGET_5XX_COUNT,
                    statistic="Sum",
                    period=cdk.Duration.minutes(5),
                ),
                threshold=cfg.fargate.alb_5xx_threshold,
                evaluation_periods=2,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
        )
        self._alarm(
            cloudwatch.Alarm(
                self,
                "ApiLatencyAlarm",
                alarm_name=cfg.resource_name("obs-api-p99"),
                alarm_description="Latencia p99 por encima del objetivo durante 15 minutos",
                metric=load_balancer.metrics.target_response_time(
                    statistic="p99", period=cdk.Duration.minutes(5)
                ),
                threshold=cfg.fargate.p99_latency_seconds,
                evaluation_periods=3,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
        )

        # --- colas muertas ---
        for dlq in dead_letter_queues:
            name = cdk.Names.unique_id(dlq.node.default_child) if dlq.node.default_child else "Dlq"
            self._alarm(
                cloudwatch.Alarm(
                    self,
                    f"DlqDepth{dlq.node.id}",
                    alarm_name=cfg.resource_name("dlq", dlq.node.id.lower()),
                    alarm_description=f"Mensajes en la DLQ {name}: hay trabajo perdido",
                    metric=dlq.metric_approximate_number_of_messages_visible(
                        statistic="Maximum", period=cdk.Duration.minutes(5)
                    ),
                    # Un solo mensaje en una DLQ ya merece que alguien mire.
                    threshold=0,
                    evaluation_periods=1,
                    comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                    treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                )
            )

        # --- AWS Batch ---
        self.batch_failed_metric = cloudwatch.Metric(
            namespace="AWS/Batch",
            metric_name="FailedJobCount",
            dimensions_map={"JobQueue": job_queue_name},
            statistic="Sum",
            period=cdk.Duration.minutes(15),
        )
        self._alarm(
            cloudwatch.Alarm(
                self,
                "BatchFailuresAlarm",
                alarm_name=cfg.resource_name("batch-failed"),
                alarm_description="Jobs de reconstruccion fallidos en AWS Batch",
                metric=self.batch_failed_metric,
                threshold=2,
                evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
        )

        # --- Aurora ---
        self._alarm(
            cloudwatch.Alarm(
                self,
                "AuroraCpuAlarm",
                alarm_name=cfg.resource_name("aurora-cpu"),
                alarm_description="CPU del cluster Aurora alta de forma sostenida",
                metric=database.metric_cpu_utilization(
                    statistic="Average", period=cdk.Duration.minutes(5)
                ),
                threshold=cfg.aurora.cpu_alarm_threshold,
                evaluation_periods=3,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
        )
        self._alarm(
            cloudwatch.Alarm(
                self,
                "AuroraConnectionsAlarm",
                alarm_name=cfg.resource_name("aurora-connections"),
                alarm_description="Conexiones a Aurora cerca del limite del pool",
                metric=database.metric_database_connections(
                    statistic="Maximum", period=cdk.Duration.minutes(5)
                ),
                threshold=100 if cfg.is_prod else 40,
                evaluation_periods=3,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
        )

        # --- servicio ---
        self._alarm(
            cloudwatch.Alarm(
                self,
                "ApiCpuAlarm",
                alarm_name=cfg.resource_name("api-cpu"),
                alarm_description="CPU de las tareas de la API al limite del autoescalado",
                metric=service.metric_cpu_utilization(
                    statistic="Average", period=cdk.Duration.minutes(5)
                ),
                threshold=90,
                evaluation_periods=3,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
        )

    # --------------------------------------------------------- dashboard ---
    def _create_dashboard(
        self,
        cfg: EnvConfig,
        database: rds.IDatabaseCluster,
        queues: list[sqs.IQueue],
        dead_letter_queues: list[sqs.IQueue],
        load_balancer: elbv2.IApplicationLoadBalancer,
        target_group: elbv2.IApplicationTargetGroup,
        service: ecs.IBaseService,
        distribution: cloudfront.IDistribution,
    ) -> None:
        self.dashboard = cloudwatch.Dashboard(
            self,
            "Dashboard",
            dashboard_name=cfg.resource_name("overview"),
            default_interval=cdk.Duration.hours(6),
        )

        self.dashboard.add_widgets(
            cloudwatch.TextWidget(
                markdown=(
                    f"# astro-photos — {cfg.name}\n"
                    f"Dominio: https://{cfg.domain_name} · "
                    f"API: https://{cfg.api_domain_name}\n\n"
                    f"Presupuesto mensual: {cfg.monthly_budget_eur} EUR · "
                    f"Aurora {cfg.aurora.min_acu}-{cfg.aurora.max_acu} ACU · "
                    f"Fargate {cfg.fargate.min_tasks}-{cfg.fargate.max_tasks} tareas · "
                    f"Batch max {cfg.batch.max_vcpus} vCPU spot"
                ),
                width=24,
                height=3,
            )
        )

        self.dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Peticiones y errores (ALB)",
                left=[load_balancer.metrics.request_count(statistic="Sum")],
                right=[
                    load_balancer.metrics.http_code_target(
                        elbv2.HttpCodeTarget.TARGET_5XX_COUNT, statistic="Sum"
                    ),
                    load_balancer.metrics.http_code_target(
                        elbv2.HttpCodeTarget.TARGET_4XX_COUNT, statistic="Sum"
                    ),
                ],
                width=12,
            ),
            cloudwatch.GraphWidget(
                title="Latencia de la API",
                left=[
                    load_balancer.metrics.target_response_time(statistic="p50", label="p50"),
                    load_balancer.metrics.target_response_time(statistic="p99", label="p99"),
                ],
                width=12,
            ),
        )

        self.dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Colas (visibles / en vuelo)",
                left=[q.metric_approximate_number_of_messages_visible() for q in queues],
                right=[q.metric_approximate_age_of_oldest_message() for q in queues],
                width=12,
            ),
            cloudwatch.GraphWidget(
                title="DLQ — cualquier valor > 0 es trabajo perdido",
                left=[
                    q.metric_approximate_number_of_messages_visible() for q in dead_letter_queues
                ],
                width=12,
            ),
        )

        self.dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Aurora Serverless v2",
                left=[
                    database.metric_cpu_utilization(),
                    database.metric("ServerlessDatabaseCapacity", statistic="Average"),
                ],
                right=[database.metric_database_connections()],
                width=12,
            ),
            cloudwatch.GraphWidget(
                title="ECS — API",
                left=[service.metric_cpu_utilization(), service.metric_memory_utilization()],
                width=12,
            ),
        )

        self.dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="AWS Batch — reconstrucciones",
                left=[
                    self.batch_failed_metric,
                    cloudwatch.Metric(
                        namespace="AWS/Batch",
                        metric_name="SucceededJobCount",
                        dimensions_map={"JobQueue": cfg.resource_name("reconstruct")},
                        statistic="Sum",
                    ),
                ],
                width=12,
            ),
            cloudwatch.GraphWidget(
                title="CloudFront",
                left=[
                    cloudwatch.Metric(
                        namespace="AWS/CloudFront",
                        metric_name="Requests",
                        dimensions_map={
                            "DistributionId": distribution.distribution_id,
                            "Region": "Global",
                        },
                        statistic="Sum",
                        region="us-east-1",  # CloudFront solo publica ahi
                    )
                ],
                right=[
                    cloudwatch.Metric(
                        namespace="AWS/CloudFront",
                        metric_name="BytesDownloaded",
                        dimensions_map={
                            "DistributionId": distribution.distribution_id,
                            "Region": "Global",
                        },
                        statistic="Sum",
                        region="us-east-1",
                    )
                ],
                width=12,
            ),
        )

        self.dashboard.add_widgets(
            cloudwatch.AlarmStatusWidget(
                title="Estado de las alarmas", alarms=self.alarms, width=24, height=4
            )
        )

    # ---------------------------------------------------------- presupuesto -
    def _create_budget(self, cfg: EnvConfig) -> None:
        """AWS Budgets filtrado por el tag `Project`, con avisos al 80 % y al 100 %.

        El 100 % es de coste **previsto** (`FORECASTED`): avisa antes de gastarlo,
        no cuando ya se ha ido.
        """
        def notification(threshold: int, kind: str):
            return budgets.CfnBudget.NotificationWithSubscribersProperty(
                notification=budgets.CfnBudget.NotificationProperty(
                    comparison_operator="GREATER_THAN",
                    notification_type=kind,
                    threshold=threshold,
                    threshold_type="PERCENTAGE",
                ),
                subscribers=[
                    budgets.CfnBudget.SubscriberProperty(
                        address=self.topic.topic_arn, subscription_type="SNS"
                    ),
                    *[
                        budgets.CfnBudget.SubscriberProperty(
                            address=email, subscription_type="EMAIL"
                        )
                        for email in cfg.alert_emails
                    ],
                ],
            )

        self.budget = budgets.CfnBudget(
            self,
            "MonthlyBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name=cfg.resource_name("monthly"),
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(
                    amount=cfg.monthly_budget_usd, unit="USD"
                ),
                cost_filters={
                    # Funciona porque TODO lleva los tags de `EnvConfig.base_tags`.
                    "TagKeyValue": [
                        f"user:Project${'astro-photos'}",
                        f"user:Environment${cfg.name}",
                    ]
                },
                cost_types=budgets.CfnBudget.CostTypesProperty(
                    include_credit=False,
                    include_refund=False,
                    include_subscription=True,
                    include_tax=True,
                    use_blended=False,
                ),
            ),
            notifications_with_subscribers=[
                notification(80, "ACTUAL"),
                notification(100, "ACTUAL"),
                notification(100, "FORECASTED"),
            ],
        )
