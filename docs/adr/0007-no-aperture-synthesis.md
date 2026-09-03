# 0007 — No prometemos síntesis de apertura

Estado: Aceptado · 2026-09-03

## Contexto

La idea fundacional del proyecto se inspira en el Event Horizon Telescope: combinar
observaciones de muchos lugares para obtener lo que un solo instrumento no puede.
La analogía es motivadora y **parcialmente falsa**, y conviene fijar por escrito dónde
se rompe antes de que se cuele en la interfaz o en el material de promoción.

El razonamiento completo, con referencias, está en
`docs/research/multi-image-astro-reconstruction.md`, sección 5.

## Decisión

El producto **no afirma en ningún momento** obtener resolución angular por combinación
de observadores separados.

El VLBI funciona porque cada estación registra el campo eléctrico complejo —amplitud y
fase— contra un reloj atómico, y luego se correlacionan. A 1,3 mm un periodo son ~4
picosegundos, alcanzable con máseres de hidrógeno. En el óptico a 550 nm un periodo
son **1,8 femtosegundos**, y una cámara de consumo registra intensidad (|E|²),
destruyendo la fase en el instante de la detección, integrada sobre 10¹²–10¹⁷ periodos,
sin referencia temporal mejor que el segundo. Dos personas a 1000 km no forman un
telescopio de 1000 km: sus imágenes se suman **incoherentemente**.

Lo que sí se afirma, porque es cierto y verificable:

- **Profundidad**: SNR ∝ √(Σ tᵢ·transparenciaᵢ), con pesos de Zackay-Ofek.
- **Muestreo**: recuperación del detalle que el píxel de cada cámara aliasaba, gracias
  a la diversidad sub-píxel que aportan observadores independientes. Aquí sí hay
  margen real: un 50 mm sobre píxeles de 4 µm da ~16 arcsec/píxel frente a un límite
  óptico de ~2 arcsec.
- **Rango dinámico**, fusión multi-escala y **dominio temporal** (variables,
  asteroides, transitorios) — esto último es lo científicamente más valioso.

## Consecuencias

- Requisito de producto: `frontend/` no puede contener copy que sugiera lo contrario;
  QA lo vigila. La landing lo explica explícitamente.
- Credibilidad ante la comunidad de astrofotografía, que es técnicamente exigente y
  alérgica a la exageración.
- Renunciamos a un titular llamativo. A cambio, todo lo que decimos se sostiene.

## Alternativas descartadas

- **Usar la analogía del EHT en marketing sin matizarla.** Falso y, en este público,
  contraproducente.
- **Interferometría de intensidad (Hanbury Brown-Twiss).** Sí funciona con telescopios
  de aficionado (demostrado sobre Sirio con 0,25 m), pero exige detectores de fotón
  único, marcado temporal disciplinado por GPS y observación simultánea. Es un programa
  de hardware, no una función de un repositorio de fotos, y no puede aplicarse
  retroactivamente a imágenes ya almacenadas.
