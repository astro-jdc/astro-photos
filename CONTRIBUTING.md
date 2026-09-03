# Contribuir a astro-photos

## Antes de escribir código

Lee `CLAUDE.md` (las reglas duras) y el contrato que vayas a tocar
(`docs/data-model.md`, `docs/api.md`). Si tu cambio afecta a lo que el producto
promete, lee además `docs/research/multi-image-astro-reconstruction.md`.

## Flujo

```bash
git checkout develop && git pull
git checkout -b feature/mi-cambio
make setup      # la primera vez
make dev
# ... trabajar ...
make lint && make test
```

PR contra `develop`. Nunca contra `main` salvo un `hotfix/*` real.

## Qué se rechaza en revisión

- Lógica de licencias fuera de `backend/app/domain/licensing.py`.
- Un pipeline no reproducible (semilla implícita, orden dependiente del sistema de
  ficheros, `random` global).
- Salida de un modelo aprendido sin etiqueta ni mapa de incertidumbre.
- Copy que prometa resolución angular por combinar observadores separados.
- Un binario de imagen pasando por el backend.
- Tipos de red escritos a mano en el frontend en vez de generados del OpenAPI.
- Una migración que rompa hacia atrás.
- Tests que no se ejecutaron.

## Commits

Convencionales (`feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`, `perf:`).
El cuerpo explica **por qué**, no qué; el qué ya está en el diff.

## Aportar fotografías

Si vas a subir tomas al repositorio: cuanta más metadata, más útiles son. RAW mejor
que JPEG (el JPEG no es lineal en flujo y lo marcamos como fotométricamente poco
fiable). Y elige la licencia con calma: se congela tras la primera descarga, y solo
puede relajarse después. Ver `docs/licensing.md`.
