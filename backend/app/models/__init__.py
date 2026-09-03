"""Modelos SQLAlchemy. Importar este paquete registra todas las tablas en la metadata."""

from __future__ import annotations

from app.db.base import Base, metadata
from app.models.audit import AuditLog
from app.models.collection import Collection, CollectionPhoto
from app.models.enums import (
    ALL_ENUMS,
    JobStatus,
    LocationSource,
    ModelArchitecture,
    ObjectCatalog,
    ObjectType,
    PhotoStatus,
    TimeSource,
)
from app.models.license import License
from app.models.ml import DatasetSnapshot, MLModel, TrainingRun
from app.models.photo import EMBEDDING_DIM, Photo
from app.models.reconstruction import Reconstruction, ReconstructionInput
from app.models.site import ObservingSite
from app.models.sky_object import SkyObject
from app.models.user import User

__all__ = [
    "ALL_ENUMS",
    "EMBEDDING_DIM",
    "AuditLog",
    "Base",
    "Collection",
    "CollectionPhoto",
    "DatasetSnapshot",
    "JobStatus",
    "License",
    "LocationSource",
    "MLModel",
    "ModelArchitecture",
    "ObjectCatalog",
    "ObjectType",
    "ObservingSite",
    "Photo",
    "PhotoStatus",
    "Reconstruction",
    "ReconstructionInput",
    "SkyObject",
    "TimeSource",
    "TrainingRun",
    "User",
    "metadata",
]
