# Roadmap

Los hitos siguen los tres tiers del informe de investigación
(`docs/research/multi-image-astro-reconstruction.md`, sección 9). El principio que los
ordena: **primero lo que es cierto y verificable, después lo que es aprendido, y solo
al final lo que es investigación**.

---

## M0 — Fundación *(hecho)*

Contratos (`docs/data-model.md`, `docs/api.md`), informe de investigación, decisiones
en `docs/adr/`, agentes, entorno local con podman-compose, y el andamiaje de los
cuatro componentes.

## M1 — Vertical slice: subir y ver

**Objetivo de usuario:** me registro, subo una foto con su licencia y su metadata, y
la veo en la galería con su ficha completa.

- Cognito + registro/login, perfil y cuota.
- Subida presignada con lectura de EXIF en el navegador y multipart para ficheros grandes.
- Worker de ingesta: EXIF/XMP, previews, thumbs, checksum, deduplicación.
- Galería, ficha de foto, visor `<AstroViewer>` con estiramiento asinh.
- Selector de licencia con `CC-BY-NC-4.0` por defecto y los dos consentimientos.
- Precisión de ubicación funcionando de punta a punta, con test por endpoint.
- Staging desplegado desde `develop`.

**Criterio de aceptación:** un usuario ajeno al equipo sube un RAW de 60 MB desde el
móvil y la foto aparece publicada con su metadata correcta y su licencia visible.

## M2 — Astrometría y calidad

**Objetivo:** el sistema sabe *dónde en el cielo* apunta cada foto y *cómo de buena* es.

- Plate solving en el worker (astrometry.net local, ASTAP como alternativa), con la
  focal del EXIF como prior. WCS con distorsión guardado en `wcs_json`.
- Medida de PSF/FWHM, excentricidad y fondo por campo; punto cero fotométrico contra
  fotometría sintética de Gaia DR3; airmass, fase y separación lunar, Bortle estimado.
- `quality_score` alimentando la ordenación y la selección de frames.
- Catálogo `sky_objects` sembrado desde OpenNGC; asociación automática foto → objeto.
- Búsqueda por cono celeste, por cercanía geográfica y por licencia compatible.
- Mapa de cobertura por objeto: dónde y cuándo faltan aportaciones.

**Criterio de aceptación:** subo una foto sin decir qué es y el sistema me dice que es
M31, con qué escala de píxel y con qué FWHM, y me la coloca en el mapa de cobertura.

## M3 — Tier A: reconstrucción clásica

**Objetivo:** cualquiera puede pedir un apilado profundo de un objeto y obtener algo
mejor que su mejor toma individual, con procedencia y licencia correctas.

- `astrostack` completo: calibración, alineación por WCS, drizzle, **coadición óptima
  de Zackay-Ofek**, rechazo robusto, deconvolución acotada.
- `POST /reconstructions/preview` con licencia resuelta y fotos bloqueadas explicadas.
- Ejecución en AWS Batch spot, progreso por SSE, resultado en FITS 32-bit + preview.
- `provenance.json` y `ATTRIBUTION.md` en cada resultado.
- Comparación lado a lado contra el mejor frame individual, con métricas medidas.
- Suite de verificación física: SNR ∝ √N, conservación de flujo, reproducibilidad
  bit a bit, recuperación lineal de fuentes inyectadas, campo vacío sin alucinaciones.

**Criterio de aceptación:** 50 aportaciones de M31 de 12 personas distintas producen
un apilado medible varias magnitudes más profundo que la mejor toma individual, y el
resultado se reproduce bit a bit al reejecutarlo.

## M4 — Comunidad y confianza

- Colecciones, seguir a otros usuarios, comentarios moderados.
- Reputación por calidad de aportaciones, no por popularidad.
- Página por objeto con su historia de reconstrucciones.
- Panel de curación: cuarentena, reportes, moderación.
- Portabilidad: exportar todas mis fotos y su metadata; borrar mi cuenta con las
  consecuencias explicadas (las reconstrucciones publicadas no se reescriben, ver ADR 0006).

## M5 — Tier B: super-resolución aprendida

- Construcción automática de pares LR/HR: coadds profundos Tier A como pseudo-verdad,
  más pares externos contra HST/HSC/DESI Legacy.
- `dataset_snapshots` respetando `allow_ai_training`, con purga al revocar.
- Modelo estilo RBSR/Burstormer **sin módulo de alineación**, con warping por WCS y
  condicionamiento físico (PSF, sigma, punto cero, airmass) — ver ADR 0008.
- Pérdidas científicas: consistencia de flujo, restricción de forma, fidelidad a
  través del modelo directo.
- Validación adversarial obligatoria antes de activar un modelo: inyección de fuentes,
  contraste contra Gaia/PanSTARRS, campos retenidos con verdad HST.
- En la interfaz: capa opcional y **etiquetada**, nunca sustituto silencioso del
  apilado clásico, siempre con mapa de incertidumbre.

**Criterio de aceptación:** el modelo mejora la métrica de consistencia de flujo y la
FWHM efectiva sobre el Tier A en campos retenidos, sin introducir una sola fuente sin
contrapartida en catálogo.

## M6 — Tier C: modelo bayesiano del cielo *(investigación)*

- Representación continua del cielo (campo implícito o base multi-resolución sobre
  plano tangente, con teselado HEALPix).
- Modelo directo explícito por fotografía: WCS + distorsión, PSF espacialmente
  variable (incluida rotación de campo y refracción cromática diferencial), respuesta
  espectral, transparencia, fondo, no linealidad y saturación.
- Deconvolución ciega multi-frame estilo *Thresher* a escala de repositorio, con la
  estructura de un `Imager` RML de eht-imaging pero en el espacio de imagen.
- Auto-calibración conjunta de los parámetros de estorbo con el modelo del cielo.
- Prior score-based con muestreo posterior, **publicando cuánta estructura vino del
  prior y no de los datos**.
- Fusión multi-escala (análogo incoherente del *feathering* de radio).
- Extensión a 4D: curvas de luz de variables, movimientos propios, asteroides,
  transitorios por diferencia ZOGY contra el coadd de referencia. Aquí es donde un
  repositorio de fotos puede producir ciencia publicable.

---

## Lo que no está en el roadmap, y por qué

- **Síntesis de apertura / resolución tipo EHT.** Físicamente imposible con cámaras
  incoherentes. Ver ADR 0007.
- **Interferometría de intensidad.** Funciona con telescopios de aficionado, pero es
  un programa de hardware (detectores de fotón único, marcado temporal por GPS,
  observación simultánea) y no puede aplicarse a imágenes ya almacenadas.
- **Realce generativo sin etiquetar.** En astronomía una fuente inventada es un falso
  descubrimiento. Toda salida aprendida va etiquetada y con incertidumbre.
