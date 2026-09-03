# Informe de QA — integración de los cuatro componentes

**Fecha:** 2026-09-03 · **Autor:** agente `qa-tester` · **Entorno:** Fedora 44, SELinux
en `Enforcing`, podman 5 + podman-compose, Python 3.12, Node 22 / pnpm 11.

> **Veredicto: no está listo para staging todavía.** Se han encontrado y arreglado
> **cuatro fallos que rompían funcionalidad central** (dos endpoints públicos que
> devolvían 500 el 100% de las veces, el entorno de desarrollo que no arrancaba, y
> las sondas de salud de ECS/ALB apuntando a rutas inexistentes). Queda **un fallo
> abierto de coherencia lectura-tras-escritura** que afecta al flujo de subida y que
> no se puede arreglar sin una decisión de diseño sobre el ciclo de vida de la
> transacción. Con ese arreglo y el contrato de tipos ya reconciliado, sí.

---

## 1. Resumen ejecutivo

| | antes | después |
|---|---|---|
| `make dev` / `podman-compose up` | **no arranca** (db y sqs mueren) | 4/4 contenedores sanos |
| `/api/v1/readyz` | **503** (`s3` y `queue` KO) | 200, los tres checks en verde |
| `GET /photos/{id}` | **500 siempre** | 200 |
| `GET /photos/{id}/download` | **500 siempre** | 302 |
| `GET /photos?dec=0` (y 3 filtros más) | **500** | 422 `problem+json` |
| Sondas de salud de infra | `/healthz` → **404** | `/api/v1/healthz` → 200 |
| `frontend/app/types/api.gen.ts` | placeholder a mano, 548 líneas | generado del OpenAPI, 5306 líneas |
| Licencia en `ATTRIBUTION.md` / FITS | **ausente** | presente y verificada contra el backend |
| Tests transversales | 0 | 121 (`tests/`, 2 784 líneas) |

Lo que **sí** funciona y conviene decirlo sin inflarlo: el flujo de subida completo
contra MinIO real funciona de punta a punta (ticket → POST presignado → complete),
incluido el `content-length-range`; la privacidad de la ubicación se respeta por los
**siete** caminos que he recorrido; la reproducibilidad bit a bit aguanta barajar la
entrada; y el copy del producto es físicamente honesto.

---

## 2. Fase 1 — verificación de lo que se decía que pasaba

Todo lo que los cuatro agentes reportaban en verde estaba efectivamente en verde.
Salida exacta, antes de tocar nada:

```
$ backend/.venv/bin/ruff check backend
All checks passed!
$ backend/.venv/bin/mypy backend/app
Success: no issues found in 71 source files
$ backend/.venv/bin/pytest backend/tests -q
681 passed, 21 skipped, 2 warnings in 3.07s

$ models/.venv/bin/ruff check models
All checks passed!
$ models/.venv/bin/pytest models/tests -q
185 passed, 5 skipped in 23.14s

$ infra/.venv/bin/python -m pytest infra/tests -q
107 passed in 16.67s

$ cd frontend && pnpm lint && pnpm typecheck && pnpm test && pnpm build
(lint: sin salida)  (typecheck: sin errores)
Test Files  8 passed (8)
     Tests  91 passed (91)
✨ Build complete!
```

**Números reales: 681 + 185 + 107 backend/models/infra, y 91 en el frontend.**

Las 21 saltadas del backend eran los tests de integración de esquema y migraciones,
que se saltan si no hay `DATABASE_URL_TEST`. Con el stack levantado pasan las 21:

```
$ DATABASE_URL_TEST=postgresql+asyncpg://astro:astro@localhost:5432/astrophotos \
  backend/.venv/bin/pytest backend/tests/integration -q
21 passed in 11.22s
```

Es decir: **nada de lo que se afirmaba era falso**. Todos los fallos que siguen
aparecieron al ejecutar cosas que ninguna de las cuatro suites ejecutaba.

---

## 3. Fallos encontrados

### 3.1 — CRÍTICO · `GET /photos/{id}` devolvía 500 para toda foto visible

**Fichero:** `backend/app/repositories/photo.py:304` (`increment_view`)

