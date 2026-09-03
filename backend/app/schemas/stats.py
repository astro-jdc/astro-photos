"""``GET /stats`` 🔓 — contadores de la portada."""

from __future__ import annotations

from datetime import datetime

from app.schemas.common import Schema

__all__ = ["StatsOut"]


class StatsOut(Schema):
    """Contadores agregados del repositorio.

    Sólo cuentan las fotos publicables (``status='ready'`` y sin borrado lógico) y
    las reconstrucciones que alguien puede ver de verdad (``succeeded`` y públicas):
    un contador de portada que incluyera trabajo privado o a medias estaría
    inflando la cifra.
    """

    photo_count: int
    object_count: int
    reconstruction_count: int
    #: Personas con al menos una foto publicada.
    contributor_count: int
    #: Suma de ``exposure_seconds`` de las fotos contadas. Es el número que de verdad
    #: dice cuánto cielo hay acumulado aquí.
    total_exposure_seconds: float
    #: Instante en que se calculó; la respuesta viene de una caché de 5 minutos.
    computed_at: datetime
