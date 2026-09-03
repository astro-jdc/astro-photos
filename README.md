# astro-photos

Repositorio colaborativo de astrofotografía. La gente sube sus tomas con metadata
rica —instante exacto, coordenadas GPS, óptica, filtro, licencia Creative Commons— y
la plataforma combina muchas tomas de muchos observadores en una sola imagen más
profunda, mejor muestreada y fotométricamente calibrada.

## Qué hace, exactamente

- **Subir** fotos (RAW, TIFF, FITS, JPEG) con licencia CC a elegir —`CC-BY-NC-4.0`
  viene por defecto— y consentimientos separados para entrenamiento de IA y para uso
  como frame en reconstrucciones de terceros.
- **Enriquecer** automáticamente: EXIF/XMP, *plate solving* (WCS con distorsión),
  medida de PSF/FWHM, fondo de cielo, punto cero fotométrico contra Gaia, airmass,
  fase de la Luna, estimación de Bortle. La metadata derivada es la que de verdad
  hace posible combinar tomas heterogéneas.
- **Buscar** por objeto, por cono en el cielo, por cercanía geográfica, por fecha,
  por focal, por filtro, por licencia compatible con lo que quieras hacer, o por
  similitud visual.
- **Reconstruir**: eliges un objeto, el sistema selecciona los mejores frames de todo
  el repositorio y produce un apilado profundo con procedencia completa y la licencia
  correctamente derivada de todas las entradas.
- **Entrenar y mejorar** los modelos de reconstrucción con los propios datos del
  repositorio, versionados y auditables.

## Qué NO hace (y por qué importa decirlo)

Combinar fotografías de observadores separados **no** sintetiza una apertura. No es
interferometría y no es el Event Horizon Telescope: una cámara registra intensidad,
no fase, así que dos personas a 1000 km no forman un telescopio de 1000 km. Sus
imágenes se combinan **incoherentemente**.

Lo que sí se gana, y es mucho: profundidad (SNR ∝ √N), recuperación de detalle que el
muestreo de cada cámara aliasaba, rango dinámico, fusión multi-escala y dominio
temporal (variables, asteroides, transitorios). Lo que no se gana: resolución angular
más allá del límite de difracción de la mejor óptica contribuyente.

El razonamiento completo, con papers, está en
[`docs/research/multi-image-astro-reconstruction.md`](docs/research/multi-image-astro-reconstruction.md).

## Arquitectura

| | |
|---|---|
| Frontend | Nuxt 4 (Vue 3, TypeScript), CloudFront + S3 |
| Backend | FastAPI (Python 3.12), ECS Fargate tras un ALB |
| Datos | Aurora Serverless v2 · PostgreSQL 16 + PostGIS + pgvector |
| Ficheros | S3 (subida y descarga por URL presignada; el binario no pasa por el backend) |
| Auth | Cognito User Pool (JWT validado por JWKS) |
| Cómputo pesado | AWS Batch sobre instancias GPU spot, disparado por SQS |
| IaC | AWS CDK v2 en Python, un stack por entorno |
| Reconstrucción | paquete `astrostack` en `models/`, ejecutable en local sin AWS ni GPU |

Detalle en [`docs/architecture.md`](docs/architecture.md).

## Empezar

```bash
make setup     # venvs de backend/models/infra + pnpm del frontend
make dev       # postgis + minio + backend + frontend en local
make test      # todo
```

Requisitos: Python 3.12, Node 22, pnpm, podman (o docker).

## Ramas y entornos

`main` es producción, `develop` es staging con su propia infraestructura completa.
Las features salen de `develop`. Ver [`docs/branching.md`](docs/branching.md).

## Documentación

- [Arquitectura](docs/architecture.md) · [Modelo de datos](docs/data-model.md) · [API](docs/api.md)
- [Licencias](docs/licensing.md) — cómo se combinan las CC en una obra derivada
- [Ramas y despliegue](docs/branching.md)
- [Investigación: reconstrucción multi-imagen](docs/research/multi-image-astro-reconstruction.md)
- [ADRs](docs/adr/)

## Licencia

Código: AGPL-3.0 (ver [LICENSE](LICENSE)). Las fotografías de los usuarios conservan
la licencia Creative Commons que cada autor eligió; el código no reclama derecho
alguno sobre ellas.