`read_photo` carga la foto, incrementa el contador de visitas con un `UPDATE`
masivo y luego serializa. Con la estrategia por defecto de SQLAlchemy, ese `UPDATE`
**expira** el objeto `Photo` recién cargado; el primer acceso a un atributo durante
la serialización dispara un refresco perezoso fuera del greenlet de asyncio:

```
sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called;
can't call await_only() here. Was IO attempted in an unexpected place?
  File "backend/app/api/v1/photos.py", line 141, in read_photo
  File "backend/app/services/photo.py", line 144, in to_out
```

Reproducción:

```bash
# con el stack arriba y una foto subida
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8000/api/v1/photos/$PHOTO_ID
# antes: 500      después: 200
```

Es la ficha de foto: la página a la que enlaza cada tarjeta de la galería y la que
el frontend abre justo después de subir. Fallaba el 100% de las veces.

**Arreglado** con `.execution_options(synchronize_session=False)`. El contador sigue
subiendo (test `test_el_contador_de_visitas_sube`).

### 3.2 — CRÍTICO · `GET /photos/{id}/download` devolvía 500 (dos causas)

**Ficheros:** `backend/app/repositories/photo.py:297` y `backend/app/api/v1/photos.py:186`

Dos fallos encadenados en el mismo endpoint:

1. **El mismo `MissingGreenlet`** en `increment_download`, y aquí con una
   consecuencia peor: justo después, `services/photo.py:283-284` escribe
   `license_locked_at` sobre ese mismo objeto expirado. O sea que **la regla de
   congelado de licencia de `docs/licensing.md` no se aplicaba nunca**.

2. **`UnicodeEncodeError` en la cabecera `X-Attribution`.** Las cabeceras HTTP se
   serializan en latin-1, y `PhotoService.attribution_line` (`services/photo.py:300`)
   lleva una raya U+2014 **fija en la plantilla**:

   ```python
   return f'"{title}" — {name} ({photo.license.value})'
   ```

   ```
   UnicodeEncodeError: 'latin-1' codec can't encode character '—'
     File "backend/app/api/v1/photos.py", line 186, in download_photo
       response.headers["X-Attribution"] = attribution
   ```

   No era un caso raro con nombres exóticos: fallaba **toda** descarga, porque la
   raya está en todas las líneas de crédito.

**Arreglados.** El congelado de licencia ahora funciona de verdad, verificado de
punta a punta (`test_una_descarga_de_un_tercero_congela_la_licencia`): tras la
descarga de un tercero, endurecer la licencia da 422 y relajarla da 200.

### 3.3 — ALTO · Cuatro filtros de búsqueda devolvían 500 en vez de 422

**Fichero:** `backend/app/api/v1/search.py:100`

`PhotoSearchQuery` se valida **a mano** dentro del handler, así que la
`ValidationError` de pydantic no pasa por el manejador de FastAPI y salía como error
interno. Lo encontró schemathesis. Cuatro entradas de usuario perfectamente normales:

```bash
curl -o /dev/null -w '%{http_code}\n' 'http://127.0.0.1:8000/api/v1/photos?dec=0'
curl -o /dev/null -w '%{http_code}\n' 'http://127.0.0.1:8000/api/v1/photos?min_focal=100&max_focal=50'
curl -o /dev/null -w '%{http_code}\n' 'http://127.0.0.1:8000/api/v1/photos?from=2027-01-01&to=2020-01-01'
curl -o /dev/null -w '%{http_code}\n' 'http://127.0.0.1:8000/api/v1/photos?sort=nearest'
# antes: 500 500 500 500      después: 422 422 422 422
```

Además de la mala experiencia, cada búsqueda mal combinada ensuciaba las alarmas de
error interno. **Arreglado**: se captura la `ValidationError` y se emite un 422 en
`problem+json` con los punteros de campo.

### 3.4 — ALTO · El entorno de desarrollo no arrancaba (SELinux)

**Fichero:** `docker-compose.dev.yml:16,48`

`podman-compose up -d` dejaba **solo MinIO en pie**. Los bind mounts no llevaban la
etiqueta SELinux `z`, y en Fedora/RHEL con SELinux en `Enforcing` el contenedor no
puede leerlos:

