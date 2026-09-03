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
