# La imagen oficial de PostGIS no trae pgvector y la de pgvector no trae PostGIS.
# El modelo de datos necesita las dos (ver docs/data-model.md), así que las juntamos.
FROM docker.io/postgis/postgis:16-3.4

RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-16-pgvector \
 && rm -rf /var/lib/apt/lists/*
