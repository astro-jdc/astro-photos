"""Stack base común a todo el proyecto."""

from __future__ import annotations

import aws_cdk as cdk
from constructs import Construct

from config import EnvConfig


class BaseStack(cdk.Stack):
    """`cdk.Stack` que resuelve las zonas de disponibilidad sin llamar a AWS.

    Con un `env` concreto (cuenta + región), CDK resuelve `availabilityZones`
    preguntándole a EC2 a través del *context provider* `availability-zones`, y
    eso **exige credenciales**. Eso rompería el criterio que nos hemos puesto:
    `cdk synth` tiene que funcionar en el CI y en los tests sin credenciales.

    Todas las regiones en las que este proyecto se despliega o se puede desplegar
    (`eu-west-1`, `us-east-1`) tienen al menos las zonas `a`, `b` y `c`, así que
    se derivan del nombre de la región. `max_azs` de la VPC decide cuántas se
    usan de verdad.

    El efecto secundario bueno: el `cdk.out` es determinista y el `cdk diff` de
    un PR no depende de a qué zonas tenga acceso la cuenta ese día.
    """

    AZ_SUFFIXES = ("a", "b", "c")

    def __init__(self, scope: Construct, construct_id: str, *, cfg: EnvConfig, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.cfg = cfg

    @property
    def availability_zones(self) -> list[str]:
        return [f"{self.region}{suffix}" for suffix in self.AZ_SUFFIXES]
