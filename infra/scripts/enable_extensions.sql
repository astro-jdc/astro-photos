-- astro-photos — extensiones obligatorias de la base de datos.
--
-- Aurora PostgreSQL 16 trae los binarios de estas extensiones, pero NO las crea:
-- hay que hacerlo una vez por base de datos, con un rol que tenga rds_superuser.
-- Se ejecuta desde la tarea de ECS `db-bootstrap` ANTES de la primera migración
-- de Alembic (ver infra/stacks/api_stack.py y .github/workflows/deploy-*.yml).
--
-- El contenido de este fichero se publica tal cual en el parámetro de SSM
-- /astro-photos/<env>/db/enable-extensions-sql para que la tarea lo lea sin
-- necesidad de llevarlo dentro de la imagen.
--
-- Es idempotente: se puede volver a ejecutar en cada despliegue sin efectos.

-- Consultas geoespaciales: "fotos de M31 a menos de 500 km de aquí".
CREATE EXTENSION IF NOT EXISTS postgis;

-- Búsqueda por similitud de embeddings de imagen (vector(768), índice HNSW).
CREATE EXTENSION IF NOT EXISTS vector;

-- users.email es citext UNIQUE: comparación de email sin distinguir mayúsculas.
CREATE EXTENSION IF NOT EXISTS citext;

-- gen_random_uuid() como DEFAULT de todas las claves primarias.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Diagnóstico de consultas lentas (la usa el dashboard de CloudWatch).
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

SELECT extname, extversion
  FROM pg_extension
 WHERE extname IN ('postgis', 'vector', 'citext', 'pgcrypto', 'pg_stat_statements')
 ORDER BY extname;
