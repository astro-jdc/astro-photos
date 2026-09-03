"""Cognito: User Pool, cliente del frontend, dominio hospedado y grupos.

El backend valida los JWT contra el JWKS del pool (`docs/api.md`), así que este
stack no tiene dependencias: se puede desplegar solo.

Los tres grupos salen de `users.role` en `docs/data-model.md`:
`member | curator | admin`.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_cognito as cognito
from constructs import Construct

from config import EnvConfig
from stacks.base import BaseStack

#: Precedencia de los grupos (menor gana en caso de solape).
GROUPS: tuple[tuple[str, str, int], ...] = (
    ("admin", "Administracion completa: activa modelos, modera y borra", 1),
    ("curator", "Cura el catalogo de objetos y revisa fotos en cuarentena", 5),
    ("member", "Usuario normal: sube fotos y pide reconstrucciones", 10),
)


class AuthStack(BaseStack):
    def __init__(self, scope: Construct, construct_id: str, *, cfg: EnvConfig, **kwargs) -> None:
        super().__init__(scope, construct_id, cfg=cfg, **kwargs)

        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=cfg.resource_name("users"),
            # Auto-registro permitido, pero la cuenta NO sirve hasta verificar el
            # email: `auto_verify` + `SelfSignUpEnabled` sin `AdminCreateUserOnly`.
            self_sign_up_enabled=True,
            sign_in_aliases=cognito.SignInAliases(email=True, username=False),
            sign_in_case_sensitive=False,
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            # Cambiar el email no revoca el anterior hasta verificar el nuevo.
            keep_original=cognito.KeepOriginalAttrs(email=True),
            user_verification=cognito.UserVerificationConfig(
                email_subject="Verifica tu cuenta de astro-photos",
                email_body=(
                    "Bienvenido a astro-photos. Tu codigo de verificacion es {####}."
                ),
                email_style=cognito.VerificationEmailStyle.CODE,
            ),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True),
                preferred_username=cognito.StandardAttribute(required=False, mutable=True),
                locale=cognito.StandardAttribute(required=False, mutable=True),
            ),
            custom_attributes={
                # `users.id` de nuestra base de datos, para no depender del sub.
                "app_user_id": cognito.StringAttribute(max_len=36, mutable=True),
            },
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
                temp_password_validity=cdk.Duration.days(3),
            ),
            # MFA opcional: no forzamos TOTP a un aficionado que solo sube fotos,
            # pero el que quiera (y todo `admin`) puede activarlo.
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(sms=False, otp=True),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            # Threat protection (deteccion de credenciales filtradas) solo en prod:
            # el plan PLUS se factura por usuario activo y staging no lo necesita.
            feature_plan=cognito.FeaturePlan.PLUS if cfg.is_prod else cognito.FeaturePlan.LITE,
            device_tracking=cognito.DeviceTracking(
                challenge_required_on_new_device=True,
                device_only_remembered_on_user_prompt=True,
            ),
            deletion_protection=cfg.is_prod,
            removal_policy=cfg.removal_policy,
        )

        callback_urls = [f"https://{cfg.domain_name}/auth/callback"]
        logout_urls = [f"https://{cfg.domain_name}/"]
        if not cfg.is_prod:
            callback_urls.append("http://localhost:3000/auth/callback")
            logout_urls.append("http://localhost:3000/")

        self.user_pool_client = self.user_pool.add_client(
            "WebClient",
            user_pool_client_name=cfg.resource_name("web"),
            # SPA pública: PKCE, sin secreto de cliente.
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_srp=True, custom=False, user_password=False),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True, implicit_code_grant=False),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                ],
                callback_urls=callback_urls,
                logout_urls=logout_urls,
            ),
            prevent_user_existence_errors=True,
            enable_token_revocation=True,
            access_token_validity=cdk.Duration.hours(1),
            id_token_validity=cdk.Duration.hours(1),
            refresh_token_validity=cdk.Duration.days(30),
            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
        )

        # Dominio hospedado de Cognito (Hosted UI). Un dominio propio exigiría un
        # certificado en us-east-1 y un registro A: se documenta como pendiente.
        self.user_pool_domain = self.user_pool.add_domain(
            "HostedDomain",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=cfg.resource_name("auth")),
        )

        for name, description, precedence in GROUPS:
            cognito.CfnUserPoolGroup(
                self,
                f"Group{name.capitalize()}",
                user_pool_id=self.user_pool.user_pool_id,
                group_name=name,
                description=description,
                precedence=precedence,
            )

        cdk.CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
        cdk.CfnOutput(
            self, "UserPoolClientId", value=self.user_pool_client.user_pool_client_id
        )
        cdk.CfnOutput(
            self,
            "UserPoolDomain",
            value=self.user_pool_domain.base_url(),
        )
        cdk.CfnOutput(
            self,
            "UserPoolJwksUri",
            value=(
                f"https://cognito-idp.{self.region}.amazonaws.com/"
                f"{self.user_pool.user_pool_id}/.well-known/jwks.json"
            ),
        )