```
$ podman logs astrophotos_db_1
psql: error: /docker-entrypoint-initdb.d/10-init.sql: Permission denied
$ podman logs astrophotos_sqs_1
java.io.FileNotFoundException: /opt/elasticmq.conf (Permission denied)
```

Consecuencia: la base se inicializaba **sin postgis, pgvector, citext ni pgcrypto**,
y no había cola. `make dev` no funciona en la máquina de destino.

**Arreglado** con `:ro,z`. Ahora los cuatro contenedores levantan y las extensiones
están (`postgis 3.4.3`, `vector 0.8.6`, `citext 1.6`, `pgcrypto 1.3`).

### 3.5 — ALTO · Las sondas de salud de infra apuntaban a rutas que dan 404

**Fichero:** `infra/stacks/api_stack.py:189` (contenedor) y `:268` (target group)

Infra leyó «`GET /healthz`» de `docs/api.md` literalmente; el backend monta **todo**
el router bajo `Settings.api_prefix`:

```
/healthz          -> 404
/readyz           -> 404
/api/v1/healthz   -> 200
/api/v1/readyz    -> 200
```

Los workflows de despliegue ya usaban la ruta buena
(`.github/workflows/deploy-prod.yml:236` → `https://astrophotos.app/api/v1/readyz`),
así que infra era el único de los tres en discrepancia. En staging esto significa
health check de contenedor fallando (bucle de reinicios) **y** target group nunca
sano: el despliegue blue/green no estabiliza jamás.

**Arreglado**, junto con el test de infra que había fijado la ruta mala
(`infra/tests/test_stacks.py:112` afirmaba `HealthCheckPath == "/readyz"`).

### 3.6 — ALTO · El backend arrancaba sin leer `.env`

**Fichero:** `backend/app/core/config.py:31` + `Makefile:44`

`SettingsConfigDict(env_file=".env")` es relativo al *cwd*, y `make dev` hace
`cd backend && .venv/bin/uvicorn ...`. `.env` vive en la raíz (lo dice
`.env.example`), así que nunca se leía:

```
$ cd backend && .venv/bin/python -c "from app.core.config import get_settings; \
    s=get_settings(); print(s.s3_endpoint_url, s.sqs_endpoint_url)"
None None
```

Con `S3_ENDPOINT_URL` y `SQS_ENDPOINT_URL` a `None`, boto3 apunta a **AWS real**:
`/readyz` daba 503 y la subida no podía funcionar. `DATABASE_URL` colaba solo porque
su valor por defecto coincide con el de desarrollo.

**Arreglado** anclando la ruta al repo. `/readyz` pasa a 200 con los tres checks en
verde.

### 3.7 — CRÍTICO (abierto) · No hay coherencia lectura-tras-escritura

**Fichero:** `backend/app/db/session.py:63-71`

El único `commit()` de la petición vive en el teardown de la dependencia
`get_session`, que corre **después** de que el handler devuelva. `POST
/photos/{id}/complete` responde 200 con la metadata nueva en el cuerpo mientras la
transacción sigue sin confirmar; una lectura inmediata ve la fila anterior.

**Medido: 12 lecturas obsoletas de 15.**

```python
pid = create_ready_photo(c, license='CC-BY-4.0', title='TITULO X')
c.get(f'/photos/{pid}').json()
# -> title: None, license: 'CC-BY-NC-4.0'    (la fila como se creó en el paso 1)
```

Mientras la base de datos ya dice lo correcto:

```
 c02d46bf-... | TITULO X | CC-BY-4.0
```

**Impacto directo:** el flujo de subida del frontend hace `complete` y navega a la
ficha. El usuario acaba de poner título y licencia, y la ficha le enseña «Sin
título» con la licencia por defecto. Sobre una foto que además ya es pública.

**No lo he arreglado**: cambiar dónde se confirma la transacción altera la semántica
de atomicidad de *todos* los endpoints de escritura, y eso es una decisión de diseño
del equipo, no un parche de QA. La recomendación es confirmar dentro de la operación
de escritura (o en el handler) antes de devolver la respuesta.

