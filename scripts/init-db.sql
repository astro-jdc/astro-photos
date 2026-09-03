-- Extensiones que necesita el modelo de datos. Ver docs/data-model.md.
-- pgcrypto aporta gen_random_uuid(), que es lo que usan los defaults de las tablas.
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS vector;
