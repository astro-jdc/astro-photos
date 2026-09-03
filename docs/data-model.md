# Modelo de datos — astro-photos

Base de datos: **PostgreSQL 16 + PostGIS + pgvector** (Aurora Serverless v2 en AWS,
contenedor `postgis/postgis` en local). PostGIS por las consultas geoespaciales
("dame todas las fotos de M31 tomadas a menos de 500 km de aquí"), pgvector por la
búsqueda por similitud de embeddings de imagen.

Todas las tablas usan `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`,
`created_at`/`updated_at TIMESTAMPTZ NOT NULL DEFAULT now()`.
Todos los instantes se guardan **en UTC**; la hora local y el offset del
observador se guardan aparte porque importan para el airmass y la rotación de campo.

---

## `users`

| columna | tipo | notas |
|---|---|---|
| `id` | uuid | PK |
| `email` | citext UNIQUE NOT NULL | |
| `cognito_sub` | text UNIQUE | sujeto de Cognito; NULL en cuentas de sistema |
| `display_name` | text NOT NULL | mostrado como autoría en la licencia CC |
| `bio` | text | |
| `website_url` | text | |
| `default_license` | license_code NOT NULL DEFAULT `'CC-BY-NC-4.0'` | preferencia del usuario |
| `role` | user_role NOT NULL DEFAULT `'member'` | `member \| curator \| admin` |
| `storage_quota_bytes` | bigint NOT NULL DEFAULT 21474836480 | 20 GiB por defecto |
| `storage_used_bytes` | bigint NOT NULL DEFAULT 0 | mantenido por trigger sobre `photos`: suma `original_bytes` de las fotos no borradas. El soft-delete **libera** cuota |
| `is_active` | boolean NOT NULL DEFAULT true | |

## `photos`

La entidad central. Una fila = una exposición subida por un usuario.