Queda fijado en `tests/integration/test_read_after_write.py` como `xfail(strict=False)`
con la reproducción y un control que demuestra que **no hay pérdida de datos**, solo
visibilidad tardía.

### 3.8 — MEDIO · La licencia y los créditos no viajaban con la imagen

**Ficheros:** `models/astrostack/pipelines/stages.py:743,816` y `io/writers.py:80`

Es la divergencia legal que había que buscar, y estaba:

- `write_attribution()` acepta `output_license`, pero **ningún** config ni el runner
  se lo pasaba nunca: `ATTRIBUTION.md` salía siempre **sin declarar la licencia de la
  obra derivada**.
- `write_result_fits()` no tiene noción de licencia ni de autoría. El FITS salía **sin
  licencia y sin créditos**, contra la regla 5 de `docs/licensing.md`, que dice
  explícitamente «se escriben los créditos […] en las cabeceras `HISTORY` del FITS».

El `.md` es un fichero suelto que se pierde en cuanto alguien reenvía solo la imagen;
la cabecera del FITS viaja siempre con ella.

**Arreglado como cableado, no como lógica nueva** (regla dura 5: `models/` no puede
*decidir* la licencia): el backend la calcula con `resolve_output_license()`, viaja en
el manifiesto (`Manifest.output_license`), y `models/` la escribe. Resultado real:

```
--- ATTRIBUTION.md ---
**Licence of this derivative work:** `CC-BY-SA-4.0` — the most restrictive
combination of the input licences.
| photo_id | author | licence | effective weight |
| `synthetic-000` | Autora 0 | CC-BY-4.0 | 0.339509 |
...
--- FITS ---
LICENSE = CC-BY-SA-4.0
HISTORY LICENCE: CC-BY-SA-4.0 (derivative of the frames below)
HISTORY CREDIT: Autora 0 (CC-BY-4.0) [synthetic-000]
HISTORY CREDIT: Autora 1 (CC-BY-SA-4.0) [synthetic-001]
```

Hay un test que compara la licencia del FITS y del `.md` **contra la que resuelve la
función de dominio del backend** para ese mismo conjunto de entradas.

### 3.9 — MEDIO · El CLI de `models/` descartaba en silencio los frames ND

**Ficheros:** `models/astrostack/cli.py:79` y `pipelines/runner.py:109`

`load_manifest` documenta y usa `strict_licenses=True`, pero **los dos únicos
llamantes lo forzaban a `False`**: el CLI (`--strict-licenses/--drop-unlicensed`,
`default=False`) y `run_pipeline`. Efecto: una foto ND llegaba al pipeline, se
descartaba sin ruido y el trabajo publicaba igual.

Contradice la regla 1 de `docs/licensing.md` («el job no se degrada: se devuelve
`blocked[]` y un 422»). El backend bloquea antes de encolar, así que un ND llegando
aquí significa que **esa puerta ha fallado** — y a una puerta de licencias que se ha
caído hay que responder con ruido, no con un `log.warning`.

**Arreglado**: ambos por defecto a `True`. El modo de descarte sigue disponible
explícitamente (`--drop-unlicensed`), y el test de `models/` que dependía del defecto
permisivo ahora lo pide explícitamente, con un test hermano que fija el nuevo defecto.

### 3.10 — MEDIO · El OpenAPI marca como privadas rutas que son públicas

**Fichero:** `backend/app/core/security.py:220` (`optional_user`)

Lo encontró schemathesis. `optional_user` comparte el `bearer_scheme` con
`current_user`, así que FastAPI anota las rutas 🔓 con `security: [{Bearer: []}]`:

```
GET /api/v1/licenses        security=[{'Bearer': []}]   <- 🔓 en docs/api.md
GET /api/v1/photos          security=[{'Bearer': []}]   <- 🔓
GET /api/v1/reconstructions security=[{'Bearer': []}]   <- 🔓
GET /api/v1/objects         <hereda global (ninguna)>
GET /api/v1/stats           <hereda global (ninguna)>
```

