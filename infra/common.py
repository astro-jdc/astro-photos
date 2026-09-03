"""Utilidades compartidas por los stacks. Sin lógica de negocio: solo traducciones."""

from __future__ import annotations

from aws_cdk import aws_logs as logs

#: `EnvConfig.log_retention_days` es un entero legible; CloudWatch solo acepta un
#: conjunto cerrado de valores. Aquí está el mapeo, en un único sitio.
_RETENTION_BY_DAYS: dict[int, logs.RetentionDays] = {
    1: logs.RetentionDays.ONE_DAY,
    3: logs.RetentionDays.THREE_DAYS,
    5: logs.RetentionDays.FIVE_DAYS,
    7: logs.RetentionDays.ONE_WEEK,
    14: logs.RetentionDays.TWO_WEEKS,
    30: logs.RetentionDays.ONE_MONTH,
    60: logs.RetentionDays.TWO_MONTHS,
    90: logs.RetentionDays.THREE_MONTHS,
    180: logs.RetentionDays.SIX_MONTHS,
    365: logs.RetentionDays.ONE_YEAR,
}


def log_retention(days: int) -> logs.RetentionDays:
    """Traduce días a `logs.RetentionDays`, redondeando al valor válido mayor o igual."""
    if days in _RETENTION_BY_DAYS:
        return _RETENTION_BY_DAYS[days]
    for valid in sorted(_RETENTION_BY_DAYS):
        if valid >= days:
            return _RETENTION_BY_DAYS[valid]
    return logs.RetentionDays.ONE_YEAR


def hosted_zone(scope, cfg):
    """Zona de Route 53 del entorno, o `None` si no se ha configurado.

    Se usa `from_hosted_zone_attributes` y **no** `from_lookup` a propósito: un
    lookup necesita credenciales de AWS y rompería `cdk synth` en CI y en los
    tests. El id se pasa por contexto (`-c hosted_zone_id=Z0123...`) o se fija en
    `config.py` cuando la zona ya exista de verdad.
    """
    from aws_cdk import aws_route53 as route53

    if not cfg.hosted_zone_id or not cfg.hosted_zone_name:
        return None
    return route53.HostedZone.from_hosted_zone_attributes(
        scope,
        "HostedZone",
        hosted_zone_id=cfg.hosted_zone_id,
        zone_name=cfg.hosted_zone_name,
    )
