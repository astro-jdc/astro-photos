# 0003 — Subida directa a S3 por URL presignada

Estado: Aceptado · 2026-09-03

## Contexto

Un fichero RAW de una réflex moderna pesa 30–80 MB; un FITS de 32 bits o un apilado
de un usuario puede pasar de 500 MB. Pasar ese tráfico por el backend obligaría a
sobredimensionar las tareas de Fargate para algo que S3 hace mejor y más barato.

## Decisión

Flujo de tres pasos (detallado en `docs/api.md`):

1. `POST /photos/uploads` valida cuota, tipo y checksum duplicado y devuelve un
   **POST presignado** de S3 (no PUT: el POST permite imponer
   `content-length-range` y tags del lado del servidor).
2. El cliente sube **directo a S3**.
3. `POST /photos/{id}/complete` aporta la metadata y encola la ingesta.

En paralelo, un evento `s3:ObjectCreated` sobre el prefijo `staging/` dispara una
Lambda de verificación (tamaño real, magic bytes, antivirus) que puede marcar la foto
como `quarantined`.

Las descargas son simétricas: URL de CloudFront firmada, nunca streaming por la API.

## Consecuencias

- El backend no toca bytes de imagen: las tareas de Fargate son pequeñas y estables.
- El progreso de subida y el multipart los gestiona el navegador, que ya sabe hacerlo.
- Hay una ventana entre los pasos 2 y 3 en la que existe un objeto sin metadata: se
  resuelve con una regla de ciclo de vida que borra `staging/` a los 7 días y con el
  estado `uploading` en la tabla.
- La validación de contenido **no puede** hacerse antes de subir; de ahí la Lambda de
  verificación y el estado `quarantined`.

## Alternativas descartadas

- **Subida a través del backend.** Coste y acoplamiento innecesarios.
- **PUT presignado.** No permite acotar el tamaño ni forzar tags en la propia política.