Funcionan sin token (comprobado), pero cualquier cliente generado del contrato se
cree lo contrario. **No lo he arreglado**: requiere un esquema de seguridad aparte
para el usuario opcional, que es un cambio en el cableado de autenticación. Queda
como `xfail(strict=True)` en `tests/contract/test_schemathesis.py`, así que se pondrá
verde solo el día que alguien lo corrija.

### 3.11 — BAJO · `make seed` apunta a un script que no existe

`Makefile:52` ejecuta `scripts/seed_dev.py`; en `scripts/` solo hay `init-db.sql`,
`elasticmq.conf`, `setup_repo.sh` y `db.Dockerfile`. Consecuencia práctica: sin
catálogo sembrado, dos tests de privacidad del mapa de cobertura se habrían saltado
en silencio. Los he hecho sembrar su propio objeto para que no se salten nunca.

### 3.12 — BAJO · `Page.total` contradice una decisión deliberada del contrato

`backend/app/schemas/common.py:73` añade `total: int | None`. `docs/api.md` dice
explícitamente: «**No hay total.** Es deliberado […] Si algún día hace falta un
total, será estimado y con ese nombre». Solo lo rellena `services/sky_object.py:154`.
O se quita, o se renombra a `estimated_total` y se documenta.

---

## 4. Fase 3 — el contrato de tipos (el trabajo más valioso de la tanda)

`frontend/app/types/api.gen.ts` era un placeholder de 548 líneas escrito a mano contra
un contrato imaginado. El real tiene 5 306. **39 de los 49 schemas que `domain.ts`
importaba no existían**: el frontend inventó nombres de dominio (`Photo`, `Me`,
`Quota`, `Problem`) mientras FastAPI emite los nombres de las clases Pydantic
(`PhotoOut`, `MeOut`, `QuotaOut`, `ProblemDetail`).

Regenerado con `pnpm run gen:api` y reconciliado el frontend. Las divergencias
estructurales reales, no solo de nombre, que hubo que resolver:

| el frontend esperaba | el backend publica |
|---|---|
| `photo.equipment.*` | `photo.optics.*` (y `EquipmentIn` para la entrada) |
| `photo.license` (string) | `photo.license` (objeto `LicenseOut` con consentimientos) |
| `photo.location_precision` | `photo.location.precision` |
| `photo.allow_ai_training`, `..._derivatives_in_stacks`, `license_locked_at` | dentro de `photo.license` |
| `photo.owner.display_name` | no existe: solo `owner_id` y `license.attribution_name` |
| `photo.object_name`, `photo.site_name`, `photo.location_label` | **no existen** |
| `quota.storage_quota_bytes`, `storage_used_bytes`, `queued_jobs` | `quota_bytes`, `used_bytes`, `jobs_queued_now` |
| `ProblemError {field, message}` | `ProblemError {pointer, detail, code}` |
| `coverage.cells[].period_start/lat_bin_deg/focal_bin_mm/photo_count` | `period`/`lat_bin`/`focal_bin`/`count` |
| `result.best_single_frame_url`, `fits_url`, `attribution_markdown_url` | `best_single_frame` (objeto), `attribution_md_url` |
| `reconstruction.uses_learned_model`, `object_name` | solo en el plan de preview; se deduce de `model_id` |
| `/reconstructions/{id}/inputs` → página | devuelve una **lista** |
| `UploadTicket` == presignado | `{photo_id, presigned_post, multipart}` |

De estas, las que valen la pena discutir con backend (no son bugs del frontend, son
huecos del contrato) están en §6.

Estado final: `pnpm lint`, `pnpm typecheck`, `pnpm test` (256) y `pnpm build`, todo
en verde contra los tipos **generados**.

Queda blindado con `tests/contract/test_openapi_types.py`, que regenera y compara byte
a byte, exige la cabecera del generador, prohíbe que `domain.ts` declare formas
propias y ejecuta `pnpm typecheck`. **Verificado por mutación**: al cambiar un campo
del fichero generado, fallan 2 de los 16 tests; al apuntar un alias a un schema
inexistente, falla el que toca.

---

## 5. Fase 4 — invariantes transversales

Todas verdes. Lo que cubren, y por qué no bastaba con lo que ya había:

