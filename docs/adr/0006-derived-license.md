# 0006 — La licencia de salida es la combinación más restrictiva de las entradas

Estado: Aceptado · 2026-09-03

## Contexto

Una reconstrucción es una obra derivada de decenas o centenares de fotografías con
licencias distintas. Publicarla con una licencia incompatible con alguna de sus
entradas es una infracción de derechos de autor, no un bug menor.

Las licencias Creative Commons, además, **no dicen nada sobre entrenamiento de
modelos**. Ampararse en esa ambigüedad sería aprovecharse de los usuarios.

## Decisión

Implementada en `backend/app/domain/licensing.py`, función única
`resolve_output_license()`, detallada en `docs/licensing.md`:

1. Una entrada `ND` o `ARR`, o con `allow_derivatives_in_stacks=false`, **bloquea el
   trabajo**. No se degrada la salida: se devuelve 422 con la lista de fotos que
   bloquean y su motivo, para que el usuario las quite y reintente.
2. `NC` es contagioso. `SA` es contagioso.
3. La salida es la licencia menos permisiva que satisfaga lo anterior. `CC0` solo sale
   si **todas** las entradas son `CC0`.
4. La atribución se emite **siempre**, incluso con entradas CC0.
5. `reconstruction_inputs.snapshot_license` congela la licencia vigente en el momento
   del uso, para que un cambio posterior no reescriba la historia.

Y dos consentimientos **separados de la licencia**: `allow_ai_training` y
`allow_derivatives_in_stacks`, ambos activados por defecto y revocables.

## Consecuencias

- Bloquear en vez de degradar puede frustrar al usuario, pero es lo correcto: una
  obra ND no puede formar parte de una derivada bajo ninguna licencia.
- `POST /licenses/resolve` expone la misma función para que el frontend avise antes.
- El test de tabla de esta función es el test más importante del repositorio: un fallo
  aquí es un problema legal.
- El uso masivo de `CC-BY-NC-4.0` por defecto hará que la mayoría de reconstrucciones
  salgan NC. Es el resultado esperado y correcto.

## Alternativas descartadas

- **Permitir solo un conjunto compatible de licencias al subir.** Reduciría mucho las
  aportaciones y no respeta la elección del autor.
- **Degradar la salida cuando entra una ND.** Legalmente inválido.
