"""Las reglas que impiden que un entorno parado cueste dinero.

Staging tiene un objetivo explícito: **< 30 €/mes** (`docs/branching.md`). Casi todo
ese objetivo se juega en tres sitios — Batch a cero, Aurora pausable y un solo NAT —
así que los tres tienen test.
"""

from __future__ import annotations


def test_batch_nunca_tiene_instancias_gpu_encendidas_sin_trabajo(env) -> None:
    """`minvCpus=0` es la regla que evita facturar ~1 €/h por nada."""
    environments = env.resources("compute", "AWS::Batch::ComputeEnvironment")
    assert environments, "no hay compute environment de Batch"
    for logical_id, ce in environments.items():
        resources = ce["Properties"]["ComputeResources"]
        assert resources["MinvCpus"] == 0, f"{logical_id} deja instancias encendidas"
        assert resources["Type"] == "SPOT", f"{logical_id} no usa spot"
        assert resources["MaxvCpus"] == env.cfg.batch.max_vcpus
        assert resources["BidPercentage"] == env.cfg.batch.spot_bid_percentage
        assert all(t.startswith(("g5.", "g6.")) for t in resources["InstanceTypes"]), resources


def test_los_jobs_de_batch_tienen_timeout_y_reintento_de_spot(env) -> None:
    definitions = env.resources("compute", "AWS::Batch::JobDefinition")
    assert len(definitions) == 2, "deberia haber job de reconstruccion y de entrenamiento"
    for logical_id, definition in definitions.items():
        props = definition["Properties"]
        assert props["Timeout"]["AttemptDurationSeconds"] > 0, logical_id
        strategy = props["RetryStrategy"]
        assert strategy["Attempts"] >= 1
        # Batch codifica "spot reclamada" como el status reason `Host EC2*`.
        reasons = {
            condition.get("OnStatusReason")
            for condition in strategy.get("EvaluateOnExit", [])
            if condition.get("Action", "").upper() == "RETRY"
        }
        assert any(reason and reason.startswith("Host EC2") for reason in reasons), logical_id


def test_aurora_de_staging_se_pausa_y_la_de_prod_no(staging, prod) -> None:
    (staging_cluster,) = staging.resources("data", "AWS::RDS::DBCluster").values()
    scaling = staging_cluster["Properties"]["ServerlessV2ScalingConfiguration"]
    # La auto-pausa de Aurora Serverless v2 solo funciona con capacidad minima 0.
    assert scaling["MinCapacity"] == 0
    assert scaling["MaxCapacity"] == 2
    assert scaling["SecondsUntilAutoPause"] == 15 * 60

    (prod_cluster,) = prod.resources("data", "AWS::RDS::DBCluster").values()
    prod_scaling = prod_cluster["Properties"]["ServerlessV2ScalingConfiguration"]
    assert prod_scaling["MinCapacity"] == 1
    assert prod_scaling["MaxCapacity"] == 16
    assert "SecondsUntilAutoPause" not in prod_scaling


def test_prod_es_multi_az_y_staging_no(staging, prod) -> None:
    assert len(staging.resources("data", "AWS::RDS::DBInstance")) == 1
    assert len(prod.resources("data", "AWS::RDS::DBInstance")) == 2


def test_staging_tiene_un_solo_nat_gateway(staging, prod) -> None:
    """Cada NAT gateway son ~32 €/mes: en staging solo puede haber uno."""
    assert len(staging.resources("network", "AWS::EC2::NatGateway")) == 1
    assert len(prod.resources("network", "AWS::EC2::NatGateway")) == 2


def test_hay_vpc_endpoints_para_evitar_trafico_por_el_nat(env) -> None:
    endpoints = env.resources("network", "AWS::EC2::VPCEndpoint")
    services = set()
    for endpoint in endpoints.values():
        service = endpoint["Properties"]["ServiceName"]
        services.add(str(service))
    joined = " ".join(services)
    for expected in ("s3", "ecr.api", "ecr.dkr", "sqs", "secretsmanager", "logs"):
        assert expected in joined, f"falta el VPC endpoint de {expected}: {services}"


def test_el_numero_de_tareas_fargate_respeta_el_entorno(staging, prod) -> None:
    (staging_service,) = [
        s
        for s in staging.resources("api", "AWS::ECS::Service").values()
        if s["Properties"].get("ServiceName", "").endswith("-api")
    ]
    assert staging_service["Properties"]["DesiredCount"] == 1

    (prod_service,) = [
        s
        for s in prod.resources("api", "AWS::ECS::Service").values()
        if s["Properties"].get("ServiceName", "").endswith("-api")
    ]
    assert prod_service["Properties"]["DesiredCount"] == 2

    for environment, expected in ((staging, (1, 2)), (prod, (2, 10))):
        targets = environment.resources("api", "AWS::ApplicationAutoScaling::ScalableTarget")
        api_targets = [
            t
            for t in targets.values()
            if t["Properties"]["ScalableDimension"] == "ecs:service:DesiredCount"
        ]
        ranges = {(t["Properties"]["MinCapacity"], t["Properties"]["MaxCapacity"]) for t in api_targets}
        assert expected in ranges, ranges


def test_hay_presupuesto_con_alerta(env) -> None:
    budgets = env.resources("observability", "AWS::Budgets::Budget")
    assert len(budgets) == 1
    (budget,) = budgets.values()
    data = budget["Properties"]["Budget"]
    assert data["TimeUnit"] == "MONTHLY"
    assert data["BudgetLimit"]["Amount"] == env.cfg.monthly_budget_usd
    notifications = budget["Properties"]["NotificationsWithSubscribers"]
    thresholds = {(n["Notification"]["Threshold"], n["Notification"]["NotificationType"]) for n in notifications}
    assert (80, "ACTUAL") in thresholds
    assert (100, "FORECASTED") in thresholds
    assert all(n["Subscribers"] for n in notifications)


def test_el_presupuesto_de_staging_son_30_euros(staging) -> None:
    assert staging.cfg.monthly_budget_eur == 30


def test_los_ciclos_de_vida_de_s3_estan_configurados(env) -> None:
    buckets = env.resources("data", "AWS::S3::Bucket")
    for bucket in buckets.values():
        name = bucket["Properties"]["BucketName"]
        rules = bucket["Properties"].get("LifecycleConfiguration", {}).get("Rules", [])
        assert rules, f"{name} no tiene ninguna regla de ciclo de vida"
        if name.endswith("-uploads"):
            staged = next(r for r in rules if r.get("Prefix") == "staging/")
            assert staged["ExpirationInDays"] == env.cfg.storage.uploads_expiration_days