**Licencias de punta a punta** (22 tests). Recorre las 6 licencias apilables en
parejas comparando `POST /licenses/resolve` con la función de dominio, y luego
comprueba que la licencia que resuelve el backend es **exactamente** la que aparece
en `ATTRIBUTION.md` y en la cabecera del FITS. Incluye un test que prohíbe que
`models/` mencione `restrictiveness`, `allows_commercial` o `requires_sharealike`,
para que nadie reimplemente la tabla ahí.

**Privacidad de la ubicación** (19 tests, 7 caminos). Con el Observatorio del Teide
como marcador reconocible, se comprueba que una foto `hidden` no filtra coordenadas
por: detalle público, detalle para un tercero autenticado, búsqueda, búsqueda
geoespacial `?near=`, similares, cabeceras de la descarga, `cells[]` y `sites[]` del
mapa de cobertura, procedencia de reconstrucciones, `/stats` y perfil público.
Incluye un **control negativo** (con `exact` las coordenadas sí salen), sin el cual
todo lo demás podría estar pasando por vacío, y un test de que el JPEG de partida
llevaba GPS de verdad. También verifica que agrupar por precisión ocurre **antes** de
agregar: un objeto con solo fotos ocultas devuelve `sites: []`.

**Reproducibilidad** (6 tests). `models/` ya probaba dos ejecuciones seguidas y el
manifiesto invertido; aquí se **baraja** con tres semillas, se comparan los **bytes
del FITS** (no solo los arrays en memoria) y se comprueba que el bloque determinista
no arrastra rutas absolutas ni marcas de tiempo. Con control de sensibilidad: quitar
un frame **tiene** que cambiar el checksum.

**Divulgación y bloqueo** (11 tests). Un pipeline aprendido sin mapa de incertidumbre
no se publica por ningún camino; un `model_id` sobre un pipeline clásico también
cuenta como aprendido; un frame ND bloquea el job entero con 422 en `problem+json`
diciendo qué foto y por qué; `blocked` y `rejected` nunca se solapan; un frame sin
resolver no bloquea nunca.

---

## 6. Fase 5 — revisión crítica

### Copy contra la física · limpio

Revisadas las 496 claves de `es.json` y `en.json` (las dos tienen exactamente las
mismas claves, sin huérfanas) y el README entero contra la sección 5 del informe de
investigación. **No hay ninguna promesa que la física no dé.** Al contrario: el copy
niega explícitamente la síntesis de apertura, explica por qué (intensidad vs campo,
fase destruida en la detección), y `home.limits.aperture` lista «ganancia de
resolución al estilo del EHT» como algo que el producto **no** hace. La descripción
de `drizzle-v1` dice «recupera muestreo, no resolución óptica», que es exactamente la
distinción entre los puntos (2) y (3) de la sección 0 del informe. Nada que corregir.

### Lógica de licencias duplicada · dos sitios, ahora vigilados

1. **`frontend/app/lib/licensing.ts:159` `resolveOutputLicenseHint()`** — réplica del
   algoritmo de combinación. Está documentada como pista optimista y el constructor
   sigue obligado a llamar a `preview`, pero es una segunda implementación de una
   regla con consecuencias legales. **Hoy coincide con el backend en las 164
   combinaciones de 1, 2 y 3 entradas sobre las 8 licencias**; lo he verificado
   generando la tabla desde `resolve_output_license()` y ejecutándola contra la
   función del frontend (`frontend/tests/unit/licenseParity.spec.ts`, 165 tests).
   Ya no puede divergir sin que falle CI.

2. **`models/astrostack/io/manifest.py:45` `NO_DERIVATIVES_CODES`** — lista de códigos
   a mano, más una heurística `"-ND-" in code.upper()`. Es conservadora (sobre-bloquea)
   y está declarada como red de seguridad para ejecuciones offline, así que la dejo,
   pero es un segundo sitio donde vive un pedazo de la tabla de licencias.

### No determinismo · limpio

```
random. / np.random global en models/ y backend/app/workers/  -> 0 resultados
datetime.now() dentro de un pipeline                          -> solo en el bloque
                                                                 volátil de provenance
iteración sobre set / os.listdir / glob sin sorted            -> 0 resultados
```

