# 0008 — Alineación guiada por WCS en lugar de flujo óptico aprendido

Estado: Aceptado · 2026-09-03

## Contexto

Toda la literatura de super-resolución multi-frame (DBSR, BIPNet, Burstormer, BSRT,
RBSR) dedica su módulo más pesado a **alinear** los frames, normalmente con flujo
óptico aprendido o convoluciones deformables. Esos modelos asumen una ráfaga: misma
cámara, misma focal, milisegundos de diferencia, desplazamientos de pocos píxeles.

Nuestro caso viola todas esas suposiciones —cámaras distintas, focales distintas,
años de diferencia, rotación de campo— pero las viola **a nuestro favor**: en
astronomía existe el *plate solving*. Resolver astrométricamente una imagen da un WCS
con distorsión que sitúa cada píxel en el cielo con precisión de una fracción de
píxel, de forma analítica y verificable.

## Decisión

La alineación es **geométrica y guiada por WCS**, no aprendida:

1. Plate solving de cada imagen (astrometry.net local o ASTAP), con la focal del EXIF
   y el tamaño de sensor como prior de escala de píxel.
2. Reproyección al grid común con `reproject_adaptive`/`reproject_exact` o drizzle.
3. `astroalign` (emparejamiento de triángulos de estrellas) como **fallback** cuando
   el plate solving falla, y como comprobación cruzada cuando ambos funcionan.

En Tier B, las arquitecturas de burst SR se adoptan **quitándoles el módulo de
alineación** y sustituyéndolo por warping por WCS; la red solo hace fusión,
deconvolución y denoising, condicionada además por la PSF, el mapa de sigma, el punto
cero y el airmass de cada frame.

## Consecuencias

- Más preciso que cualquier flujo óptico aprendido, y **auditable**: el residuo de
  alineación es un número que se guarda en `reconstruction_inputs.alignment_rms_px`.
- La red es más pequeña y entrena con muchos menos datos, porque no tiene que aprender
  geometría que ya conocemos.
- Generaliza a combinaciones de focales y orientaciones que ninguna ráfaga contiene.
- Dependemos de que el plate solving funcione: hay que empaquetar los índices de
  astrometry.net en la imagen de Batch y aceptar que las tomas sin estrellas
  suficientes (planetarias, muy cerradas) no entran por esta vía.

## Alternativas descartadas

- **Flujo óptico aprendido, como en la literatura de ráfagas.** Peor y menos
  auditable cuando la geometría ya es conocida.
- **Solo astroalign.** No da coordenadas absolutas, así que no permite combinar tomas
  de campos parcialmente solapados ni asociarlas a un objeto del catálogo.
