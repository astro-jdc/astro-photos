"""``/licenses`` 🔓 — catálogo y resolución.

``POST /licenses/resolve`` expone **la misma función de dominio** que usa el motor de
reconstrucción, para que el frontend pueda avisar antes de dejar pulsar el botón.
Si esto y ``ReconstructionService`` alguna vez dieran resultados distintos, sería un
bug de duplicación: por eso ambos llaman a ``resolve_output_license`` y nada más.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import OptionalDbUser, get_photo_repository
from app.domain.licensing import DEFAULT_LICENSE, PhotoLicenseFacts, resolve_output_license
from app.repositories.photo import PhotoRepository
from app.schemas.license import LicenseCatalogOut, LicenseResolveIn, LicenseResolveOut

router = APIRouter(prefix="/licenses", tags=["licenses"])


@router.get("", response_model=LicenseCatalogOut, summary="Catálogo con flags")
async def list_licenses(viewer: OptionalDbUser) -> LicenseCatalogOut:
    """El ``default_license`` devuelto es el del usuario si está autenticado."""
    default = viewer.default_license if viewer is not None else DEFAULT_LICENSE
    return LicenseCatalogOut.build(default)


@router.post(
    "/resolve",
    response_model=LicenseResolveOut,
    summary="Licencia resultante de combinar un conjunto de fotos",
)
async def resolve_licenses(
    payload: LicenseResolveIn,
    repo: Annotated[PhotoRepository, Depends(get_photo_repository)],
) -> LicenseResolveOut:
    photos = await repo.get_many(payload.photo_ids)
    found = {p.id for p in photos}
    unknown = [pid for pid in payload.photo_ids if pid not in found]
    resolution = resolve_output_license(
        [
            PhotoLicenseFacts(
                photo_id=str(p.id),
                license=p.license,
                allow_derivatives_in_stacks=p.allow_derivatives_in_stacks,
                allow_ai_training=p.allow_ai_training,
                attribution_name=p.attribution_name,
            )
            for p in photos
        ]
    )
    return LicenseResolveOut.from_domain(resolution, unknown=unknown)