| columna | tipo | notas |
|---|---|---|
| `id` | uuid | PK |
| `owner_id` | uuid → users | ON DELETE RESTRICT (no se borran fotos con derechos cedidos) |
| `status` | photo_status | `uploading \| processing \| ready \| failed \| quarantined` |
| `title` | text | |
| `description` | text | descripción libre del autor |
| `object_id` | uuid → sky_objects | NULL si no se identificó el objeto |
| **Almacenamiento** | | |
| `s3_bucket` | text NOT NULL | |
| `s3_key_original` | text NOT NULL | original inmutable, nunca se sirve directo |
| `s3_key_preview` | text | JPEG 2048 px para la galería |
| `s3_key_thumb` | text | WebP 512 px |
| `original_bytes` | bigint | |
| `checksum_sha256` | bytea NOT NULL | deduplicación; UNIQUE por `owner_id` |
| `multipart_upload_id` | text | id del multipart de S3 mientras la subida está en curso. Sin guardarlo no se puede validar que el `upload_id` que manda el cliente es el de esta foto. Índice parcial `WHERE ... IS NOT NULL` para barrer huérfanas |
| `mime_type` | text | `image/jpeg`, `image/tiff`, `image/x-canon-cr3`, `image/fits`… |
| `width_px` / `height_px` | int | |
| `bit_depth` | smallint | 8/16/32 |
| **Tiempo** | | |
| `captured_at_utc` | timestamptz | instante de inicio de la exposición, en UTC |
| `captured_at_local` | timestamp | hora de pared del observador |
| `utc_offset_minutes` | smallint | |
| `time_source` | time_source | `exif \| gps \| user \| inferred` — la confianza importa |
| **Lugar** | | |
| `location` | geography(Point,4326) | GPS del observador |
| `location_accuracy_m` | real | |
| `elevation_m` | real | altitud sobre el nivel del mar |
| `location_source` | location_source | `exif_gps \| user_pin \| named_site \| undisclosed` |
| `country_code` | char(2) | ISO 3166-1; necesario para la precisión `country`. Si es NULL, `country` degrada a `hidden` |
| `location_precision` | location_precision | `exact \| city \| country \| hidden` — privacidad del autor |
| `site_id` | uuid → observing_sites | NULL si es una ubicación puntual |
| **Óptica y cámara** | | |
| `camera_make` / `camera_model` | text | |
| `sensor_width_mm` / `sensor_height_mm` | real | de la base de datos de cámaras o del usuario |
| `pixel_pitch_um` | real | derivado; clave para el muestreo sub-píxel |
| `lens_model` | text | |
| `focal_length_mm` | real | |
| `focal_ratio` | real | f/N |
| `aperture_mm` | real | derivado: `focal_length_mm / focal_ratio`; fija el límite de difracción |
| `exposure_seconds` | real | |
| `iso` | int | |
| `is_stacked` | boolean NOT NULL DEFAULT false | true si ya es un apilado |
| `sub_frames` | int | nº de subs si `is_stacked` |
| `telescope_model` | text | |
| `mount_model` | text | |
| `is_tracked` | boolean | montura de seguimiento sí/no |
| `filter_name` | text | `none \| UV/IR-cut \| L-eNhance \| Ha \| OIII \| SII \| L \| R \| G \| B` |
| **Astrometría** (rellenada por el worker de plate solving) | | |
| `is_plate_solved` | boolean NOT NULL DEFAULT false | |
| `ra_deg` / `dec_deg` | double precision | centro del campo, J2000 |
| `field_radius_deg` | real | |
| `pixel_scale_arcsec` | real | arcsec/píxel |
| `orientation_deg` | real | ángulo de posición |
| `parity` | smallint | +1 / -1 |
| `wcs_json` | jsonb | WCS completo (CTYPE/CRVAL/CRPIX/CD) para reproyección |
| `dither_phase_x` / `dither_phase_y` | real | fase sub-píxel derivada del WCS, en [0,1). Es lo que mide la **diversidad de muestreo**: `selection.py` la maximiza porque sin dither diverso no hay super-resolución que recuperar |
| `astrometry_job_id` | text | referencia al job de astrometry.net / ASTAP |
| **Calidad** (worker de QA de imagen) | | |
| `fwhm_arcsec` | real | seeing efectivo medido sobre las estrellas |
| `star_count` | int | |
| `eccentricity` | real | 0 = redondo, >0.5 = arrastre de estrellas |
| `background_adu` | real | |
| `snr_estimate` | real | |
| `bortle_estimate` | smallint | 1–9, del mapa de contaminación lumínica en `location` |
| `moon_illumination` | real | 0–1, calculado de `captured_at_utc` |
| `moon_separation_deg` | real | |
| `airmass` | real | calculado de alt/az en el instante y lugar |
| `quality_score` | real | 0–1 agregado; ordena la selección de frames |
| **Licencia** | | |
| `license` | license_code NOT NULL DEFAULT `'CC-BY-NC-4.0'` | ver `docs/licensing.md` |
| `license_locked_at` | timestamptz | tras la primera descarga por terceros la licencia deja de poder endurecerse |
| `attribution_name` | text | cómo quiere el autor ser citado |
| `allow_ai_training` | boolean NOT NULL DEFAULT true | opt-out explícito e independiente de la licencia |
| `allow_derivatives_in_stacks` | boolean NOT NULL DEFAULT true | |
| **Otros** | | |
| `embedding` | vector(768) | pgvector; búsqueda visual por similitud |
| `view_count` / `download_count` | bigint | |
| `exif_raw` | jsonb | EXIF/XMP crudo tal cual, para poder reprocesar |
| `deleted_at` | timestamptz | soft-delete. Libera cuota del propietario, pero la foto sigue sirviendo a las reconstrucciones publicadas que la usaron |

Índices: GIST sobre `location`, BTREE sobre `(object_id, captured_at_utc)`,
BTREE sobre `(ra_deg, dec_deg)`, HNSW sobre `embedding`,
índice parcial `WHERE status='ready' AND is_plate_solved`.

## `sky_objects`

Catálogo canónico de objetos (Messier, NGC/IC, Caldwell, planetas, cometas).
`id`, `catalog` (`M`,`NGC`,`IC`,`SH2`,`solar`), `catalog_number`, `common_name`,
`common_name_es`, `object_type` (galaxy/nebula/cluster/planet/comet/moon/other),
`ra_deg`, `dec_deg`, `magnitude`, `size_arcmin`, `aliases text[]`.
`catalog` admite `M`, `NGC`, `IC`, `C` (Caldwell), `SH2` y `solar`.
Se siembra desde OpenNGC (CC-BY-SA-4.0). Los objetos móviles (planetas, cometas)
tienen `is_ephemeral=true` y no llevan RA/Dec fijas.

## `observing_sites`

Sitios con nombre y reutilizables ("Observatorio del Teide", "el balcón de casa").
`owner_id` (NULL = público), `name`, `location geography(Point,4326)`,
`elevation_m`, `bortle`, `is_public`.

## `collections` y `collection_photos`

Álbumes creados por usuarios. N:M con `position`.

## `reconstructions`

Un trabajo de reconstrucción: N fotos de entrada → 1 imagen de salida.

