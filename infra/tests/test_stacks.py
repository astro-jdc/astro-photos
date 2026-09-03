"""Que cada pieza del diagrama de `docs/architecture.md` exista de verdad."""

from __future__ import annotations

import pytest

from config import PROJECT


# --------------------------------------------------------------------- colas --
def test_las_dos_colas_tienen_dlq(env) -> None:
    queues = env.resources("data", "AWS::SQS::Queue")
    names = {q["Properties"]["QueueName"] for q in queues.values()}
    prefix = env.cfg.prefix
    assert names == {
        f"{prefix}-ingest",
        f"{prefix}-ingest-dlq",
        f"{prefix}-reconstruct",
        f"{prefix}-reconstruct-dlq",
    }

    with_redrive = {
        q["Properties"]["QueueName"]
        for q in queues.values()
        if q["Properties"].get("RedrivePolicy")
    }
    assert with_redrive == {f"{prefix}-ingest", f"{prefix}-reconstruct"}
    for queue in queues.values():
        redrive = queue["Properties"].get("RedrivePolicy")
        if redrive:
            assert redrive["maxReceiveCount"] >= 3


# --------------------------------------------------------------------- auth ---
def test_el_user_pool_exige_verificar_el_email(env) -> None:
    (pool,) = env.resources("auth", "AWS::Cognito::UserPool").values()
    props = pool["Properties"]
    # Auto-registro permitido, pero la cuenta no queda verificada sola.
    assert props["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is False
    assert props["AutoVerifiedAttributes"] == ["email"]
    assert props["AccountRecoverySetting"]["RecoveryMechanisms"][0]["Name"] == "verified_email"


def test_la_politica_de_contrasenas_es_seria(env) -> None:
    (pool,) = env.resources("auth", "AWS::Cognito::UserPool").values()
    policy = pool["Properties"]["Policies"]["PasswordPolicy"]
    assert policy["MinimumLength"] >= 12
    assert policy["RequireLowercase"] and policy["RequireUppercase"]
    assert policy["RequireNumbers"] and policy["RequireSymbols"]


def test_mfa_opcional_con_totp(env) -> None:
    (pool,) = env.resources("auth", "AWS::Cognito::UserPool").values()
    assert pool["Properties"]["MfaConfiguration"] == "OPTIONAL"
    assert pool["Properties"]["EnabledMfas"] == ["SOFTWARE_TOKEN_MFA"]


def test_existen_los_tres_grupos(env) -> None:
    groups = env.resources("auth", "AWS::Cognito::UserPoolGroup")
    assert {g["Properties"]["GroupName"] for g in groups.values()} == {
        "member",
        "curator",
        "admin",
    }


def test_el_cliente_web_no_tiene_secreto(env) -> None:
    (client,) = env.resources("auth", "AWS::Cognito::UserPoolClient").values()
    assert client["Properties"].get("GenerateSecret") in (None, False)
    assert client["Properties"]["AllowedOAuthFlows"] == ["code"]


# ---------------------------------------------------------------------- api ---
def test_el_servicio_de_la_api_usa_codedeploy(env) -> None:
    services = [
        s
        for s in env.resources("api", "AWS::ECS::Service").values()
        if s["Properties"].get("ServiceName", "").endswith("-api")
    ]
    assert len(services) == 1
    assert services[0]["Properties"]["DeploymentController"] == {"Type": "CODE_DEPLOY"}


def test_hay_deployment_group_blue_green_con_rollback_por_alarma(env) -> None:
    groups = env.resources("api", "AWS::CodeDeploy::DeploymentGroup")
    assert len(groups) == 1
    (group,) = groups.values()
    props = group["Properties"]

    assert props["DeploymentStyle"] == {
        "DeploymentOption": "WITH_TRAFFIC_CONTROL",
        "DeploymentType": "BLUE_GREEN",
    }
    events = set(props["AutoRollbackConfiguration"]["Events"])
    assert {"DEPLOYMENT_FAILURE", "DEPLOYMENT_STOP_ON_ALARM"} <= events
    assert props["AutoRollbackConfiguration"]["Enabled"] is True

    alarms = props["AlarmConfiguration"]
    assert alarms["Enabled"] is True
    assert len(alarms["Alarms"]) >= 2, "faltan alarmas conectadas al despliegue"

    # La version antigua sobrevive 10 minutos: ventana de rollback.
    blue_green = props["BlueGreenDeploymentConfiguration"]
    assert blue_green["TerminateBlueInstancesOnDeploymentSuccess"]["TerminationWaitTimeInMinutes"] == 10


def test_hay_dos_target_groups_y_un_listener_de_pruebas(env) -> None:
    target_groups = env.resources("api", "AWS::ElasticLoadBalancingV2::TargetGroup")
    names = {t["Properties"]["Name"] for t in target_groups.values()}
    assert names == {env.cfg.resource_name("blue"), env.cfg.resource_name("green")}
    for target_group in target_groups.values():
        assert target_group["Properties"]["HealthCheckPath"] == "/readyz"

    ports = {
        listener["Properties"]["Port"]
        for listener in env.resources("api", "AWS::ElasticLoadBalancingV2::Listener").values()
    }
    assert {80, 443, 8443} <= ports


def test_las_migraciones_son_una_tarea_aparte(env) -> None:
    """Alembic corre en su propia task definition, no al arrancar la API."""
    tasks = env.resources("api", "AWS::ECS::TaskDefinition")
    families = {t["Properties"]["Family"]: t for t in tasks.values()}
    migrations = families[env.cfg.resource_name("migrations")]

    containers = {c["Name"]: c for c in migrations["Properties"]["ContainerDefinitions"]}
    assert set(containers) == {"db-bootstrap", "migrate"}

    # El bootstrap de extensiones corre ANTES que Alembic.
    assert containers["migrate"]["DependsOn"] == [
        {"Condition": "COMPLETE", "ContainerName": "db-bootstrap"}
    ]
    assert containers["migrate"]["Command"] == ["alembic", "upgrade", "head"]

    # ...y ninguna otra task definition arranca migraciones por su cuenta.
    for family, task in families.items():
        if family == env.cfg.resource_name("migrations"):
            continue
        for container in task["Properties"]["ContainerDefinitions"]:
            command = " ".join(container.get("Command", []))
            assert "alembic" not in command, f"{family} corre migraciones al arrancar"


def test_el_sql_de_extensiones_habilita_postgis_y_pgvector(env) -> None:
    tasks = env.resources("api", "AWS::ECS::TaskDefinition")
    migrations = next(
        t
        for t in tasks.values()
        if t["Properties"]["Family"] == env.cfg.resource_name("migrations")
    )
    bootstrap = next(
        c
        for c in migrations["Properties"]["ContainerDefinitions"]
        if c["Name"] == "db-bootstrap"
    )
    command = " ".join(bootstrap["Command"])
    for extension in ("postgis", "vector", "citext", "pgcrypto"):
        assert f"CREATE EXTENSION IF NOT EXISTS {extension}" in command


def test_el_sql_de_extensiones_esta_publicado_en_ssm(env) -> None:
    (parameter,) = env.resources("data", "AWS::SSM::Parameter").values()
    value = parameter["Properties"]["Value"]
    assert "CREATE EXTENSION IF NOT EXISTS postgis;" in value
    assert "CREATE EXTENSION IF NOT EXISTS vector;" in value


def test_autoescalado_por_cpu_y_por_profundidad_de_cola(env) -> None:
    policies = env.resources("api", "AWS::ApplicationAutoScaling::ScalingPolicy")
    types = {p["Properties"]["PolicyType"] for p in policies.values()}
    assert "TargetTrackingScaling" in types, "falta el escalado por CPU"
    assert "StepScaling" in types, "falta el escalado por profundidad de cola"

    cpu_policies = [
        p
        for p in policies.values()
        if p["Properties"]["PolicyType"] == "TargetTrackingScaling"
    ]
    assert any(
        p["Properties"]["TargetTrackingScalingPolicyConfiguration"]["PredefinedMetricSpecification"][
            "PredefinedMetricType"
        ]
        == "ECSServiceAverageCPUUtilization"
        for p in cpu_policies
    )


# ------------------------------------------------------------------ compute ---
def test_la_lambda_dispatcher_consume_de_la_cola_reconstruct(env) -> None:
    mappings = env.resources("compute", "AWS::Lambda::EventSourceMapping")
    assert len(mappings) == 1
    (mapping,) = mappings.values()
    assert mapping["Properties"]["BatchSize"] == 1


def test_la_lambda_de_verificacion_escucha_el_prefijo_staging(env) -> None:
    rules = env.resources("compute", "AWS::Events::Rule")
    assert len(rules) == 1
    (rule,) = rules.values()
    pattern = rule["Properties"]["EventPattern"]
    assert pattern["source"] == ["aws.s3"]
    assert pattern["detail-type"] == ["Object Created"]
    assert pattern["detail"]["object"]["key"] == [{"prefix": "staging/"}]
    assert pattern["detail"]["bucket"]["name"] == [env.cfg.resource_name("uploads")]


def test_las_lambdas_usan_python_312(env) -> None:
    functions = env.resources("compute", "AWS::Lambda::Function")
    runtimes = {f["Properties"].get("Runtime") for f in functions.values()}
    assert runtimes <= {"python3.12", None}


# ------------------------------------------------------------------- edge -----
def test_cloudfront_tiene_los_tres_origenes(env) -> None:
    (distribution,) = env.resources("edge", "AWS::CloudFront::Distribution").values()
    config = distribution["Properties"]["DistributionConfig"]
    assert len(config["Origins"]) == 3, config["Origins"]

    paths = {behavior["PathPattern"] for behavior in config["CacheBehaviors"]}
    assert paths == {"/api/*", "/media/*"}

    api_behavior = next(b for b in config["CacheBehaviors"] if b["PathPattern"] == "/api/*")
    assert set(api_behavior["AllowedMethods"]) >= {"GET", "POST", "PUT", "DELETE"}


def test_cloudfront_anade_cabeceras_de_seguridad(env) -> None:
    policies = env.resources("edge", "AWS::CloudFront::ResponseHeadersPolicy")
    (policy,) = policies.values()
    headers = policy["Properties"]["ResponseHeadersPolicyConfig"]["SecurityHeadersConfig"]
    assert headers["StrictTransportSecurity"]["AccessControlMaxAgeSec"] >= 31536000
    assert headers["ContentTypeOptions"]["Override"] is True
    assert headers["FrameOptions"]["FrameOption"] == "DENY"
    assert "default-src 'self'" in headers["ContentSecurityPolicy"]["ContentSecurityPolicy"]


def test_el_certificado_de_cloudfront_esta_en_us_east_1(env) -> None:
    assert env.stacks["edge_global"].region == "us-east-1"
    certificates = env.resources("edge_global", "AWS::CertificateManager::Certificate")
    assert len(certificates) == 1


def test_hay_registros_de_route53_cuando_se_configura_la_zona() -> None:
    """Sin `hosted_zone_id` no hay DNS; con él, alias A y AAAA del sitio."""
    from tests.conftest import Env

    sin_zona = Env("staging")
    assert not sin_zona.resources("edge", "AWS::Route53::RecordSet")

    con_zona = Env("staging", hosted_zone_id="Z0123456789ABCDEFGHIJ")
    records = con_zona.resources("edge", "AWS::Route53::RecordSet")
    types = {r["Properties"]["Type"] for r in records.values()}
    assert types == {"A", "AAAA"}
    assert len(records) == 4  # apex y www, en A y AAAA

    api_records = con_zona.resources("api", "AWS::Route53::RecordSet")
    assert len(api_records) == 1


# --------------------------------------------------------- observabilidad -----
def test_hay_alarmas_para_lo_que_importa(env) -> None:
    alarms = env.resources("observability", "AWS::CloudWatch::Alarm")
    names = {a["Properties"]["AlarmName"] for a in alarms.values()}
    prefix = env.cfg.prefix
    for expected in (
        f"{prefix}-obs-api-5xx",
        f"{prefix}-obs-api-p99",
        f"{prefix}-batch-failed",
        f"{prefix}-aurora-cpu",
    ):
        assert expected in names, names
    # Una alarma por DLQ.
    assert len([n for n in names if "-dlq-" in n]) == 2


def test_las_alarmas_notifican_al_topico_sns(env) -> None:
    topics = env.resources("observability", "AWS::SNS::Topic")
    assert len(topics) == 1
    for alarm in env.resources("observability", "AWS::CloudWatch::Alarm").values():
        assert alarm["Properties"]["AlarmActions"], alarm["Properties"]["AlarmName"]


def test_hay_dashboard(env) -> None:
    dashboards = env.resources("observability", "AWS::CloudWatch::Dashboard")
    assert len(dashboards) == 1
    (dashboard,) = dashboards.values()
    assert dashboard["Properties"]["DashboardName"] == env.cfg.resource_name("overview")


def test_los_log_groups_tienen_retencion(env) -> None:
    for stack_key in ("api", "compute", "observability"):
        for logical_id, group in env.resources(stack_key, "AWS::Logs::LogGroup").items():
            assert group["Properties"].get("RetentionInDays"), logical_id


# ------------------------------------------------------------------- tags -----
@pytest.mark.parametrize("stack_key", ["data", "api", "compute", "edge", "network"])
def test_todo_lleva_los_tags_obligatorios(env, stack_key) -> None:
    """`Project`, `Environment`, `ManagedBy` y `CostCenter` en cada recurso etiquetable."""
    template = env.template(stack_key).to_json()
    etiquetables = 0
    for logical_id, resource in template["Resources"].items():
        tags = resource.get("Properties", {}).get("Tags")
        if not isinstance(tags, list) or not tags:
            continue
        if not all(isinstance(t, dict) and "Key" in t for t in tags):
            continue
        keys = {t["Key"] for t in tags}
        assert {"Project", "Environment", "ManagedBy", "CostCenter"} <= keys, logical_id
        etiquetables += 1
    assert etiquetables > 0, f"{stack_key}: ningun recurso etiquetado"

    values = {
        t["Key"]: t["Value"]
        for resource in template["Resources"].values()
        for t in resource.get("Properties", {}).get("Tags", [])
        if isinstance(t, dict) and t.get("Key") == "Project"
    }
    assert values.get("Project") == PROJECT
