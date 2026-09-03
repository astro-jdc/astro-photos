# Licencias

## Catálogo ofrecido al subir

| código | nombre | comercial | derivadas | share-alike | restrictividad |
|---|---|---|---|---|---|
| `CC0-1.0` | Dominio público | ✅ | ✅ | ❌ | 0 |
| `CC-BY-4.0` | Atribución | ✅ | ✅ | ❌ | 1 |
| `CC-BY-SA-4.0` | Atribución + CompartirIgual | ✅ | ✅ | ✅ | 2 |
| **`CC-BY-NC-4.0`** | **Atribución + NoComercial** | ❌ | ✅ | ❌ | **3 — por defecto** |
| `CC-BY-NC-SA-4.0` | Atribución + NoComercial + CompartirIgual | ❌ | ✅ | ✅ | 4 |
| `CC-BY-ND-4.0` | Atribución + SinDerivadas | ✅ | ❌ | ❌ | 5 |
| `CC-BY-NC-ND-4.0` | Atribución + NoComercial + SinDerivadas | ❌ | ❌ | ❌ | 6 |
| `ARR` | Todos los derechos reservados | ❌ | ❌ | ❌ | 7 |

`CC-BY-NC-4.0` viene preseleccionada, tal como se pidió. El usuario puede cambiar su
`default_license` en el perfil y el formulario de subida la respeta.

## Consentimientos separados de la licencia

Dos casillas independientes, porque las licencias CC **no** hablan de entrenamiento de
modelos y no queremos ampararnos en ambigüedad:

- `allow_ai_training` (por defecto ✅) — la foto puede entrar en `dataset_snapshots`
  para entrenar modelos de reconstrucción. Retirarlo purga la foto de los snapshots
  futuros; los modelos ya entrenados se marcan y se reentrenan en el siguiente ciclo.
- `allow_derivatives_in_stacks` (por defecto ✅) — la foto puede usarse como frame de
  entrada en reconstrucciones de otros usuarios.

Un `ND` implica `allow_derivatives_in_stacks=false` de forma forzosa; la UI lo explica
en vez de dejar un estado incoherente.

## Combinación en una reconstrucción

Implementado en `backend/app/domain/licensing.py`, función `resolve_output_license()`:

```python
def resolve_output_license(inputs: list[PhotoLicenseFacts]) -> LicenseResolution:
    """Devuelve la licencia de salida o la lista de fotos que bloquean el job."""
```

Reglas, en orden:

1. **Bloqueo duro** — cualquier entrada con `allows_derivatives=False` (ND, ARR) o
   `allow_derivatives_in_stacks=False` se rechaza. El job no se degrada: se devuelve
   `blocked=[{photo_id, reason}]` y un 422 con el detalle, para que el usuario pueda
   quitarla y reintentar.
2. **NoComercial es contagioso** — si alguna entrada es NC, la salida es NC.
3. **ShareAlike es contagioso** — si alguna entrada es SA, la salida es SA.
4. La salida es la licencia CC de menor permisividad que satisfaga 2 y 3.
   Si no hay ninguna entrada NC ni SA, la salida hereda la más restrictiva presente
   (CC0 solo sale si *todas* las entradas son CC0).
5. **Atribución siempre.** Aunque todo fuera CC0, se emite `ATTRIBUTION.md` con
   autor, título, licencia y URL de cada frame, y se escriben los créditos en XMP
   (`dc:creator`, `xmpRights:UsageTerms`, `Iptc4xmpExt:ArtworkOrObject`) del TIFF y
   en las cabeceras `HISTORY` del FITS.

`POST /licenses/resolve` expone la misma función para que el frontend avise antes de
que el usuario pierda tiempo.

## Congelado de licencia

`photos.license` puede cambiarse libremente **mientras nadie haya descargado la foto
ni la haya usado en una reconstrucción publicada**. A partir de ahí `license_locked_at`
se fija y la licencia solo puede **relajarse** (bajar de restrictividad). Es lo mismo
que dicen las propias CC: son irrevocables para quien ya recibió la obra. La UI lo
explica antes del primer cambio, no después.

`reconstruction_inputs.snapshot_license` guarda la licencia vigente en el momento del
uso, de modo que un cambio posterior nunca reescribe la historia de una reconstrucción
ya publicada.

