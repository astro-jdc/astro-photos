# 0002 — PostgreSQL + PostGIS + pgvector frente a DynamoDB

Estado: Aceptado · 2026-09-03

## Contexto

Las consultas centrales del producto son intrínsecamente multidimensionales:

- geoespaciales sobre la Tierra ("fotos tomadas a menos de 50 km de aquí"),
- de cono sobre la esfera celeste ("todo lo que caiga a menos de 2° de RA 10.68 Dec 41.27"),
- por rango temporal, por focal, por filtro y por licencia compatible,
- y por similitud de embedding.

Y casi siempre **combinadas**: "dame las mejores 200 tomas de M31, de menos de 3
arcsec de FWHM, con licencia compatible con uso comercial, repartidas por latitud".

## Decisión

PostgreSQL 16 con **PostGIS** (geografía terrestre y footprints celestes como
polígonos esféricos), **pgvector** con índice HNSW (similitud visual) y **citext**.
En AWS, Aurora Serverless v2.

## Consecuencias

- Una sola consulta SQL resuelve filtros que en un almacén clave-valor serían varias
  tablas de índice mantenidas a mano y consistentes solo a ratos.
- Transacciones reales: crear una reconstrucción y sus `reconstruction_inputs` es
  atómico, que es un requisito legal, no una comodidad.
- Aurora Serverless v2 escala a 0.5 ACU y se auto-pausa: staging cuesta céntimos.
- Hay que gestionar migraciones y conexiones (pool, límites de Aurora), cosa que
  DynamoDB no exige.

## Alternativas descartadas

- **DynamoDB.** El patrón de acceso no es de clave conocida; es analítico y facetado.
  Habría exigido replicar a OpenSearch de todos modos, duplicando el coste.
- **PostgreSQL + OpenSearch desde el día uno.** Complejidad prematura: PostGIS y
  pgvector aguantan de sobra hasta el primer millón de fotos. Se revisará entonces.
