# Contrato de API — astro-photos

REST/JSON bajo `/api/v1`. OpenAPI 3.1 generado por FastAPI en `/api/v1/openapi.json`.
El frontend **no escribe tipos a mano**: `pnpm run gen:api` los genera desde ese schema.

Autenticación: `Authorization: Bearer <JWT de Cognito>`. El backend valida contra el
JWKS del User Pool. Rutas marcadas 🔓 son públicas.

Errores: RFC 9457 `application/problem+json`
(`{type, title, status, detail, instance, errors[]}`).

Paginación: cursor. `?limit=50&cursor=<opaco>` → `{items: [...], next_cursor: str|null}`.

---

## Auth y usuarios

| método | ruta | descripción |
|---|---|---|
| `GET` | `/me` | perfil del usuario actual + cuota |
| `PATCH` | `/me` | `display_name`, `bio`, `website_url`, `default_license` |
| `GET` | `/users/{id}` 🔓 | perfil público + fotos |

## Subida de fotos (flujo en 3 pasos, el binario nunca pasa por el backend)

1. `POST /photos/uploads` → `{filename, size_bytes, mime_type, checksum_sha256}`
   Valida cuota, tipo y duplicado. Devuelve
   `{photo_id, upload_url, fields, expires_at}` — un **POST presignado de S3**
   (no PUT: permite forzar `content-length-range` y tags del lado del servidor).
   Para ficheros > 100 MB devuelve en su lugar `{multipart: {upload_id, part_urls[]}}`.
2. El cliente sube directo a S3 (`s3://astro-photos-{env}-uploads/staging/{user}/{photo_id}`).
3. `POST /photos/{id}/complete` → `{title, description, license, captured_at_local,
   utc_offset_minutes, location:{lat,lon,accuracy_m,elevation_m}, location_precision,
   object_id?, site_id?, equipment:{...}, allow_ai_training, allow_derivatives_in_stacks}`
   Marca `status=processing` y encola el pipeline de ingesta.
   Los campos que el cliente no envía se rellenan desde el EXIF; los que sí envía
   **ganan** al EXIF y quedan marcados con `*_source='user'`.

El evento `s3:ObjectCreated` sobre `staging/` dispara además una Lambda de
verificación (tamaño, magic bytes, antivirus) que pone `quarantined` si algo no cuadra.

| método | ruta | descripción |
|---|---|---|
| `GET` | `/photos/{id}` 🔓 | metadata completa (la ubicación se ofusca según `location_precision`) |
| `PATCH` | `/photos/{id}` | edita metadata; la licencia solo puede **relajarse** si `license_locked_at` no es NULL |
| `DELETE` | `/photos/{id}` | soft-delete; rechazado con 409 si la foto participa en reconstrucciones publicadas |
| `GET` | `/photos/{id}/download` | 302 a URL de CloudFront firmada; incrementa `download_count` y audita |

## Búsqueda 🔓

`GET /photos` con filtros combinables:

```
?object=M31                  alias o id del objeto
&ra=10.68&dec=41.27&radius=2 cono en el cielo, grados
&near=28.30,-16.51&km=50     cerca de una posición en la Tierra
&from=2026-01-01&to=2026-03-01
&min_focal=200&max_focal=800
&filter=Ha
&license=CC-BY-4.0,CC0-1.0   compatibles con lo que quiero hacer
&usable_for=commercial       atajo: solo licencias que permiten uso comercial
&min_quality=0.6
&tracked=true
&sort=quality|recent|nearest
```

`GET /photos/similar/{id}` 🔓 — vecinos por embedding (pgvector HNSW).

`GET /objects` 🔓 / `GET /objects/{id}` 🔓 — catálogo, con `photo_count` y
`reconstruction_count`. `GET /objects/{id}/coverage` 🔓 devuelve el **mapa de
cobertura**: cuántas fotos hay por celda de tiempo × latitud × focal. Es lo que
alimenta el widget "a este objeto le faltan tomas desde el hemisferio sur".

## Reconstrucciones

| método | ruta | descripción |
|---|---|---|
| `POST` | `/reconstructions/preview` | body igual que el POST real; **no encola nada**: devuelve el plan (fotos seleccionadas, fotos rechazadas y por qué, licencia resultante, coste y tiempo estimados). El frontend siempre llama a esto primero. |
| `POST` | `/reconstructions` | crea el job. `{object_id?, photo_ids[]?, selector?, pipeline, params}` — o das la lista explícita, o das un `selector` con la misma sintaxis que la búsqueda y el backend elige los mejores N frames. |
| `GET` | `/reconstructions/{id}` | estado, progreso (0–1), métricas parciales |
| `GET` | `/reconstructions/{id}/events` | SSE con el progreso en vivo |
| `GET` | `/reconstructions/{id}/inputs` | procedencia: qué entró, con qué peso, qué se descartó |
| `GET` | `/reconstructions/{id}/result` 🔓 | URL firmada del TIFF/FITS + `ATTRIBUTION.md` |
| `DELETE` | `/reconstructions/{id}` | cancela si está en cola o corriendo |
| `GET` | `/reconstructions` 🔓 | galería pública de reconstrucciones |

`POST /reconstructions` responde **202** con `Location: /api/v1/reconstructions/{id}`.
Rate limit: 5 jobs en cola por usuario, 20 al día (config por entorno).

## Modelos 🔓 (lectura)

`GET /models`, `GET /models/{id}` (model card, métricas, dataset snapshot).
`POST /models/{id}/activate` — solo `admin`.

## Licencias 🔓

`GET /licenses` — catálogo con flags.
`POST /licenses/resolve` → `{photo_ids[]}` → `{resulting_license, blocked: [{photo_id, reason}]}`.
Es la misma función de dominio que usa el motor de reconstrucción; se expone para
que el frontend pueda avisar antes de dejar pulsar el botón.

## Salud

`GET /healthz` (liveness), `GET /readyz` (DB + S3 + cola).
