# Contrato de API — astro-photos

REST/JSON bajo `/api/v1`. OpenAPI 3.1 generado por FastAPI en `/api/v1/openapi.json`.
El frontend **no escribe tipos a mano**: `pnpm run gen:api` los genera desde ese schema.

Autenticación: `Authorization: Bearer <JWT de Cognito>`. El backend valida contra el
JWKS del User Pool. Rutas marcadas 🔓 son públicas.

Errores: RFC 9457 `application/problem+json`
(`{type, title, status, detail, instance, errors[]}`).

Paginación: cursor. `?limit=50&cursor=<opaco>` → `{items: [...], next_cursor: str|null}`.
**No hay total.** Es deliberado: contar con filtros geoespaciales y de embedding sobre
millones de filas es caro y el número envejece mal. La UI dice "N cargadas", no
"N encontradas". Si algún día hace falta un total, será estimado y con ese nombre.

### Ofuscación de la ubicación

Toda respuesta que incluya una foto aplica `photos.location_precision` **en el
serializador**, nunca en la query. El cliente no reimplementa esto:

| `location_precision` | qué devuelve `location` | `location_label` |
|---|---|---|
| `exact` | lat/lon reales y `accuracy_m` | topónimo si se conoce |
| `city` | lat/lon redondeadas a 0.1° (~11 km) | ciudad, país |
| `country` | centroide del país | país |
| `hidden` | `null` | `null` |

Si `country_code` es desconocido, `country` degrada a `hidden`: nunca se inventa un
centroide. La ofuscación se aplica también al EXIF del fichero servido, a la preview
y al resultado de una reconstrucción.

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

Cuando la subida es multipart, el cliente cierra con
`POST /photos/{id}/uploads/complete-multipart` → `{upload_id, parts: [{part_number, etag}]}`,
que llama a `CompleteMultipartUpload` en S3 y deja la foto lista para el paso 3.
Sin esta llamada el objeto queda incompleto y la regla de ciclo de vida lo borra.

| método | ruta | descripción |
|---|---|---|
| `GET` | `/photos/{id}` 🔓 | metadata completa, con la ubicación ya ofuscada |
| `PATCH` | `/photos/{id}` | edita metadata. Mientras `license_locked_at` sea NULL la licencia se cambia libremente; una vez fijado, solo puede **relajarse** (bajar de restrictividad). Ver `docs/licensing.md`. |
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
&owner=<uuid>                fotos de un usuario ("mis fotos")
```

`GET /photos/similar/{id}` 🔓 — vecinos por embedding (pgvector HNSW).

`GET /objects?q=<texto>` 🔓 / `GET /objects/{id}` 🔓 — catálogo con búsqueda por nombre
o alias (el autocompletado del formulario de subida usa `q`), con `photo_count` y
`reconstruction_count`. `GET /objects/{id}/coverage` 🔓 devuelve el **mapa de
cobertura**: cuántas fotos hay por celda de tiempo × latitud × focal. Es lo que
alimenta el widget "a este objeto le faltan tomas desde el hemisferio sur". Responde
`{period_bin, lat_bin_size_deg, focal_bins_mm, cells: [{period, lat_bin, focal_bin, count, best_quality}],
sites: [{lat, lon, count}], gaps: [{reason, description}]}`.

Los puntos de `sites[]` vienen **ya ofuscados**, y el orden de las operaciones importa:
se agrupa por `location_precision` **antes** de agregar, nunca después. Agregar primero
y ofuscar el centroide después filtraría posiciones exactas por la puerta de atrás,
que es justo lo que la regla de privacidad existe para evitar.

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
Rate limit: 5 jobs en cola por usuario, 20 al día (config por entorno). Los límites
vigentes viajan en `GET /me` dentro de `quota`, para que el cliente pueda deshabilitar
el botón en vez de descubrirlo con un 429.

`GET /reconstructions` acepta `?object_id=<uuid>` y `?mine=true`.

Una reconstrucción de **una sola foto** se rechaza con 422: no es una reconstrucción.

### Formas de respuesta

`POST /reconstructions/preview` — el cliente **debe** llamar a esto antes de dejar
lanzar nada, y enseñar lo que devuelve:

```jsonc
{
  "selected": [{"photo_id", "weight", "quality_score", "fwhm_arcsec", "pixel_scale_arcsec"}],
  // Bloqueo y rechazo son cosas distintas y no deben mezclarse:
  // un bloqueo aborta el job entero con 422 porque no hay forma legal de seguir;
  // un rechazo solo deja ese frame fuera y el job continúa.
  "blocked":  [{"photo_id", "reason"}],   // no_derivatives | stack_opt_out
  "rejected": [{"photo_id", "reason"}],   // unsolved | too_low_quality | duplicate |
                                          // no_overlap | saturated
  "resulting_license": "CC-BY-NC-4.0",
  "estimated_compute_seconds": 420,
  "estimated_queue_seconds": 180,         // arranque en frío de Batch spot
  "estimated_cost_usd": 0.31,
  "cost_basis": "batch-spot-g5-eu-west-1",
  "uses_learned_model": false             // si true, la UI exige AiDisclosure
}
```

`GET /reconstructions/{id}/events` (SSE) — un evento por cambio de estado o de etapa:

```jsonc
{"status": "running", "progress": 0.42, "stage": "coadd",
 "message": "Coadición óptima, 128/300 frames", "metrics": {...}, "at": "..."}
```

`GET /reconstructions/{id}/result` 🔓:

```jsonc
{
  "result_url", "preview_url", "uncertainty_map_url", "weight_map_url",
  "provenance_json_url", "attribution_md_url",
  "best_single_frame": {"photo_id", "preview_url", "fwhm_arcsec", "snr_estimate"},
  "metrics": {"fwhm_arcsec", "snr_gain_db", "effective_pixel_scale", "input_count"},
  "license", "pipeline", "pipeline_version", "model_id"
}
```

`best_single_frame` no es un extra: es la **comparación honesta**. Sin ella el usuario
no puede juzgar si la reconstrucción aportó algo, y la interfaz estaría afirmando una
mejora que no enseña.

## Modelos 🔓 (lectura)

`GET /models`, `GET /models/{id}` (model card, métricas, dataset snapshot).
`POST /models/{id}/activate` — solo `admin`.

## Estadísticas 🔓

`GET /stats` → `{photo_count, object_count, reconstruction_count, contributor_count,
total_exposure_seconds}`. Alimenta los contadores de la portada. Cacheado 5 minutos.

## Licencias 🔓

`GET /licenses` — catálogo: `[{code, name, version, url, allows_commercial,
allows_derivatives, requires_attribution, requires_sharealike, restrictiveness, spdx_id}]`,
la tabla `licenses` de `docs/data-model.md`, envuelto en
`{items: [...], default_license: "CC-BY-NC-4.0"}`. `default_license` es la del usuario
si viene autenticado, y la del sistema si no: así el formulario de subida sabe qué
preseleccionar con una sola llamada, sin cruzar `/licenses` con `/me`.
`POST /licenses/resolve` → `{photo_ids[]}` → `{resulting_license, blocked: [{photo_id, reason}]}`.
Es la misma función de dominio que usa el motor de reconstrucción; se expone para
que el frontend pueda avisar antes de dejar pulsar el botón.

## Salud

`GET /healthz` (liveness), `GET /readyz` (DB + S3 + cola).