| columna | tipo | notas |
|---|---|---|
| `id` | uuid | PK |
| `requested_by` | uuid → users | |
| `object_id` | uuid → sky_objects | objetivo |
| `pipeline` | text NOT NULL | `classical-stack-v1`, `drizzle-v1`, `burst-sr-v1`… |
| `pipeline_version` | text NOT NULL | git sha del código de `models/` que corrió |
| `model_id` | uuid → models | NULL en pipelines puramente clásicos |
| `params` | jsonb | parámetros efectivos (drizzle pixfrac, kernel, rechazo…) |
| `status` | job_status | `queued \| running \| succeeded \| failed \| cancelled` |
| `progress` | real | 0–1, lo publica el SSE |
| `idempotency_key` | text | UNIQUE por `requested_by`; evita duplicar un job al reintentar |
| `is_public` | boolean NOT NULL DEFAULT true | si aparece en la galería pública |
| `batch_job_id` | text | id del job en AWS Batch |
| `input_count` | int | |
| `s3_key_result` | text | salida a resolución completa (TIFF 32-bit + FITS con WCS) |
| `s3_key_preview` | text | |
| `s3_key_report` | text | informe HTML con métricas y contribuciones |
| `s3_key_attribution` | text | `ATTRIBUTION.md` |
| `s3_key_provenance` | text | `provenance.json` firmado |
| `s3_key_uncertainty` | text | mapa de incertidumbre por píxel. **Obligatorio** si `model_id` no es NULL: un pipeline aprendido no puede publicar resultado sin él |
| `s3_key_weight_map` | text | mapa de peso de la coadición |
| `metrics` | jsonb | `fwhm_arcsec`, `snr_gain_db`, `effective_pixel_scale`, `psnr`, `ssim` |
| `license` | license_code | **la más restrictiva** de todas las entradas (ver abajo) |
| `error_message` | text | |
| `started_at` / `finished_at` | timestamptz | |
| `compute_seconds` / `cost_usd_estimate` | real | |

## `reconstruction_inputs`

Procedencia. **Cada foto que entra en una reconstrucción deja fila aquí para siempre.**
`reconstruction_id`, `photo_id`, `weight` (contribución efectiva 0–1),
`was_rejected` (boolean) + `rejection_reason`, `alignment_rms_px`,
`snapshot_license` y `snapshot_attribution_name` (licencia y autoría de la foto *en el
momento* de usarla — un cambio posterior no reescribe una reconstrucción publicada).

## `models`

Pesos entrenados versionados.
`name`, `version`, `architecture` (`bipnet`,`burstormer`,`rbsr`,`edsr-burst`,`custom`),
`s3_key_weights`, `training_run_id`, `metrics jsonb`, `is_active`,
`trained_on_photo_count`, `card_markdown` (model card),
`respects_ai_optout` (boolean, siempre true en modelos publicados).

## `training_runs`

`dataset_snapshot_id`, `git_sha`, `hyperparams jsonb`, `started_at`, `finished_at`,
`status`, `final_metrics jsonb`, `log_s3_key`, `hardware` (`xpu-arc-b70`, `g5.xlarge`…).

## `dataset_snapshots`

Un snapshot inmutable de qué fotos formaron un conjunto de entrenamiento,
para reproducibilidad y para poder purgar a quien revoque el consentimiento.
`photo_ids uuid[]`, `filter_query jsonb`, `checksum`, `photo_count`.

## `licenses` (tabla de referencia, semilla fija)

`code` (PK), `name`, `version`, `url`, `allows_commercial`, `allows_derivatives`,
`requires_attribution`, `requires_sharealike`, `restrictiveness` (int, para el cálculo
de compatibilidad), `spdx_id`.

## `audit_log`

`actor_id`, `action`, `entity_type`, `entity_id`, `payload jsonb`, `ip_hash`.
Append-only. Toda mutación de licencia, borrado y descarga masiva queda aquí.

---

## Regla de compatibilidad de licencias

La salida de una reconstrucción es una **obra derivada de todas las entradas**.
El backend calcula la licencia resultante como la **combinación más restrictiva**:

1. Si alguna entrada es `NC` → la salida es `NC`.
2. Si alguna entrada es `SA` → la salida es `SA` (y hereda la versión más alta).
3. Si alguna entrada es `ND` → **esa foto no puede entrar**: se rechaza en la
   validación del job, no se degrada la salida.
4. Si alguna entrada tiene `allow_derivatives_in_stacks=false` → se rechaza.
5. Atribución: siempre se genera el fichero `ATTRIBUTION.md` y los créditos EXIF/XMP
   con todos los autores, aunque todas las entradas fueran CC0.

Esta lógica vive en un único sitio (`backend/app/domain/licensing.py`) y está
cubierta por tests de tabla exhaustivos.
