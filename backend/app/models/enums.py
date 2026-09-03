"""Enums de Postgres. Los valores Python viven en ``app/domain`` cuando son dominio.

Un solo sitio donde se declaran los ``ENUM`` para que la migración inicial y los
modelos no puedan divergir.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Enum as SAEnum

from app.core.security import Role
from app.domain.licensing import LicenseCode
from app.domain.location import LocationPrecision

__all__ = [
    "JobStatus",
    "LocationSource",
    "ModelArchitecture",
    "ObjectCatalog",
    "ObjectType",
    "PhotoStatus",
    "TimeSource",
    "job_status_enum",
    "license_code_enum",
    "location_precision_enum",
    "location_source_enum",
    "model_architecture_enum",
    "object_catalog_enum",
    "object_type_enum",
    "photo_status_enum",
    "time_source_enum",
    "user_role_enum",
]


class PhotoStatus(StrEnum):
    UPLOADING = "uploading"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    QUARANTINED = "quarantined"


class TimeSource(StrEnum):
    EXIF = "exif"
    GPS = "gps"
    USER = "user"
    INFERRED = "inferred"


class LocationSource(StrEnum):
    EXIF_GPS = "exif_gps"
    USER_PIN = "user_pin"
    NAMED_SITE = "named_site"
    UNDISCLOSED = "undisclosed"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ObjectCatalog(StrEnum):
    MESSIER = "M"
    NGC = "NGC"
    IC = "IC"
    SH2 = "SH2"
    CALDWELL = "C"
    SOLAR = "solar"


class ObjectType(StrEnum):
    GALAXY = "galaxy"
    NEBULA = "nebula"
    CLUSTER = "cluster"
    PLANET = "planet"
    COMET = "comet"
    MOON = "moon"
    OTHER = "other"


class ModelArchitecture(StrEnum):
    BIPNET = "bipnet"
    BURSTORMER = "burstormer"
    RBSR = "rbsr"
    EDSR_BURST = "edsr-burst"
    CUSTOM = "custom"


def _pg_enum(enum_cls: type[StrEnum], name: str) -> SAEnum:
    """``ENUM`` nativo de Postgres ligado a la enum de Python.

    ``values_callable`` es imprescindible: sin él SQLAlchemy persistiría el *nombre*
    del miembro (``CC_BY_NC``) en vez de su valor (``CC-BY-NC-4.0``), que es lo que
    dice el contrato. ``create_type=False`` porque los tipos los crea la migración,
    no el ORM.
    """
    return SAEnum(
        enum_cls,
        name=name,
        create_type=False,
        native_enum=True,
        validate_strings=True,
        values_callable=lambda enum: [member.value for member in enum],
    )


license_code_enum = _pg_enum(LicenseCode, "license_code")
user_role_enum = _pg_enum(Role, "user_role")
photo_status_enum = _pg_enum(PhotoStatus, "photo_status")
time_source_enum = _pg_enum(TimeSource, "time_source")
location_source_enum = _pg_enum(LocationSource, "location_source")
location_precision_enum = _pg_enum(LocationPrecision, "location_precision")
job_status_enum = _pg_enum(JobStatus, "job_status")
object_catalog_enum = _pg_enum(ObjectCatalog, "object_catalog")
object_type_enum = _pg_enum(ObjectType, "object_type")
model_architecture_enum = _pg_enum(ModelArchitecture, "model_architecture")

#: Todos los enums, en el orden en que la migración inicial debe crearlos.
ALL_ENUMS: tuple[tuple[str, type[StrEnum]], ...] = (
    ("license_code", LicenseCode),
    ("user_role", Role),
    ("photo_status", PhotoStatus),
    ("time_source", TimeSource),
    ("location_source", LocationSource),
    ("location_precision", LocationPrecision),
    ("job_status", JobStatus),
    ("object_catalog", ObjectCatalog),
    ("object_type", ObjectType),
    ("model_architecture", ModelArchitecture),
)
