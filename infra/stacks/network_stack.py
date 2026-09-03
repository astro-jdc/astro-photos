"""VPC, subredes y VPC endpoints.

Los endpoints no son estética: sin ellos todo el tráfico a S3, ECR, SQS, Secrets
Manager y CloudWatch Logs sale por el NAT gateway y se paga a ~0,045 €/GB. Con
imágenes de contenedor de varios GB y objetos astronómicos de 100+ MB eso es la
mayor parte de la factura de un entorno pequeño.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from constructs import Construct

from config import EnvConfig
from stacks.base import BaseStack


class NetworkStack(BaseStack):
    """VPC con subredes públicas, privadas con NAT y privadas aisladas."""

    def __init__(self, scope: Construct, construct_id: str, *, cfg: EnvConfig, **kwargs) -> None:
        super().__init__(scope, construct_id, cfg=cfg, **kwargs)

        # Sí, las zonas están "hardcodeadas": es deliberado y está explicado en
        # `stacks/base.py` (synth sin credenciales y `cdk.out` determinista).
        cdk.Annotations.of(self).acknowledge_warning(
            "CloudFormation-Validate::W3010",
            "Las AZ se derivan de la region a proposito; ver stacks/base.py",
        )

        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            vpc_name=cfg.resource_name("vpc"),
            ip_addresses=ec2.IpAddresses.cidr("10.42.0.0/16"),
            max_azs=cfg.max_azs,
            # staging: un solo NAT gateway (~32 €/mes). prod: uno por AZ.
            nat_gateways=cfg.nat_gateways,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="private", subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS, cidr_mask=22
                ),
                ec2.SubnetConfiguration(
                    name="isolated", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24
                ),
            ],
            enable_dns_hostnames=True,
            enable_dns_support=True,
        )

        # Flow logs a CloudWatch: solo rechazos en staging (barato), todo en prod.
        self.vpc.add_flow_log(
            "FlowLog",
            traffic_type=(
                ec2.FlowLogTrafficType.ALL if cfg.is_prod else ec2.FlowLogTrafficType.REJECT
            ),
            destination=ec2.FlowLogDestination.to_cloud_watch_logs(),
        )

        # El security group de Aurora NO vive aquí: lo crea `DataStack` junto al
        # cluster. Si viviera en este stack, la rotación del secreto (que añade su
        # propia regla de entrada) haría que Network dependiese de Data y Data de
        # Network: ciclo.

        # --- VPC endpoints ----------------------------------------------------
        # Gateway endpoints: gratis. No hay excusa para no tenerlos.
        self.vpc.add_gateway_endpoint(
            "S3Endpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3,
            subnets=[
                ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
                ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            ],
        )
        self.vpc.add_gateway_endpoint(
            "DynamoDbEndpoint", service=ec2.GatewayVpcEndpointAwsService.DYNAMODB
        )

        self.endpoint_security_group = ec2.SecurityGroup(
            self,
            "EndpointSg",
            vpc=self.vpc,
            security_group_name=cfg.resource_name("vpce"),
            description="VPC interface endpoints de astro-photos",
            allow_all_outbound=False,
        )
        self.endpoint_security_group.add_ingress_rule(
            peer=ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(443),
            description="HTTPS desde la VPC hacia los endpoints",
        )

        # Interface endpoints: ~7 €/mes por endpoint y AZ, pero ahorran mucho más
        # en tráfico NAT (pull de imágenes de ECR, subidas a S3, polling de SQS).
        interface_services = {
            "EcrApi": ec2.InterfaceVpcEndpointAwsService.ECR,
            "EcrDocker": ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
            "Sqs": ec2.InterfaceVpcEndpointAwsService.SQS,
            "SecretsManager": ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
            "CloudWatchLogs": ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
        }
        self.interface_endpoints: dict[str, ec2.InterfaceVpcEndpoint] = {}
        for name, service in interface_services.items():
            self.interface_endpoints[name] = self.vpc.add_interface_endpoint(
                f"{name}Endpoint",
                service=service,
                security_groups=[self.endpoint_security_group],
                subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
                private_dns_enabled=True,
                open=False,
            )

        cdk.CfnOutput(self, "VpcId", value=self.vpc.vpc_id, export_name=f"{construct_id}-VpcId")