## Atribución de las reconstrucciones

Cada resultado publica, junto a la imagen:

- `ATTRIBUTION.md` — tabla de los N autores, ordenada por peso de contribución.
- `provenance.json` — JSON firmado: ids de fotos, checksums, pesos, versión del
  pipeline, git sha, id del modelo, parámetros. Reproducible bit a bit.
- Una tarjeta de créditos renderizada para compartir en redes.

---

## Datos de terceros: qué podemos usar para entrenar, y qué no

Esta sección existe para que la pregunta "¿y si entrenamos con las fotos de X?" no se
replantee cada seis meses sin ver el análisis.

### El principio

Ya decidimos, más arriba en este mismo documento, que **las licencias Creative Commons
no dicen nada sobre entrenamiento de modelos** y que ampararse en esa ambigüedad sería
aprovecharse de los usuarios. Por eso pedimos `allow_ai_training` explícito y separado
de la licencia.

Ese principio **se aplica igual a los usuarios de otras plataformas**. Si solo vale
cuando nos conviene, no es un principio. El silencio de unos términos de servicio sobre
IA no es permiso.

### AstroBin

Es el repositorio de astrofotografía de referencia (desde 2011, con plate solving
automático, base de datos de equipamiento, licencias CC por imagen y un repositorio
compartido de FITS/RAW). Tiene API. **No podemos usar sus fotos para entrenar.** Tres
razones independientes, cualquiera de ellas suficiente
([Términos de Servicio](https://welcome.astrobin.com/terms-of-service)):

1. **El scraping está prohibido.** *User Conduct*: nada de acceso "via any robot,
   spider, scraper or other automated means without our express written permission,
   with the exception of established Search Engines".
2. **La API es para mostrar, no para obtener datos.** Máximo 30 imágenes de AstroBin
   por página; prohibido "replicate or attempt to replace the essential user
   experience"; y uso comercial permitido solo si "the primary purpose of your
   application has nothing to do with the display of astronomical images from third
   party services" — evaluado al conceder la clave. Nuestro propósito principal *son*
   imágenes astronómicas, así que ese permiso no nos corresponde.
3. **AstroBin no puede licenciarnos nada aunque quisiera.** "By uploading your
   photographic works to the Site, you retain full rights that you had prior to
   uploading." El copyright es del fotógrafo; AstroBin solo se reserva uso promocional
   y redistribución no comercial. Un acuerdo con la plataforma no nos daría derechos
   sobre una sola foto.

Matiz técnico, para no exagerar en la otra dirección: una foto concreta publicada allí
bajo CC-BY o CC0 sí lleva esa licencia consigo, y usarla conforme a sus términos sería
legal. Pero sigue sin poder obtenerse por scraping, y sigue sin haber consentimiento
explícito de entrenamiento.

### Lo que sí es legítimo

- **Importación por el propio autor.** Un flujo donde el usuario se autentica él mismo,
  trae **sus** imágenes y marca nuestras casillas de consentimiento. Es el titular de
  derechos ejerciendo sus derechos, no nosotros recolectando.
- **Acuerdo explícito con opt-in**, si alguna plataforma quiere colaborar.
- **Datasets publicados para investigación**: AstroSR (pares SDSS↔HSC), STAR (54.738
  pares del HST), DESI–HST de FluxFlow, BurstSR, PROBA-V. Ver
  `docs/research/multi-image-astro-reconstruction.md` §7 y respetar la licencia de cada uno.
- **Archivos profesionales públicos**: MAST (HST/JWST), ESO, DESI Legacy Survey.
- **Nuestro propio corpus**, que es la vía principal: un apilado profundo Tier A es
  pseudo-verdad legítima para subconjuntos pequeños extraídos de él mismo. Escala sola
  conforme crece el repositorio y solo usa fotos con `allow_ai_training = true`.
  Implementado en `models/training/dataset.py`.

### La regla operativa

Ningún dato entra en un `dataset_snapshot` sin una de estas tres cosas: consentimiento
explícito del autor en nuestra plataforma, una licencia que cubra expresamente el uso, o
un acuerdo firmado. **La duda se resuelve dejando el dato fuera**, no dentro.