Los dos `set(...)` que aparecen son comprobaciones de pertenencia (`x in set(...)`),
no iteraciones: no afectan al orden. Verificado además empíricamente por los tests de
reproducibilidad, que es la prueba que cuenta.

### Tests que no prueban nada

La calidad de las suites existentes es alta: cero mocks que se prueben a sí mismos,
cero asserts sobre `call_count` en lugar de sobre el resultado. Lo que sí hay:

- **`backend/tests/test_api_smoke.py:143` `test_every_contract_route_exists`** — no es
  vacío, pero es el que creó una falsa sensación de cobertura. Comprueba que las
  rutas *aparecen en el OpenAPI*, no que funcionen. `GET /photos/{id}` y
  `GET /photos/{id}/download` figuran en su tabla `CONTRACT_ROUTES` y devolvían 500
  las dos. **Pasaría con el cuerpo de todos los handlers reemplazado por `raise`.**
- **`backend/tests/unit/test_location_privacy.py:175`
  `test_precision_accepts_the_string_form_used_by_the_api`** — sin ningún assert.
  Pasaría con el cuerpo de `obfuscate_location` vacío.
- **`backend/tests/unit/test_multipart_upload.py:222` `test_storage_abort_never_raises`**
  — sin assert, pero «no debe lanzar» es un patrón legítimo y el docstring lo explica.
  Lo dejo.

**La causa raíz de que 681 tests convivieran con tres 500s** es estructural: los tests
unitarios del backend usan dobles en memoria y nunca ejercitan la semántica real del
mapa de identidad de SQLAlchemy ni la serialización de cabeceras HTTP. Ninguna de las
cuatro suites hacía una petición contra Postgres de verdad a los caminos de lectura.
Eso es lo que cubre ahora `tests/integration/test_read_paths.py`.

### Huecos del contrato (no son bugs, son decisiones pendientes)

Aparecieron al reconciliar los tipos. El frontend los daba por hechos:

- **`PhotoOut` no trae el nombre del autor ni el del objeto** (`owner`, `object_name`),
  así que la ficha no puede pintar «M31 — por Ana» sin una segunda llamada. He hecho
  que degrade a `license.attribution_name` y a un enlace por `object_id`.
- **`docs/api.md` promete `location_label`** (la columna de la tabla de ofuscación),
  y `LocationOut` no lo tiene. O se implementa o se quita del documento.
- **`PhotoSummaryOut` no trae `allow_derivatives_in_stacks`**, así que la galería no
  puede saber si una foto con licencia permisiva tiene el consentimiento retirado.
  El frontend ahora deduce el caso ND/ARR de la licencia y deja el resto para
  `preview`, que es obligatorio; funciona, pero el usuario descubre el bloqueo más
  tarde de lo necesario.
- **`PhotoSummaryOut.license` es un código y `PhotoOut.license` es un objeto.**
  Incoherencia menor, pero obliga a ramificar en el cliente.

### CI no ejecuta la suite transversal

`.github/workflows/ci.yml:184` corre `python -m pytest tests -q` dentro del job de
`infra` (con `working-directory: infra`), así que **el `tests/` de la raíz no lo
ejecuta nadie**. He añadido `make test-cross` y `make test-all`, pero hace falta un
job de CI que levante el stack (postgis + minio + elasticmq + backend) y lo corra;
no lo he escrito porque no puedo verificar un workflow de GitHub Actions desde aquí y
prefiero no dejar YAML sin probar.

---

## 7. Qué he tocado

**Arreglos** (9 ficheros de producto):

| fichero | cambio |
|---|---|
| `docker-compose.dev.yml` | `:ro,z` en los dos bind mounts (SELinux) |
| `backend/app/core/config.py` | `.env` anclado a la raíz del repo |
| `backend/app/repositories/photo.py` | `synchronize_session=False` en los dos contadores |
| `backend/app/repositories/user.py` | ídem en `reserve_quota` |
| `backend/app/api/v1/photos.py` | `_header_safe()` para `X-Attribution` |
| `backend/app/api/v1/search.py` | `ValidationError` → 422 `problem+json` |
| `infra/stacks/api_stack.py` | `API_PREFIX` en las dos sondas |
| `models/astrostack/io/manifest.py` | `Manifest.output_license` desde el manifiesto |
| `models/astrostack/pipelines/stages.py` | licencia y créditos al FITS y al `.md` |
| `models/astrostack/pipelines/runner.py`, `cli.py` | `strict_licenses` por defecto `True` |

