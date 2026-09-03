"""Fixtures de los tests de infraestructura.

Sintetizar el árbol completo tarda unos segundos, así que se hace una vez por
entorno y por sesión y se comparte entre todos los tests.

Nada aquí necesita credenciales de AWS: `config.resolve_account` cae a una cuenta
placeholder y no se usa ningún `from_lookup`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

INFRA_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INFRA_DIR))

import config
from stacks.api_stack import ApiStack
from stacks.auth_stack import AuthStack
from stacks.compute_stack import ComputeStack
from stacks.data_stack import DataStack
from stacks.ecr_stack import EcrStack
from stacks.edge_stack import EdgeGlobalStack, EdgeStack
from stacks.network_stack import NetworkStack
from stacks.observability_stack import ObservabilityStack

ENVIRONMENTS = ("staging", "prod")


def build_app(env_name: str, **extra_context: str) -> tuple[cdk.App, dict[str, cdk.Stack]]:
    """Compone el árbol completo igual que `app.py`, sin sintetizar todavía."""
    context = {"env": env_name, "account": "111122223333", "region": "eu-west-1"}
    context.update(extra_context)
    app = cdk.App(context=context)
    cfg = config.load(app)
    common = {"env": cfg.cdk_env, "cfg": cfg}

    network = NetworkStack(app, cfg.stack_name("Network"), **common)
    ecr = EcrStack(app, cfg.stack_name("Ecr"), **common)
    data = DataStack(app, cfg.stack_name("Data"), vpc=network.vpc, **common)
    auth = AuthStack(app, cfg.stack_name("Auth"), **common)
    compute = ComputeStack(
        app,
        cfg.stack_name("Compute"),
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
    edge_global = EdgeGlobalStack(
        app,
        cfg.stack_name("EdgeGlobal"),
        env=cfg.us_east_1_env,
        cfg=cfg,
        cross_region_references=True,
    )
    edge = EdgeStack(
        app,
        cfg.stack_name("Edge"),
        derived_bucket_name=data.derived_bucket.bucket_name,
        logs_bucket_name=data.logs_bucket.bucket_name,
        certificate=edge_global.certificate,
        web_acl_arn=edge_global.web_acl_arn,
        cross_region_references=True,
        **common,
    )
    observability = ObservabilityStack(
        app,
        cfg.stack_name("Observability"),
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
    for key, value in cfg.base_tags().items():
        cdk.Tags.of(app).add(key, value)

    stacks = {
        "network": network,
        "ecr": ecr,
        "data": data,
        "auth": auth,
        "compute": compute,
        "api": api,
        "edge_global": edge_global,
        "edge": edge,
        "observability": observability,
    }
    return app, stacks


class Env:
    """Un entorno sintetizado, con acceso perezoso a la `Template` de cada stack."""

    def __init__(self, name: str, **extra_context: str) -> None:
        self.name = name
        self.app, self.stacks = build_app(name, **extra_context)
        self.cfg = config.load(self.app)
        self._templates: dict[str, Template] = {}

    def template(self, key: str) -> Template:
        if key not in self._templates:
            self._templates[key] = Template.from_stack(self.stacks[key])
        return self._templates[key]

    def resources(self, key: str, cfn_type: str) -> dict:
        return self.template(key).find_resources(cfn_type)


@pytest.fixture(scope="session")
def staging() -> Env:
    return Env("staging")


@pytest.fixture(scope="session")
def prod() -> Env:
    return Env("prod")


@pytest.fixture(scope="session", params=ENVIRONMENTS)
def env(request: pytest.FixtureRequest) -> Env:
    """Todos los tests que usan esta fixture corren para staging y para prod."""
    return request.getfixturevalue(request.param)
