"""Esquema inicial: extensiones, enums, tablas, índices y semilla de licencias.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-03

Crea el modelo completo de ``docs/data-model.md`` sobre PostgreSQL 16 + PostGIS +
pgvector. Es idempotente en las extensiones (``IF NOT EXISTS``) para poder correr
contra el contenedor de ``docker-compose.dev.yml``, que ya las crea en
``scripts/init-db.sql``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geography
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql as pg

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
ENUMS: dict[str, tuple[str, ...]] = {
    "license_code": (
        "CC0-1.0",
        "CC-BY-4.0",
        "CC-BY-SA-4.0",
        "CC-BY-NC-4.0",
        "CC-BY-NC-SA-4.0",
        "CC-BY-ND-4.0",
        "CC-BY-NC-ND-4.0",
        "ARR",
    ),
    "user_role": ("member", "curator", "admin"),
    "photo_status": ("uploading", "processing", "ready", "failed", "quarantined"),
    "time_source": ("exif", "gps", "user", "inferred"),
    "location_source": ("exif_gps", "user_pin", "named_site", "undisclosed"),
    "location_precision": ("exact", "city", "country", "hidden"),
    "job_status": ("queued", "running", "succeeded", "failed", "cancelled"),
    "object_catalog": ("M", "NGC", "IC", "SH2", "C", "solar"),
    "object_type": (
        "galaxy",
        "nebula",
        "cluster",
        "planet",
        "comet",
        "moon",
        "other",
    ),
    "model_architecture": ("bipnet", "burstormer", "rbsr", "edsr-burst", "custom"),
}


def _enum(name: str) -> pg.ENUM:
    """Referencia a un enum ya creado (``create_type=False``)."""
    return pg.ENUM(*ENUMS[name], name=name, create_type=False)


#: Semilla fija de la tabla de referencia ``licenses``. Espejo de
#: ``app.domain.licensing.LICENSE_CATALOG``; el test de tabla comprueba que no
#: divergen.
LICENSE_SEED: tuple[tuple[str, str, str, str, str, bool, bool, bool, bool, int, str | None], ...] = (
    (
        "CC0-1.0",
        "Public Domain Dedication",
        "Dominio público",
        "1.0",
        "https://creativecommons.org/publicdomain/zero/1.0/",
        True,
        True,
        False,
        False,
        0,
        "CC0-1.0",
    ),
    (
        "CC-BY-4.0",
        "Attribution",
        "Atribución",
        "4.0",
        "https://creativecommons.org/licenses/by/4.0/",
        True,
        True,
        True,
        False,
        1,
        "CC-BY-4.0",
    ),
    (
        "CC-BY-SA-4.0",
        "Attribution-ShareAlike",
        "Atribución + CompartirIgual",
        "4.0",
        "https://creativecommons.org/licenses/by-sa/4.0/",
        True,
        True,
        True,
        True,
        2,
        "CC-BY-SA-4.0",
    ),
    (
        "CC-BY-NC-4.0",
        "Attribution-NonCommercial",
        "Atribución + NoComercial",
        "4.0",
        "https://creativecommons.org/licenses/by-nc/4.0/",
        False,
        True,
        True,
        False,
        3,
        "CC-BY-NC-4.0",
    ),
    (
        "CC-BY-NC-SA-4.0",
        "Attribution-NonCommercial-ShareAlike",
        "Atribución + NoComercial + CompartirIgual",
        "4.0",
        "https://creativecommons.org/licenses/by-nc-sa/4.0/",
        False,
        True,
        True,
        True,
        4,
        "CC-BY-NC-SA-4.0",
    ),
    (
        "CC-BY-ND-4.0",
        "Attribution-NoDerivatives",
        "Atribución + SinDerivadas",
        "4.0",
        "https://creativecommons.org/licenses/by-nd/4.0/",
        True,
        False,
        True,
        False,
        5,
        "CC-BY-ND-4.0",
    ),
    (
        "CC-BY-NC-ND-4.0",
        "Attribution-NonCommercial-NoDerivatives",
        "Atribución + NoComercial + SinDerivadas",
        "4.0",
        "https://creativecommons.org/licenses/by-nc-nd/4.0/",
        False,
        False,
        True,
        False,
        6,
        "CC-BY-NC-ND-4.0",
    ),
    (
        "ARR",
        "All Rights Reserved",
        "Todos los derechos reservados",
        "",
        "",
        False,
        False,
        True,
        False,
        7,
        None,
    ),
)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    ]


def _uuid_pk() -> sa.Column:
    return sa.Column(
        "id",
        pg.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # Extensiones
    # ------------------------------------------------------------------ #
    for extension in ("postgis", "pgcrypto", "citext", "vector"):
        op.execute(f'CREATE EXTENSION IF NOT EXISTS "{extension}"')

    # ------------------------------------------------------------------ #
    # Enums
    # ------------------------------------------------------------------ #
    for name, values in ENUMS.items():
        rendered = ", ".join(f"'{v}'" for v in values)
        op.execute(
            f"""
            DO $$ BEGIN
                CREATE TYPE {name} AS ENUM ({rendered});
            EXCEPTION WHEN duplicate_object THEN NULL;
            END $$;
            """
        )

    # ------------------------------------------------------------------ #
    # licenses (tabla de referencia)
    # ------------------------------------------------------------------ #
    licenses = op.create_table(
        "licenses",
        sa.Column("code", _enum("license_code"), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("name_es", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("allows_commercial", sa.Boolean(), nullable=False),
        sa.Column("allows_derivatives", sa.Boolean(), nullable=False),
        sa.Column("requires_attribution", sa.Boolean(), nullable=False),
        sa.Column("requires_sharealike", sa.Boolean(), nullable=False),
        sa.Column("restrictiveness", sa.Integer(), nullable=False),
        sa.Column("spdx_id", sa.Text(), nullable=True),
        *_timestamps(),
    )
    op.bulk_insert(
        licenses,
        [
            {
                "code": row[0],
                "name": row[1],
                "name_es": row[2],
                "version": row[3],
                "url": row[4],
                "allows_commercial": row[5],
                "allows_derivatives": row[6],
                "requires_attribution": row[7],
                "requires_sharealike": row[8],
                "restrictiveness": row[9],
                "spdx_id": row[10],
            }
            for row in LICENSE_SEED
        ],
    )

    # ------------------------------------------------------------------ #
    # users
    # ------------------------------------------------------------------ #
    op.create_table(
        "users",
        _uuid_pk(),
        sa.Column("email", pg.CITEXT(), nullable=False, unique=True),
        sa.Column("cognito_sub", sa.Text(), nullable=True, unique=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("bio", sa.Text(), nullable=True),
        sa.Column("website_url", sa.Text(), nullable=True),
        sa.Column(
            "default_license",
            _enum("license_code"),
            nullable=False,
            server_default=sa.text("'CC-BY-NC-4.0'::license_code"),
        ),
        sa.Column(
            "role",
            _enum("user_role"),
            nullable=False,
            server_default=sa.text("'member'::user_role"),
        ),
        sa.Column(
            "storage_quota_bytes",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("21474836480"),
        ),
        sa.Column(
            "storage_used_bytes",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("attribution_name", sa.String(length=200), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_users_cognito_sub", "users", ["cognito_sub"])

    # ------------------------------------------------------------------ #
    # sky_objects
    # ------------------------------------------------------------------ #
    op.create_table(
        "sky_objects",
        _uuid_pk(),
        sa.Column("catalog", _enum("object_catalog"), nullable=False),
        sa.Column("catalog_number", sa.Text(), nullable=False),
        sa.Column("common_name", sa.Text(), nullable=True),
        sa.Column("common_name_es", sa.Text(), nullable=True),
        sa.Column(
            "object_type",
            _enum("object_type"),
            nullable=False,
            server_default=sa.text("'other'::object_type"),
        ),
        sa.Column("ra_deg", sa.Float(precision=53), nullable=True),
        sa.Column("dec_deg", sa.Float(precision=53), nullable=True),
        sa.Column("magnitude", sa.Float(), nullable=True),
        sa.Column("size_arcmin", sa.Float(), nullable=True),
        sa.Column(
            "aliases",
            pg.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column(
            "is_ephemeral", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("photo_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "reconstruction_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        *_timestamps(),
        sa.UniqueConstraint(
            "catalog", "catalog_number", name="uq_sky_objects_catalog_number"
        ),
    )
    op.create_index("ix_sky_objects_radec", "sky_objects", ["ra_deg", "dec_deg"])
    op.create_index(
        "ix_sky_objects_aliases_gin", "sky_objects", ["aliases"], postgresql_using="gin"
    )
    op.create_index("ix_sky_objects_common_name", "sky_objects", ["common_name"])

    # ------------------------------------------------------------------ #
    # observing_sites
    # ------------------------------------------------------------------ #
    op.create_table(
        "observing_sites",
        _uuid_pk(),
        sa.Column(
            "owner_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "location",
            Geography(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column("elevation_m", sa.Float(), nullable=True),
        sa.Column("bortle", sa.SmallInteger(), nullable=True),
        sa.Column(
            "is_public", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        *_timestamps(),
    )
    op.create_index(
        "ix_observing_sites_location_gist",
        "observing_sites",
        ["location"],
        postgresql_using="gist",
    )
    op.create_index("ix_observing_sites_owner", "observing_sites", ["owner_id"])

    # ------------------------------------------------------------------ #
    # photos
    # ------------------------------------------------------------------ #
    op.create_table(
        "photos",
        _uuid_pk(),
        sa.Column(
            "owner_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "status",
            _enum("photo_status"),
            nullable=False,
            server_default=sa.text("'uploading'::photo_status"),
        ),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "object_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("sky_objects.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Almacenamiento
        sa.Column("s3_bucket", sa.Text(), nullable=False),
        sa.Column("s3_key_original", sa.Text(), nullable=False),
        sa.Column("s3_key_preview", sa.Text(), nullable=True),
        sa.Column("s3_key_thumb", sa.Text(), nullable=True),
        sa.Column("original_bytes", sa.BigInteger(), nullable=True),
        sa.Column("checksum_sha256", sa.LargeBinary(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=True),
        sa.Column("width_px", sa.Integer(), nullable=True),
        sa.Column("height_px", sa.Integer(), nullable=True),
        sa.Column("bit_depth", sa.SmallInteger(), nullable=True),
        # Tiempo
        sa.Column("captured_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("captured_at_local", sa.DateTime(timezone=False), nullable=True),
        sa.Column("utc_offset_minutes", sa.SmallInteger(), nullable=True),
        sa.Column("time_source", _enum("time_source"), nullable=True),
        # Lugar
        sa.Column(
            "location",
            Geography(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=True,
        ),
        sa.Column("location_accuracy_m", sa.Float(), nullable=True),
        sa.Column("elevation_m", sa.Float(), nullable=True),
        sa.Column("location_source", _enum("location_source"), nullable=True),
        sa.Column(
            "location_precision",
            _enum("location_precision"),
            nullable=False,
            server_default=sa.text("'exact'::location_precision"),
        ),
        sa.Column("country_code", sa.Text(), nullable=True),
        sa.Column(
            "site_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("observing_sites.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Óptica y cámara
        sa.Column("camera_make", sa.Text(), nullable=True),
        sa.Column("camera_model", sa.Text(), nullable=True),
        sa.Column("sensor_width_mm", sa.Float(), nullable=True),
        sa.Column("sensor_height_mm", sa.Float(), nullable=True),
        sa.Column("pixel_pitch_um", sa.Float(), nullable=True),
        sa.Column("lens_model", sa.Text(), nullable=True),
        sa.Column("focal_length_mm", sa.Float(), nullable=True),
        sa.Column("focal_ratio", sa.Float(), nullable=True),
        sa.Column("aperture_mm", sa.Float(), nullable=True),
        sa.Column("exposure_seconds", sa.Float(), nullable=True),
        sa.Column("iso", sa.Integer(), nullable=True),
        sa.Column(
            "is_stacked", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("sub_frames", sa.Integer(), nullable=True),
        sa.Column("telescope_model", sa.Text(), nullable=True),
        sa.Column("mount_model", sa.Text(), nullable=True),
        sa.Column("is_tracked", sa.Boolean(), nullable=True),
        sa.Column("filter_name", sa.Text(), nullable=True),
        # Astrometría
        sa.Column(
            "is_plate_solved",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("ra_deg", sa.Float(precision=53), nullable=True),
        sa.Column("dec_deg", sa.Float(precision=53), nullable=True),
        sa.Column("field_radius_deg", sa.Float(), nullable=True),
        sa.Column("pixel_scale_arcsec", sa.Float(), nullable=True),
        sa.Column("orientation_deg", sa.Float(), nullable=True),
        sa.Column("parity", sa.SmallInteger(), nullable=True),
        sa.Column("wcs_json", pg.JSONB(), nullable=True),
        sa.Column("astrometry_job_id", sa.Text(), nullable=True),
        sa.Column("dither_phase_x", sa.Float(), nullable=True),
        sa.Column("dither_phase_y", sa.Float(), nullable=True),
        # Calidad
        sa.Column("fwhm_arcsec", sa.Float(), nullable=True),
        sa.Column("star_count", sa.Integer(), nullable=True),
        sa.Column("eccentricity", sa.Float(), nullable=True),
        sa.Column("background_adu", sa.Float(), nullable=True),
        sa.Column("snr_estimate", sa.Float(), nullable=True),
        sa.Column("bortle_estimate", sa.SmallInteger(), nullable=True),
        sa.Column("moon_illumination", sa.Float(), nullable=True),
        sa.Column("moon_separation_deg", sa.Float(), nullable=True),
        sa.Column("airmass", sa.Float(), nullable=True),
        sa.Column("quality_score", sa.Float(), nullable=True),
        # Licencia
        sa.Column(
            "license",
            _enum("license_code"),
            nullable=False,
            server_default=sa.text("'CC-BY-NC-4.0'::license_code"),
        ),
        sa.Column("license_locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attribution_name", sa.Text(), nullable=True),
        sa.Column(
            "allow_ai_training",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "allow_derivatives_in_stacks",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        # Otros
        sa.Column("embedding", Vector(768), nullable=True),
        sa.Column(
            "view_count", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "download_count", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("exif_raw", pg.JSONB(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint(
            "owner_id", "checksum_sha256", name="uq_photos_owner_checksum"
        ),
        sa.CheckConstraint(
            "quality_score IS NULL OR (quality_score >= 0 AND quality_score <= 1)",
            name="ck_photos_quality_score_range",
        ),
        sa.CheckConstraint(
            "bortle_estimate IS NULL OR (bortle_estimate BETWEEN 1 AND 9)",
            name="ck_photos_bortle_range",
        ),
        sa.CheckConstraint(
            "dec_deg IS NULL OR (dec_deg BETWEEN -90 AND 90)",
            name="ck_photos_dec_range",
        ),
        sa.CheckConstraint(
            "ra_deg IS NULL OR (ra_deg >= 0 AND ra_deg < 360)",
            name="ck_photos_ra_range",
        ),
    )
    op.create_index("ix_photos_owner_id", "photos", ["owner_id"])
    op.create_index("ix_photos_location_gist", "photos", ["location"], postgresql_using="gist")
    op.create_index("ix_photos_object_captured", "photos", ["object_id", "captured_at_utc"])
    op.create_index("ix_photos_radec", "photos", ["ra_deg", "dec_deg"])
    op.create_index("ix_photos_owner_status", "photos", ["owner_id", "status"])
    op.create_index("ix_photos_captured_at", "photos", ["captured_at_utc"])
    op.create_index(
        "ix_photos_ready_solved",
        "photos",
        ["object_id", "quality_score"],
        postgresql_where=sa.text("status = 'ready' AND is_plate_solved"),
    )
    # HNSW sobre el embedding: búsqueda visual por similitud en tiempo sublineal.
    op.execute(
        """
        CREATE INDEX ix_photos_embedding_hnsw ON photos
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        """
    )

    # ------------------------------------------------------------------ #
    # collections
    # ------------------------------------------------------------------ #
    op.create_table(
        "collections",
        _uuid_pk(),
        sa.Column(
            "owner_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "is_public", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        *_timestamps(),
    )
    op.create_index("ix_collections_owner", "collections", ["owner_id"])

    op.create_table(
        "collection_photos",
        _uuid_pk(),
        sa.Column(
            "collection_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("collections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "photo_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("photos.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False, server_default=sa.text("0")),
        *_timestamps(),
        sa.UniqueConstraint(
            "collection_id", "photo_id", name="uq_collection_photos_pair"
        ),
    )
    op.create_index(
        "ix_collection_photos_collection_position",
        "collection_photos",
        ["collection_id", "position"],
    )

    # ------------------------------------------------------------------ #
    # dataset_snapshots / training_runs / models
    # ------------------------------------------------------------------ #
    op.create_table(
        "dataset_snapshots",
        _uuid_pk(),
        sa.Column(
            "photo_ids",
            pg.ARRAY(pg.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("'{}'::uuid[]"),
        ),
        sa.Column(
            "filter_query",
            pg.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("checksum", sa.Text(), nullable=False),
        sa.Column("photo_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        *_timestamps(),
        sa.UniqueConstraint("checksum", name="uq_dataset_snapshots_checksum"),
    )

    op.create_table(
        "training_runs",
        _uuid_pk(),
        sa.Column(
            "dataset_snapshot_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("dataset_snapshots.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("git_sha", sa.Text(), nullable=False),
        sa.Column(
            "hyperparams",
            pg.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            _enum("job_status"),
            nullable=False,
            server_default=sa.text("'queued'::job_status"),
        ),
        sa.Column("final_metrics", pg.JSONB(), nullable=True),
        sa.Column("log_s3_key", sa.Text(), nullable=True),
        sa.Column("hardware", sa.Text(), nullable=True),
        *_timestamps(),
    )

    op.create_table(
        "models",
        _uuid_pk(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("architecture", _enum("model_architecture"), nullable=False),
        sa.Column("s3_key_weights", sa.Text(), nullable=False),
        sa.Column(
            "training_run_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("training_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("metrics", pg.JSONB(), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("trained_on_photo_count", sa.Integer(), nullable=True),
        sa.Column("card_markdown", sa.Text(), nullable=True),
        sa.Column(
            "respects_ai_optout",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        *_timestamps(),
        sa.UniqueConstraint("name", "version", name="uq_models_name_version"),
    )
    op.create_index(
        "ix_models_active",
        "models",
        ["architecture"],
        postgresql_where=sa.text("is_active"),
    )

    # ------------------------------------------------------------------ #
    # reconstructions
    # ------------------------------------------------------------------ #
    op.create_table(
        "reconstructions",
        _uuid_pk(),
        sa.Column(
            "requested_by",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "object_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("sky_objects.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("pipeline", sa.Text(), nullable=False),
        sa.Column("pipeline_version", sa.Text(), nullable=False),
        sa.Column(
            "model_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("models.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "params", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "status",
            _enum("job_status"),
            nullable=False,
            server_default=sa.text("'queued'::job_status"),
        ),
        sa.Column("progress", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("batch_job_id", sa.Text(), nullable=True),
        sa.Column("input_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("s3_key_result", sa.Text(), nullable=True),
        sa.Column("s3_key_preview", sa.Text(), nullable=True),
        sa.Column("s3_key_report", sa.Text(), nullable=True),
        sa.Column("s3_key_attribution", sa.Text(), nullable=True),
        sa.Column("s3_key_provenance", sa.Text(), nullable=True),
        sa.Column("metrics", pg.JSONB(), nullable=True),
        sa.Column("license", _enum("license_code"), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("compute_seconds", sa.Float(), nullable=True),
        sa.Column("cost_usd_estimate", sa.Float(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column(
            "is_public", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        *_timestamps(),
        sa.UniqueConstraint(
            "requested_by", "idempotency_key", name="uq_reconstructions_idempotency"
        ),
        sa.CheckConstraint(
            "progress >= 0 AND progress <= 1", name="ck_reconstructions_progress_range"
        ),
    )
    op.create_index(
        "ix_reconstructions_object_status", "reconstructions", ["object_id", "status"]
    )
    op.create_index(
        "ix_reconstructions_requested_by",
        "reconstructions",
        ["requested_by", "created_at"],
    )
    op.create_index(
        "ix_reconstructions_queued",
        "reconstructions",
        ["requested_by"],
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )

    op.create_table(
        "reconstruction_inputs",
        _uuid_pk(),
        sa.Column(
            "reconstruction_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("reconstructions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "photo_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("photos.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("weight", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "was_rejected", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("alignment_rms_px", sa.Float(), nullable=True),
        sa.Column("snapshot_license", _enum("license_code"), nullable=False),
        sa.Column("snapshot_attribution_name", sa.Text(), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint(
            "reconstruction_id", "photo_id", name="uq_reconstruction_inputs_pair"
        ),
        sa.CheckConstraint(
            "weight >= 0 AND weight <= 1", name="ck_reconstruction_inputs_weight_range"
        ),
    )
    op.create_index("ix_reconstruction_inputs_photo", "reconstruction_inputs", ["photo_id"])
    op.create_index(
        "ix_reconstruction_inputs_recon",
        "reconstruction_inputs",
        ["reconstruction_id", "was_rejected"],
    )

    # ------------------------------------------------------------------ #
    # audit_log
    # ------------------------------------------------------------------ #
    op.create_table(
        "audit_log",
        _uuid_pk(),
        sa.Column(
            "actor_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("payload", pg.JSONB(), nullable=True),
        sa.Column("ip_hash", sa.Text(), nullable=True),
        *_timestamps(),
    )
    op.create_index(
        "ix_audit_log_entity", "audit_log", ["entity_type", "entity_id", "created_at"]
    )
    op.create_index("ix_audit_log_actor", "audit_log", ["actor_id", "created_at"])
    op.create_index("ix_audit_log_action", "audit_log", ["action", "created_at"])

    # ------------------------------------------------------------------ #
    # Triggers
    # ------------------------------------------------------------------ #
    # updated_at automático: si se dejara al ORM, cualquier UPDATE hecho en SQL
    # crudo (migraciones de datos, scripts) dejaría el campo mintiendo.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table in (
        "users",
        "photos",
        "sky_objects",
        "observing_sites",
        "collections",
        "collection_photos",
        "reconstructions",
        "reconstruction_inputs",
        "models",
        "training_runs",
        "dataset_snapshots",
        "licenses",
        "audit_log",
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_updated_at
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
            """
        )

    # `users.storage_used_bytes` mantenido por trigger (docs/data-model.md). El
    # borrado lógico también libera cuota: `deleted_at` pasando de NULL a no NULL
    # resta los bytes.
    # Cada `op.execute` lleva **una** sentencia: asyncpg usa prepared statements y
    # rechaza varias en una sola llamada.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION sync_storage_used() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.deleted_at IS NULL AND NEW.original_bytes IS NOT NULL THEN
                    UPDATE users SET storage_used_bytes = storage_used_bytes
                        + NEW.original_bytes WHERE id = NEW.owner_id;
                END IF;
            ELSIF TG_OP = 'DELETE' THEN
                IF OLD.deleted_at IS NULL AND OLD.original_bytes IS NOT NULL THEN
                    UPDATE users SET storage_used_bytes = GREATEST(0,
                        storage_used_bytes - OLD.original_bytes) WHERE id = OLD.owner_id;
                END IF;
            ELSE
                IF OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL THEN
                    UPDATE users SET storage_used_bytes = GREATEST(0,
                        storage_used_bytes - COALESCE(OLD.original_bytes, 0))
                        WHERE id = OLD.owner_id;
                ELSIF OLD.deleted_at IS NOT NULL AND NEW.deleted_at IS NULL THEN
                    UPDATE users SET storage_used_bytes = storage_used_bytes
                        + COALESCE(NEW.original_bytes, 0) WHERE id = NEW.owner_id;
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_photos_storage_used
        AFTER INSERT OR UPDATE OR DELETE ON photos
        FOR EACH ROW EXECUTE FUNCTION sync_storage_used();
        """
    )

    # audit_log es append-only: sin UPDATE ni DELETE, ni siquiera por error humano.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_audit_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log es append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_audit_log_append_only
        BEFORE DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_audit_log_append_only ON audit_log")
    op.execute("DROP TRIGGER IF EXISTS trg_photos_storage_used ON photos")
    op.execute("DROP FUNCTION IF EXISTS reject_audit_mutation()")
    op.execute("DROP FUNCTION IF EXISTS sync_storage_used()")

    for table in (
        "audit_log",
        "reconstruction_inputs",
        "reconstructions",
        "models",
        "training_runs",
        "dataset_snapshots",
        "collection_photos",
        "collections",
        "photos",
        "observing_sites",
        "sky_objects",
        "users",
        "licenses",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_updated_at ON {table}")
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
    for name in ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {name}")
    # Las extensiones no se borran: las puede estar usando otro esquema.
