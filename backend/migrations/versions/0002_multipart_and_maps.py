"""Multipart abierto y mapas de incertidumbre/peso.

Revision ID: 0002_multipart_maps
Revises: 0001_initial
Create Date: 2026-09-03

Migración **expand**: solo añade columnas anulables, así que la versión anterior del
código sigue funcionando contra este esquema (``docs/branching.md``: expand →
migrate → contract, en despliegues separados).

* ``photos.multipart_upload_id`` — el ``UploadId`` de S3 mientras la subida grande
  está abierta. Sin él no se puede validar que el ``upload_id`` que manda el cliente
  en ``POST /photos/{id}/uploads/complete-multipart`` es el de esa foto.
* ``reconstructions.s3_key_uncertainty`` / ``s3_key_weight_map`` — los dos mapas que
  ``GET /reconstructions/{id}/result`` publica junto al resultado.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_multipart_maps"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("photos", sa.Column("multipart_upload_id", sa.Text(), nullable=True))
    op.add_column(
        "reconstructions", sa.Column("s3_key_uncertainty", sa.Text(), nullable=True)
    )
    op.add_column(
        "reconstructions", sa.Column("s3_key_weight_map", sa.Text(), nullable=True)
    )
    # Índice parcial: el barrido de subidas huérfanas busca justo estas filas.
    op.create_index(
        "ix_photos_open_multipart",
        "photos",
        ["owner_id"],
        postgresql_where=sa.text("multipart_upload_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_photos_open_multipart", table_name="photos")
    op.drop_column("reconstructions", "s3_key_weight_map")
    op.drop_column("reconstructions", "s3_key_uncertainty")
    op.drop_column("photos", "multipart_upload_id")
