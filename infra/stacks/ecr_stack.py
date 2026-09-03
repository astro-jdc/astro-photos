"""Repositorios ECR.

Stack aparte y desplegado el primero a propósito: el CI construye y sube las
imágenes **antes** de desplegar el resto de stacks (que referencian una etiqueta
concreta), así que los repositorios tienen que existir de antes. Ver
`.github/workflows/deploy-staging.yml`.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_ecr as ecr
from constructs import Construct

from config import EnvConfig
from stacks.base import BaseStack


class EcrStack(BaseStack):
    """Un repositorio para la imagen del backend y otro para la de `models/`."""

    def __init__(self, scope: Construct, construct_id: str, *, cfg: EnvConfig, **kwargs) -> None:
        super().__init__(scope, construct_id, cfg=cfg, **kwargs)

        # Las imágenes de `models/` son enormes (CUDA + torch): la política de
        # ciclo de vida es lo único que impide que ECR se coma el presupuesto.
        lifecycle = [
            ecr.LifecycleRule(
                rule_priority=1,
                description="Conserva las N imagenes etiquetadas mas recientes",
                tag_status=ecr.TagStatus.TAGGED,
                tag_prefix_list=["sha-", "v"],
                max_image_count=20 if cfg.is_prod else 10,
            ),
            ecr.LifecycleRule(
                rule_priority=2,
                description="Borra las imagenes sin etiquetar a la semana",
                tag_status=ecr.TagStatus.UNTAGGED,
                max_image_age=cdk.Duration.days(7),
            ),
        ]

        self.backend_repository = ecr.Repository(
            self,
            "BackendRepository",
            repository_name=cfg.resource_name("backend"),
            image_scan_on_push=True,
            image_tag_mutability=ecr.TagMutability.IMMUTABLE,
            encryption=ecr.RepositoryEncryption.AES_256,
            lifecycle_rules=lifecycle,
            removal_policy=cfg.removal_policy,
            empty_on_delete=cfg.auto_delete_objects,
        )

        self.models_repository = ecr.Repository(
            self,
            "ModelsRepository",
            repository_name=cfg.resource_name("models"),
            image_scan_on_push=True,
            image_tag_mutability=ecr.TagMutability.IMMUTABLE,
            encryption=ecr.RepositoryEncryption.AES_256,
            lifecycle_rules=lifecycle,
            removal_policy=cfg.removal_policy,
            empty_on_delete=cfg.auto_delete_objects,
        )

        for name, repo in (
            ("BackendRepositoryUri", self.backend_repository),
            ("ModelsRepositoryUri", self.models_repository),
        ):
            cdk.CfnOutput(self, name, value=repo.repository_uri)
