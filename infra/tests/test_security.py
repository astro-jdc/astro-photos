"""Las reglas duras de seguridad, comprobadas sobre la plantilla sintetizada.

Estos tests no verifican "que el código llame a la API de CDK", verifican lo que
de verdad se va a desplegar. Si alguien añade un bucket público o quita el DLQ de
una cola, esto se pone rojo antes de que llegue a AWS.
"""

from __future__ import annotations

import pytest

BUCKET_STACKS = {"data": 4, "edge": 1}  # logs, uploads, originals, derived / site


def test_todos_los_buckets_bloquean_el_acceso_publico(env) -> None:
    total = 0
    for stack_key in BUCKET_STACKS:
        buckets = env.resources(stack_key, "AWS::S3::Bucket")
        for logical_id, bucket in buckets.items():
            block = bucket["Properties"].get("PublicAccessBlockConfiguration")
            assert block is not None, f"{logical_id} no bloquea el acceso publico"
            assert block == {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            }, f"{logical_id} tiene el bloqueo de acceso publico incompleto"
            total += 1
    assert total == sum(BUCKET_STACKS.values())


def test_todos_los_buckets_estan_cifrados(env) -> None:
    for stack_key in BUCKET_STACKS:
        for logical_id, bucket in env.resources(stack_key, "AWS::S3::Bucket").items():
            encryption = bucket["Properties"].get("BucketEncryption")
            assert encryption, f"{logical_id} no esta cifrado en reposo"
            rules = encryption["ServerSideEncryptionConfiguration"]
            algorithms = {
                r["ServerSideEncryptionByDefault"]["SSEAlgorithm"]
                for r in rules
                if "ServerSideEncryptionByDefault" in r
            }
            assert algorithms <= {"AES256", "aws:kms"}, f"{logical_id}: {algorithms}"


def test_ninguna_politica_de_bucket_permite_principal_anonimo(env) -> None:
    for stack_key in BUCKET_STACKS:
        for logical_id, policy in env.resources(stack_key, "AWS::S3::BucketPolicy").items():
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
                if statement.get("Effect") != "Allow":
                    continue
                principal = statement.get("Principal")
                assert principal != "*", f"{logical_id} permite acceso anonimo"
                if isinstance(principal, dict):
                    assert principal.get("AWS") != "*", f"{logical_id} permite acceso anonimo"


def test_los_buckets_obligan_a_tls(env) -> None:
    """`enforce_ssl` mete un Deny con `aws:SecureTransport=false` en cada bucket."""
    for stack_key in BUCKET_STACKS:
        policies = env.resources(stack_key, "AWS::S3::BucketPolicy")
        denies = [
            statement
            for policy in policies.values()
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]
            if statement.get("Effect") == "Deny"
            and statement.get("Condition", {}).get("Bool", {}).get("aws:SecureTransport") == "false"
        ]
        assert len(denies) >= 1, f"{stack_key}: ningun bucket obliga a TLS"


def test_el_bucket_de_originales_tiene_versionado_y_glacier(env) -> None:
    buckets = env.resources("data", "AWS::S3::Bucket")
    originals = next(
        b for b in buckets.values() if b["Properties"]["BucketName"].endswith("-originals")
    )
    assert originals["Properties"]["VersioningConfiguration"] == {"Status": "Enabled"}

    transitions = [
        transition
        for rule in originals["Properties"]["LifecycleConfiguration"]["Rules"]
        for transition in rule.get("Transitions", [])
    ]
    assert any(t["StorageClass"] == "GLACIER_IR" for t in transitions), transitions


def test_el_bucket_de_derivados_solo_lo_lee_cloudfront(env) -> None:
    policies = env.resources("data", "AWS::S3::BucketPolicy")
    statements = [
        statement
        for policy in policies.values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        if statement.get("Sid") == "AllowCloudFrontOacRead"
    ]
    assert len(statements) == 1, "falta (o sobra) la politica de OAC del bucket derived"
    statement = statements[0]
    assert statement["Principal"] == {"Service": "cloudfront.amazonaws.com"}
    assert statement["Action"] == "s3:GetObject"
    # Sin la condicion del SourceArn, cualquier distribucion del mundo podria leer.
    assert "AWS:SourceArn" in statement["Condition"]["StringLike"]


