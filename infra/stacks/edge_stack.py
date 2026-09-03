"""CloudFront, OAC, certificados, WAF y Route 53.

Dos stacks porque AWS obliga: el certificado ACM y el Web ACL de una distribución
de CloudFront **tienen** que vivir en `us-east-1`, aunque el resto del entorno esté
en `eu-west-1`. `EdgeGlobalStack` va allí y `EdgeStack` los consume mediante
referencias entre regiones (`cross_region_references=True`).

La distribución tiene tres orígenes (`docs/architecture.md`):

* `/` → bucket S3 del frontend Nuxt prerenderizado, **privado**, con OAC.
* `/media/*` → bucket S3 de derivados (previews, thumbs, tiles), **privado**, con OAC.
* `/api/*` → ALB, sin caché y reenviando `Authorization`.

Ningún bucket es público. Nunca. El acceso es siempre CloudFront → OAC → S3.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as route53_targets
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_wafv2 as wafv2
from constructs import Construct

from common import hosted_zone
from config import EnvConfig
from stacks.base import BaseStack

#: Prefijo bajo el que CloudFront sirve el bucket de derivados.
MEDIA_PATH = "/media/*"
API_PATH = "/api/*"


class EdgeGlobalStack(BaseStack):
    """Recursos de CloudFront que solo existen en us-east-1: ACM y WAF."""

    def __init__(self, scope: Construct, construct_id: str, *, cfg: EnvConfig, **kwargs) -> None:
        super().__init__(scope, construct_id, cfg=cfg, **kwargs)

        zone = hosted_zone(self, cfg)
        self.certificate = acm.Certificate(
            self,
            "SiteCertificate",
            domain_name=cfg.domain_name,
            subject_alternative_names=[f"www.{cfg.domain_name}"],
            validation=(
                acm.CertificateValidation.from_dns(zone)
                if zone is not None
                else acm.CertificateValidation.from_dns()
            ),
        )

        self.web_acl_arn: str | None = None
        if cfg.waf_enabled:
            # Solo en prod: un Web ACL cuesta ~5 €/mes + peticiones, y staging no
            # tiene tráfico real que proteger.
            web_acl = wafv2.CfnWebACL(
                self,
                "WebAcl",
                name=cfg.resource_name("cloudfront"),
                scope="CLOUDFRONT",
                default_action=wafv2.CfnWebACL.DefaultActionProperty(
                    allow=wafv2.CfnWebACL.AllowActionProperty()
                ),
                visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                    cloud_watch_metrics_enabled=True,
                    metric_name=cfg.resource_name("cloudfront"),
                    sampled_requests_enabled=True,
                ),
                rules=[
                    self._managed_rule("AWSManagedRulesCommonRuleSet", 1),
                    self._managed_rule("AWSManagedRulesKnownBadInputsRuleSet", 2),
                    self._managed_rule("AWSManagedRulesAmazonIpReputationList", 3),
                    # Límite de peticiones por IP: la subida va directa a S3, así
                    # que nadie legítimo necesita 2000 req/5 min contra la API.
                    wafv2.CfnWebACL.RuleProperty(
                        name="RateLimitPerIp",
                        priority=10,
                        action=wafv2.CfnWebACL.RuleActionProperty(
                            block=wafv2.CfnWebACL.BlockActionProperty()
                        ),
                        statement=wafv2.CfnWebACL.StatementProperty(
                            rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                                limit=2000, aggregate_key_type="IP"
                            )
                        ),
                        visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                            cloud_watch_metrics_enabled=True,
                            metric_name="RateLimitPerIp",
                            sampled_requests_enabled=True,
                        ),
                    ),
                ],
            )
            self.web_acl_arn = web_acl.attr_arn

        cdk.CfnOutput(self, "CertificateArn", value=self.certificate.certificate_arn)

    @staticmethod
    def _managed_rule(name: str, priority: int) -> wafv2.CfnWebACL.RuleProperty:
        return wafv2.CfnWebACL.RuleProperty(
            name=name,
            priority=priority,
            # `none` = no se sobrescribe la accion del grupo gestionado.
            override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
            statement=wafv2.CfnWebACL.StatementProperty(
                managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                    vendor_name="AWS", name=name
                )
            ),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name=name,
                sampled_requests_enabled=True,
            ),
        )


class EdgeStack(BaseStack):
    """Bucket del frontend, distribución de CloudFront y registros DNS."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cfg: EnvConfig,
        derived_bucket_name: str,
        logs_bucket_name: str,
        certificate: acm.ICertificate,
        web_acl_arn: str | None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, cfg=cfg, **kwargs)

        # Los buckets de `DataStack` se importan **por nombre** (son nombres
        # físicos deterministas de `EnvConfig`) y no como referencia entre stacks:
        # así el origen con OAC no intenta escribir la política del bucket desde
        # aquí, que es lo que crearía un ciclo Data <-> Edge. La política de
        # lectura vive en `data_stack.py`.
        cdk.Annotations.of(self).acknowledge_warning(
            "@aws-cdk/aws-cloudfront-origins:updateImportedBucketPolicyOac",
            "La politica de lectura con OAC se escribe a proposito en data_stack.py",
        )
        derived_bucket = s3.Bucket.from_bucket_name(self, "DerivedBucketRef", derived_bucket_name)
        logs_bucket = s3.Bucket.from_bucket_name(self, "LogsBucketRef", logs_bucket_name)

        # --- bucket del frontend ---------------------------------------------
        # El sitio Nuxt prerenderizado. Privado: se sirve solo por CloudFront.
        self.site_bucket = s3.Bucket(
            self,
            "SiteBucket",
            bucket_name=cfg.resource_name("site"),
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=cfg.is_prod,
            removal_policy=cfg.removal_policy,
            auto_delete_objects=cfg.auto_delete_objects,
        )

        # --- políticas compartidas -------------------------------------------
        self.security_headers = cloudfront.ResponseHeadersPolicy(
            self,
            "SecurityHeaders",
            response_headers_policy_name=cfg.resource_name("security-headers"),
            comment="Cabeceras de seguridad de astro-photos",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=self._csp(cfg),
                    override=True,
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY, override=True
                ),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
                    override=True,
                ),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=cdk.Duration.days(365),
                    include_subdomains=True,
                    preload=True,
                    override=True,
                ),
                xss_protection=cloudfront.ResponseHeadersXSSProtection(
                    protection=True, mode_block=True, override=True
                ),
            ),
        )

        # Caché de imágenes derivadas: son inmutables (la clave lleva hash), así
        # que se pueden cachear un año sin miedo.
        media_cache_policy = cloudfront.CachePolicy(
            self,
            "MediaCachePolicy",
            cache_policy_name=cfg.resource_name("media"),
            default_ttl=cdk.Duration.days(30),
            max_ttl=cdk.Duration.days(365),
            min_ttl=cdk.Duration.hours(1),
            enable_accept_encoding_brotli=True,
            enable_accept_encoding_gzip=True,
            query_string_behavior=cloudfront.CacheQueryStringBehavior.none(),
            cookie_behavior=cloudfront.CacheCookieBehavior.none(),
            header_behavior=cloudfront.CacheHeaderBehavior.none(),
        )

        api_behavior = cloudfront.BehaviorOptions(
            origin=origins.HttpOrigin(
                cfg.api_domain_name,
                protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
                read_timeout=cdk.Duration.seconds(60),
                keepalive_timeout=cdk.Duration.seconds(60),
                custom_headers={"X-Forwarded-Host": cfg.domain_name},
            ),
            viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
            cached_methods=cloudfront.CachedMethods.CACHE_GET_HEAD,
            # La API no se cachea: hay JWT de por medio y respuestas por usuario.
            cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
            origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER,
            response_headers_policy=self.security_headers,
            compress=True,
        )

        media_behavior = cloudfront.BehaviorOptions(
            origin=origins.S3BucketOrigin.with_origin_access_control(derived_bucket),
            viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
            cache_policy=media_cache_policy,
            response_headers_policy=self.security_headers,
            compress=True,
        )

        self.distribution = cloudfront.Distribution(
            self,
            "Distribution",
            comment=f"astro-photos {cfg.name}",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(self.site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=self.security_headers,
                compress=True,
            ),
            additional_behaviors={API_PATH: api_behavior, MEDIA_PATH: media_behavior},
            domain_names=[cfg.domain_name, f"www.{cfg.domain_name}"],
            certificate=certificate,
            web_acl_id=web_acl_arn,
            default_root_object="index.html",
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
            enable_ipv6=True,
            # PRICE_CLASS_100 (EU + NA) en staging; global en prod.
            price_class=(
                cloudfront.PriceClass.PRICE_CLASS_ALL
                if cfg.is_prod
                else cloudfront.PriceClass.PRICE_CLASS_100
            ),
            enable_logging=True,
            log_bucket=logs_bucket,
            log_file_prefix="cloudfront/",
            log_includes_cookies=False,
            error_responses=[
                # Nuxt sirve rutas prerenderizadas; las del panel de usuario son
                # SPA y no existen como objeto en S3.
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=cdk.Duration.minutes(1),
                ),
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=cdk.Duration.minutes(1),
                ),
            ],
        )

        # --- DNS --------------------------------------------------------------
        self.zone = hosted_zone(self, cfg)
        if self.zone is not None:
            target = route53.RecordTarget.from_alias(
                route53_targets.CloudFrontTarget(self.distribution)
            )
            for i, record_name in enumerate([cfg.domain_name, f"www.{cfg.domain_name}"]):
                route53.ARecord(
                    self, f"AliasA{i}", zone=self.zone, record_name=record_name, target=target
                )
                route53.AaaaRecord(
                    self, f"AliasAaaa{i}", zone=self.zone, record_name=record_name, target=target
                )

        cdk.CfnOutput(self, "DistributionId", value=self.distribution.distribution_id)
        cdk.CfnOutput(self, "DistributionDomainName", value=self.distribution.domain_name)
        cdk.CfnOutput(self, "SiteBucketName", value=self.site_bucket.bucket_name)
        cdk.CfnOutput(self, "SiteUrl", value=f"https://{cfg.domain_name}")

    @staticmethod
    def _csp(cfg: EnvConfig) -> str:
        """CSP del visor WebGL + MapLibre + Cognito Hosted UI."""
        connect = [
            "'self'",
            f"https://{cfg.api_domain_name}",
            "https://*.amazonaws.com",
            "https://*.amazoncognito.com",
        ]
        if not cfg.is_prod:
            connect.append("http://localhost:*")
        return "; ".join(
            [
                "default-src 'self'",
                # MapLibre GL y el visor astronómico compilan shaders en workers.
                "script-src 'self' 'wasm-unsafe-eval'",
                "worker-src 'self' blob:",
                "style-src 'self' 'unsafe-inline'",
                "img-src 'self' data: blob:",
                "font-src 'self' data:",
                f"connect-src {' '.join(connect)}",
                "frame-ancestors 'none'",
                "base-uri 'self'",
                "form-action 'self'",
                "object-src 'none'",
            ]
        )