**Frontend**: `api.gen.ts` regenerado, `domain.ts` reescrito como alias a los schemas
reales, y 14 ficheros de componentes/páginas/stores ajustados al contrato real.

**Tests ajustados** (no borrados): `infra/tests/test_stacks.py` (fijaba la ruta de
salud mala), `models/tests/test_pipelines.py` (dependía del defecto permisivo; ahora
lo pide explícito y hay un test hermano para el nuevo defecto),
`frontend/tests/unit/{apiClient,upload}.spec.ts` (fijaban el contrato imaginado).

**Tests nuevos**: `tests/` — 2 784 líneas, 121 tests (112 pasan, 9 `xfail`
documentados). Más `frontend/tests/unit/licenseParity.spec.ts` (165) y su fixture
generado.

---

## 8. Estado final

```
backend   ruff OK · format OK · mypy OK (71 ficheros) · 702 passed
models    ruff OK · 186 passed, 5 skipped
infra     ruff OK · 107 passed
frontend  lint OK · typecheck OK · 256 passed · build OK
tests/    112 passed, 9 xfailed  (contrato + integración + invariantes)
```

Los 9 `xfail` son los defectos abiertos, deliberadamente visibles: la coherencia
lectura-tras-escritura (§3.7, cinco casos), la anotación de seguridad del OpenAPI
(§3.10, tres rutas), y el EXIF de la preview, que no se puede comprobar hasta que el
worker de ingesta forme parte del stack local.

Entre ejecuciones, algunos de los cinco casos de §3.7 salen como `xpass` en vez de
`xfail` (por eso van con `strict=False`): es exactamente la intermitencia del fallo
—12 de 15— y verla oscilar en el informe de pytest es información, no ruido. El
resto de la suite es estable: **verificada en 5 pasadas consecutivas sin variación**.

Durante esa verificación apareció **un test inestable propio**
(`test_la_busqueda_no_filtra`): daba por hecho que la foto de prueba entraría en los
primeros 200 resultados de la búsqueda global, lo que deja de ser cierto según crece
la base. Arreglado acotando la búsqueda por `owner`. Un test flaky es peor que
ninguno, así que se caza y se arregla, no se reintenta.

## 9. Recomendación

**No desplegar a staging todavía.** Falta una cosa, y es de las que el usuario nota
en el primer minuto:

1. **Arreglar §3.7 (lectura-tras-escritura).** Es lo único bloqueante que queda. Un
   usuario que sube una foto ve, el 80% de las veces, la ficha con la metadata
   equivocada. Requiere decidir dónde se confirma la transacción.

Después de eso, y por orden de valor:

2. Arreglar §3.10 (anotación de seguridad del OpenAPI) antes de que alguien genere un
   cliente a partir del contrato.
3. Añadir el job de CI que ejecute `tests/` con el stack levantado; si no, los
   arreglos de esta tanda se pueden deshacer sin que nadie se entere.
4. Cerrar los huecos de contrato de §6 (`location_label`, autoría y objeto en
   `PhotoOut`, `allow_derivatives_in_stacks` en el resumen) o quitarlos de
   `docs/api.md`.
5. Escribir `scripts/seed_dev.py` o quitar el target `make seed`.

Una nota que no es un fallo pero conviene tener presente en el primer despliegue con
migración en caliente: durante los tests aparecieron
`asyncpg.InvalidCachedStatementError` transitorios («cached statement plan is invalid
due to a database schema or configuration change») tras cambios de esquema. Se
autorrecuperan tras un fallo, pero con blue/green y migraciones contra una API viva,
cada despliegue puede producir una ráfaga de 500s. Si molesta, la mitigación conocida
es `statement_cache_size=0` en el motor asyncpg, a cambio de algo de rendimiento.