def test_cloudfront_usa_origin_access_control_y_nunca_oai(env) -> None:
    template = env.template("edge")
    oacs = template.find_resources("AWS::CloudFront::OriginAccessControl")
    assert oacs, "el frontend deberia servirse con OAC"
    assert not template.find_resources("AWS::CloudFront::CloudFrontOriginAccessIdentity")

    distributions = template.find_resources("AWS::CloudFront::Distribution")
    (distribution,) = distributions.values()
    config = distribution["Properties"]["DistributionConfig"]
    assert config["ViewerCertificate"]["MinimumProtocolVersion"] == "TLSv1.2_2021"
    for origin in config["Origins"]:
        if "S3OriginConfig" in origin or "OriginAccessControlId" in origin:
            assert origin.get("OriginAccessControlId"), f"origen S3 sin OAC: {origin}"


def test_todos_los_comportamientos_de_cloudfront_fuerzan_https(env) -> None:
    (distribution,) = env.resources("edge", "AWS::CloudFront::Distribution").values()
    config = distribution["Properties"]["DistributionConfig"]
    behaviors = [config["DefaultCacheBehavior"], *config.get("CacheBehaviors", [])]
    for behavior in behaviors:
        assert behavior["ViewerProtocolPolicy"] in ("redirect-to-https", "https-only")


def test_el_alb_solo_expone_https(env) -> None:
    listeners = env.resources("api", "AWS::ElasticLoadBalancingV2::Listener")
    for logical_id, listener in listeners.items():
        props = listener["Properties"]
        if props["Protocol"] == "HTTP":
            # El unico listener HTTP admitido es el que redirige a HTTPS.
            actions = props["DefaultActions"]
            assert all(a["Type"] == "redirect" for a in actions), logical_id
            assert all(a["RedirectConfig"]["Protocol"] == "HTTPS" for a in actions), logical_id
        else:
            assert props["Protocol"] == "HTTPS", logical_id
            assert props["Certificates"], logical_id


def test_el_waf_solo_existe_en_prod(staging, prod) -> None:
    assert not staging.resources("edge_global", "AWS::WAFv2::WebACL")
    acls = prod.resources("edge_global", "AWS::WAFv2::WebACL")
    assert len(acls) == 1
    (acl,) = acls.values()
    assert acl["Properties"]["Scope"] == "CLOUDFRONT"
    rule_names = {rule["Name"] for rule in acl["Properties"]["Rules"]}
    assert "AWSManagedRulesCommonRuleSet" in rule_names
    assert "RateLimitPerIp" in rule_names


def test_no_hay_secretos_en_texto_plano_en_las_plantillas(env) -> None:
    """Las contraseñas se inyectan por Secrets Manager, nunca como texto."""
    import json

    for stack_key in env.stacks:
        rendered = json.dumps(env.template(stack_key).to_json())
        for forbidden in ("AKIA", "-----BEGIN", "aws_secret_access_key"):
            assert forbidden not in rendered, f"{stack_key} contiene {forbidden!r}"

    for task in env.resources("api", "AWS::ECS::TaskDefinition").values():
        for container in task["Properties"]["ContainerDefinitions"]:
            names = {e["Name"] for e in container.get("Environment", [])}
            assert "DB_PASSWORD" not in names, "la contrasena va en `secrets`, no en `environment`"
            secret_names = {s["Name"] for s in container.get("Secrets", [])}
            if container["Name"] != "db-bootstrap":
                continue
            assert {"DB_PASSWORD", "DB_USER"} <= secret_names


@pytest.mark.parametrize("stack_key", ["data", "compute", "api"])
def test_las_colas_y_topicos_estan_cifrados(env, stack_key) -> None:
    for logical_id, queue in env.resources(stack_key, "AWS::SQS::Queue").items():
        props = queue["Properties"]
        assert props.get("SqsManagedSseEnabled") or props.get("KmsMasterKeyId"), logical_id
