"""Índice GIST para la búsqueda por cono celeste.

Revision ID: 0003_sky_cone_index
Revises: 0002_multipart_maps
Create Date: 2026-09-03

La búsqueda por cono —la consulta central del producto— filtraba con
``ST_DistanceSphere(...) <= radio``. Es correcta, pero **ninguna función de
distancia esférica es acelerable por índice**, así que la consulta hacía un escaneo
secuencial de toda la tabla: medido sobre 500 000 fotos, 610 ms y 14 070 bloques
leídos.

``ST_DWithin`` sobre ``geography`` **sí** usa índice, y con ``use_spheroid => false``
es exactamente el mismo predicado: PostGIS reescribe ``ST_DistanceSphere(a, b)`` a
``ST_Distance(a::geography, b::geography, false)``, de modo que el cambio no mueve
ni una fila. Con este índice, la misma consulta baja a 9,7 ms leyendo 401 bloques.

Migración **expand**: solo crea un índice. No toca datos, no reescribe la tabla y el
código anterior sigue funcionando contra este esquema.

La expresión del índice tiene que ser **idéntica** a la de la consulta o PostgreSQL
no lo usará en silencio. Aquí va escrita literal, como toda migración: una migración
es una foto congelada del esquema, no un reflejo de los modelos de hoy. Quien vigila
que no se separen es el test de plan (`EXPLAIN` sin `Seq Scan`), que falla en cuanto
la consulta deja de encajar con el índice.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_sky_cone_index"
down_revision: str | None = "0002_multipart_maps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "ix_photos_sky_gist"


#: La expresión, literal. Debe coincidir con `app.models.photo.sky_geography`.
SKY_EXPRESSION = (
    "CAST(ST_SetSRID(ST_MakePoint(ra_deg - 180.0, dec_deg), 4326) "
    "AS geography(POINT,4326))"
)


def upgrade() -> None:
    op.create_index(
        INDEX_NAME,
        "photos",
        [sa.text(SKY_EXPRESSION)],
        postgresql_using="gist",
        postgresql_where=sa.text("ra_deg IS NOT NULL AND dec_deg IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="photos")
