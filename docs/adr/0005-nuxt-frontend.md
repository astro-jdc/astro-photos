# 0005 — Nuxt 4 con renderizado híbrido frente a SPA

Estado: Aceptado · 2026-09-03

## Contexto

El valor del repositorio depende de que la gente **encuentre** las fotos y los
objetos. Cada ficha de foto y cada ficha de objeto es una página pública que debería
posicionar en buscadores y verse bien al compartirla. Una SPA pura entrega un HTML
vacío al rastreador y una tarjeta Open Graph genérica.

A la vez, el constructor de reconstrucciones y el panel de usuario son aplicación
interactiva pura, donde el SSR no aporta nada.

## Decisión

**Nuxt 4** con renderizado por ruta:

- prerenderizado / ISR: `/`, `/explore`, `/objects/**`, `/photos/**`, `/reconstructions/**`
- SPA cliente: `/me/**`, `/build/**`

Se despliega como sitio estático en S3 detrás de CloudFront; la revalidación se hace
por invalidación desde el pipeline y por webhook cuando una reconstrucción termina.

## Consecuencias

- Cada foto y cada objeto son una landing indexable con su Open Graph y su preview.
- Primer pintado rápido en galerías grandes, que es donde una SPA sufre.
- Sigue siendo Vue 3 y TypeScript: no se pierde nada del stack pedido.
- Hay que tener cuidado con el código que solo puede correr en cliente (WebGL,
  MapLibre): va en componentes `<ClientOnly>` con su fallback.

## Alternativas descartadas

- **Vue 3 + Vite SPA.** Más simple y más barata, pero regala el SEO, que aquí es
  producto y no detalle técnico.
- **Next.js / React.** Mejor ecosistema de visores, pero el usuario pidió Vue y la
  diferencia real no lo justifica.
