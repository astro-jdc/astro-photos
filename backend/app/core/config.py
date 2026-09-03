"""Configuración de la aplicación. Todo viene del entorno / `.env` (ver `.env.example`)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, PostgresDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "get_settings"]

Environment = Literal["dev", "test", "staging", "prod"]
AuthMode = Literal["local", "cognito"]

#: `.env` vive en la raíz del repo (lo dice `.env.example`), pero el backend se
#: arranca desde `backend/` (`make dev` hace `cd backend && uvicorn ...`). Un
#: `env_file=".env"` relativo al cwd no encuentra nada y la app levanta sin
#: `S3_ENDPOINT_URL` ni `SQS_ENDPOINT_URL`, apuntando a AWS real: `/readyz`
#: responde 503 y la subida no funciona. Anclamos la ruta al repo.
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: Se prueban en orden; pydantic-settings admite varios y el último gana, así que
#: un `.env` propio del backend sigue pudiendo sobreescribir al de la raíz.
_ENV_FILES = (_REPO_ROOT / ".env", _REPO_ROOT / "backend" / ".env", Path(".env"))

#: Longitud mínima del secreto HS256 (RFC 7518 §3.2: al menos el tamaño del hash).
MIN_JWT_SECRET_BYTES = 32

#: Placeholder de `.env.example`. Desplegar con él sería un agujero abierto.
EXAMPLE_JWT_SECRET = "cambia-esto-en-local"


class Settings(BaseSettings):
    """Ajustes del backend.

    Los nombres coinciden uno a uno con las variables de `.env.example`; no hay
    prefijo para no divergir del fichero que ya usan infra y el frontend.
    """

    model_config = SettingsConfigDict(
        env_file=_ENV_FILES,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    environment: Environment = "dev"
    log_level: str = "INFO"

    # --- Base de datos ----------------------------------------------------- #
    database_url: str = "postgresql+asyncpg://astro:astro@localhost:5432/astrophotos"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_echo: bool = False

    # --- Almacenamiento ---------------------------------------------------- #
    s3_endpoint_url: str | None = None
    s3_region: str = "eu-west-1"
    s3_bucket_uploads: str = "astro-photos-dev-uploads"
    s3_bucket_originals: str = "astro-photos-dev-originals"
    s3_bucket_derived: str = "astro-photos-dev-derived"
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None

    #: Firma de descargas. En AWS es CloudFront; en local se firma con S3/MinIO.
    cloudfront_domain: str | None = None
    cloudfront_key_pair_id: str | None = None
    cloudfront_private_key: str | None = None
    download_url_ttl_seconds: int = 300
    upload_url_ttl_seconds: int = 3600

    # --- Colas -------------------------------------------------------------- #
    sqs_endpoint_url: str | None = None
    sqs_queue_ingest: str = "astro-photos-dev-ingest"
    sqs_queue_reconstruct: str = "astro-photos-dev-reconstruct"

    # --- Auth --------------------------------------------------------------- #
    auth_mode: AuthMode = "local"
    jwt_secret: str = "cambia-esto-en-local"
    jwt_algorithm: str = "HS256"
    jwt_audience: str | None = None
    jwt_issuer: str | None = None
    cognito_user_pool_id: str | None = None
    cognito_client_id: str | None = None
    cognito_region: str = "eu-west-1"
    jwks_cache_seconds: int = 3600

    # --- Límites ------------------------------------------------------------ #
    max_upload_bytes: int = 536_870_912
    default_user_quota_bytes: int = 21_474_836_480
    max_queued_jobs_per_user: int = 5
    max_jobs_per_day: int = 20
    #: A partir de aquí la subida se hace multipart (``docs/api.md``: > 100 MB).
    multipart_threshold_bytes: int = 100 * 1024 * 1024
    multipart_chunk_bytes: int = 32 * 1024 * 1024
    #: Tope de frames que puede pedir una reconstrucción.
    max_reconstruction_inputs: int = 500
    default_reconstruction_inputs: int = 50

    # --- HTTP --------------------------------------------------------------- #
    api_prefix: str = "/api/v1"
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"]
    )
    default_page_size: int = 50
    max_page_size: int = 200

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Permite ``CORS_ORIGINS=a,b,c`` además de la lista JSON."""
        if isinstance(value, str) and not value.strip().startswith("["):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _secret_is_strong_enough(self) -> Settings:
        """HS256 con un secreto corto es firmable por fuerza bruta (RFC 7518 §3.2).

        En dev se tolera el placeholder de `.env.example`; en staging y producción
        arrancar con él sería un agujero, así que la app se niega a levantar.
        """
        if self.auth_mode == "local" and self.environment in ("staging", "prod"):
            if self.jwt_secret == EXAMPLE_JWT_SECRET:
                raise ValueError(
                    "JWT_SECRET sigue siendo el valor de ejemplo de `.env.example`; "
                    "cámbialo antes de desplegar."
                )
            if len(self.jwt_secret.encode()) < MIN_JWT_SECRET_BYTES:
                raise ValueError(
                    f"JWT_SECRET debe tener al menos {MIN_JWT_SECRET_BYTES} bytes en "
                    f"«{self.environment}»."
                )
        return self

    @field_validator("database_url")
    @classmethod
    def _check_async_driver(cls, value: str) -> str:
        """Async de arriba abajo: un driver síncrono aquí es un bug, no una opción."""
        if value.startswith("postgresql://"):
            value = value.replace("postgresql://", "postgresql+asyncpg://", 1)
        if not value.startswith(("postgresql+asyncpg://", "sqlite+aiosqlite://")):
            raise ValueError(
                "database_url debe usar un driver async (postgresql+asyncpg o "
                "sqlite+aiosqlite para tests)"
            )
        # Valida la forma de la URL de Postgres sin romper la de SQLite de tests.
        if value.startswith("postgresql+asyncpg://"):
            PostgresDsn(value)
        return value

    @property
    def is_local_auth(self) -> bool:
        return self.auth_mode == "local"

    @property
    def cognito_issuer(self) -> str:
        if not self.cognito_user_pool_id:
            raise ValueError("COGNITO_USER_POOL_ID es obligatorio con AUTH_MODE=cognito")
        return (
            f"https://cognito-idp.{self.cognito_region}.amazonaws.com/{self.cognito_user_pool_id}"
        )

    @property
    def cognito_jwks_url(self) -> str:
        return f"{self.cognito_issuer}/.well-known/jwks.json"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Settings cacheados. En tests se limpia con ``get_settings.cache_clear()``."""
    return Settings()
