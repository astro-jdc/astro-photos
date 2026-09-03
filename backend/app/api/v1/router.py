"""Agrega todos los routers de la v1.

El orden importa: ``search`` declara ``GET /photos`` y debe registrarse antes que
``photos``, que declara ``GET /photos/{photo_id}``; si no, ``/photos`` se intentaría
resolver como un ``photo_id``.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    auth,
    health,
    licenses,
    models,
    objects,
    photos,
    reconstructions,
    search,
    stats,
)

__all__ = ["V1_ROUTERS", "api_router"]

#: Los routers hoja, en orden de registro. Se exponen porque ``include_router``
#: envuelve cada uno y desde el agregado ya no se llega a sus ``APIRoute``; hay
#: código (la anotación de seguridad del OpenAPI) que necesita inspeccionarlas.
V1_ROUTERS = (
    health.router,
    auth.router,
    search.router,
    photos.router,
    objects.router,
    reconstructions.router,
    models.router,
    licenses.router,
    stats.router,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(search.router)
api_router.include_router(photos.router)
api_router.include_router(objects.router)
api_router.include_router(reconstructions.router)
api_router.include_router(models.router)
api_router.include_router(licenses.router)
api_router.include_router(stats.router)
