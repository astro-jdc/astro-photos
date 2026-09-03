"""Única fuente de verdad de licencias de astro-photos.

Ni los routers, ni los workers, ni el frontend replican esta lógica: todos llaman
aquí. Ver ``docs/licensing.md`` y la sección "Regla de compatibilidad de licencias"
de ``docs/data-model.md``.

Módulo **puro**: sin IO, sin base de datos, sin dependencias de FastAPI.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

__all__ = [
    "LICENSE_CATALOG",
    "BlockReason",
    "BlockedPhoto",
    "LicenseChangeDecision",
    "LicenseCode",
    "LicenseInfo",
    "LicenseResolution",
    "PhotoLicenseFacts",
    "can_change_license",
    "enforce_stack_consent",
    "license_info",
    "resolve_output_license",
]


class LicenseCode(StrEnum):
    """Las 8 licencias del catálogo (``docs/licensing.md``).

    El valor es el código tal cual viaja por la API y se guarda en el enum de
    Postgres ``license_code``.
    """

    CC0 = "CC0-1.0"
    CC_BY = "CC-BY-4.0"
    CC_BY_SA = "CC-BY-SA-4.0"
    CC_BY_NC = "CC-BY-NC-4.0"
    CC_BY_NC_SA = "CC-BY-NC-SA-4.0"
    CC_BY_ND = "CC-BY-ND-4.0"
    CC_BY_NC_ND = "CC-BY-NC-ND-4.0"
    ARR = "ARR"


#: Licencia preseleccionada en el formulario de subida.
DEFAULT_LICENSE: LicenseCode = LicenseCode.CC_BY_NC


@dataclass(frozen=True, slots=True)
class LicenseInfo:
    """Flags de una licencia. Espejo exacto de la tabla de referencia ``licenses``."""

    code: LicenseCode
    name: str
    name_es: str
    version: str
    url: str
    allows_commercial: bool
    allows_derivatives: bool
    requires_attribution: bool
    requires_sharealike: bool
    restrictiveness: int
    spdx_id: str | None


_C = LicenseCode

#: Catálogo inmutable. El orden es el de restrictividad creciente.
LICENSE_CATALOG: tuple[LicenseInfo, ...] = (
    LicenseInfo(
        code=_C.CC0,
        name="Public Domain Dedication",
        name_es="Dominio público",
        version="1.0",
        url="https://creativecommons.org/publicdomain/zero/1.0/",
        allows_commercial=True,
        allows_derivatives=True,
        requires_attribution=False,
        requires_sharealike=False,
        restrictiveness=0,
        spdx_id="CC0-1.0",
    ),
    LicenseInfo(
        code=_C.CC_BY,
        name="Attribution",
        name_es="Atribución",
        version="4.0",
        url="https://creativecommons.org/licenses/by/4.0/",
        allows_commercial=True,
        allows_derivatives=True,
        requires_attribution=True,
        requires_sharealike=False,
        restrictiveness=1,
        spdx_id="CC-BY-4.0",
    ),
    LicenseInfo(
        code=_C.CC_BY_SA,
        name="Attribution-ShareAlike",
        name_es="Atribución + CompartirIgual",
        version="4.0",
        url="https://creativecommons.org/licenses/by-sa/4.0/",
        allows_commercial=True,
        allows_derivatives=True,
        requires_attribution=True,
        requires_sharealike=True,
        restrictiveness=2,
        spdx_id="CC-BY-SA-4.0",
    ),
    LicenseInfo(
        code=_C.CC_BY_NC,
        name="Attribution-NonCommercial",
        name_es="Atribución + NoComercial",
        version="4.0",
        url="https://creativecommons.org/licenses/by-nc/4.0/",
        allows_commercial=False,
        allows_derivatives=True,
        requires_attribution=True,
        requires_sharealike=False,
        restrictiveness=3,
        spdx_id="CC-BY-NC-4.0",
    ),
    LicenseInfo(
        code=_C.CC_BY_NC_SA,
        name="Attribution-NonCommercial-ShareAlike",
        name_es="Atribución + NoComercial + CompartirIgual",
        version="4.0",
        url="https://creativecommons.org/licenses/by-nc-sa/4.0/",
        allows_commercial=False,
        allows_derivatives=True,
        requires_attribution=True,
        requires_sharealike=True,
        restrictiveness=4,
        spdx_id="CC-BY-NC-SA-4.0",
    ),
    LicenseInfo(
        code=_C.CC_BY_ND,
        name="Attribution-NoDerivatives",
        name_es="Atribución + SinDerivadas",
        version="4.0",
        url="https://creativecommons.org/licenses/by-nd/4.0/",
        allows_commercial=True,
        allows_derivatives=False,
        requires_attribution=True,
        requires_sharealike=False,
        restrictiveness=5,
        spdx_id="CC-BY-ND-4.0",
    ),
    LicenseInfo(
        code=_C.CC_BY_NC_ND,
        name="Attribution-NonCommercial-NoDerivatives",
        name_es="Atribución + NoComercial + SinDerivadas",
        version="4.0",
        url="https://creativecommons.org/licenses/by-nc-nd/4.0/",
        allows_commercial=False,
        allows_derivatives=False,
        requires_attribution=True,
        requires_sharealike=False,
        restrictiveness=6,
        spdx_id="CC-BY-NC-ND-4.0",
    ),
    LicenseInfo(
        code=_C.ARR,
        name="All Rights Reserved",
        name_es="Todos los derechos reservados",
        version="",
        url="",
        allows_commercial=False,
        allows_derivatives=False,
        requires_attribution=True,
        requires_sharealike=False,
        restrictiveness=7,
        spdx_id=None,
    ),
)

_BY_CODE: dict[LicenseCode, LicenseInfo] = {info.code: info for info in LICENSE_CATALOG}


def license_info(code: LicenseCode | str) -> LicenseInfo:
    """Flags de ``code``. Lanza ``KeyError`` si el código no está en el catálogo."""
    return _BY_CODE[LicenseCode(code)]


class BlockReason(StrEnum):
    """Por qué una foto no puede entrar en una reconstrucción."""

    #: La licencia no permite obras derivadas (ND o ARR).
    NO_DERIVATIVES = "no_derivatives"
    #: El autor retiró explícitamente el consentimiento de uso como frame.
    STACK_OPT_OUT = "stack_opt_out"


@dataclass(frozen=True, slots=True)
class PhotoLicenseFacts:
    """Los únicos hechos de una foto que importan para decidir la licencia de salida.

    Deliberadamente no es el modelo ORM: el dominio no conoce la base de datos.
    """

    photo_id: str
    license: LicenseCode
    allow_derivatives_in_stacks: bool = True
    allow_ai_training: bool = True
    attribution_name: str | None = None


@dataclass(frozen=True, slots=True)
class BlockedPhoto:
    """Una foto rechazada, con motivo legible por el frontend."""

    photo_id: str
    reason: BlockReason
    detail: str
    license: LicenseCode


@dataclass(frozen=True, slots=True)
class LicenseResolution:
    """Resultado de combinar las licencias de las entradas de una reconstrucción."""

    #: Licencia de salida, o ``None`` si el job está bloqueado o no hay entradas.
    resulting_license: LicenseCode | None
    #: Fotos que impiden el job. No vacío ⇒ 422 con el detalle.
    blocked: tuple[BlockedPhoto, ...] = ()
    #: Fotos que sí pueden entrar (mismo orden que la entrada).
    accepted_photo_ids: tuple[str, ...] = ()
    #: Siempre ``True``: la regla 5 de ``docs/licensing.md`` no tiene excepciones.
    requires_attribution: bool = True
    #: Explicación de por qué salió esa licencia, para mostrarla en la UI.
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """``True`` si el job puede encolarse."""
        return not self.blocked and self.resulting_license is not None


def enforce_stack_consent(code: LicenseCode, allow_derivatives_in_stacks: bool) -> bool:
    """Un ND/ARR fuerza ``allow_derivatives_in_stacks=False``.

    ``docs/licensing.md``: "Un ND implica ``allow_derivatives_in_stacks=false`` de
    forma forzosa; la UI lo explica en vez de dejar un estado incoherente".
    Se aplica al escribir la foto, no al leerla, para que la fila sea coherente.
    """
    if not license_info(code).allows_derivatives:
        return False
    return allow_derivatives_in_stacks


def _block(facts: PhotoLicenseFacts) -> BlockedPhoto | None:
    """Regla 1 — bloqueo duro. El motivo de licencia gana al del consentimiento."""
    info = license_info(facts.license)
    if not info.allows_derivatives:
        return BlockedPhoto(
            photo_id=facts.photo_id,
            reason=BlockReason.NO_DERIVATIVES,
            license=facts.license,
            detail=(
                f"La licencia {facts.license.value} no permite obras derivadas, "
                "así que la foto no puede usarse como frame de una reconstrucción."
            ),
        )
    if not facts.allow_derivatives_in_stacks:
        return BlockedPhoto(
            photo_id=facts.photo_id,
            reason=BlockReason.STACK_OPT_OUT,
            license=facts.license,
            detail=(
                "El autor retiró el consentimiento «usar como frame de entrada en "
                "reconstrucciones» (allow_derivatives_in_stacks=false)."
            ),
        )
    return None


def resolve_output_license(inputs: Sequence[PhotoLicenseFacts]) -> LicenseResolution:
    """Devuelve la licencia de salida o la lista de fotos que bloquean el job.

    Reglas de ``docs/licensing.md``, en orden:

    1. **Bloqueo duro** — cualquier entrada con ``allows_derivatives=False`` (ND, ARR)
       o ``allow_derivatives_in_stacks=False`` se rechaza. El job **no se degrada**:
       se devuelve ``blocked`` para que el usuario quite esas fotos y reintente.
    2. **NoComercial es contagioso** — si alguna entrada es NC, la salida es NC.
    3. **ShareAlike es contagioso** — si alguna entrada es SA, la salida es SA.
    4. La salida es la licencia CC menos permisiva necesaria para satisfacer 2 y 3.
       Sin entradas NC ni SA, hereda la más restrictiva presente; ``CC0-1.0`` solo
       sale si **todas** las entradas son ``CC0-1.0``.
    5. **Atribución siempre** — ``requires_attribution`` es ``True`` incluso con
       todo CC0; el pipeline emite ``ATTRIBUTION.md`` y los créditos XMP/FITS.

    Es determinista y no depende del orden de ``inputs``.
    """
    if not inputs:
        return LicenseResolution(
            resulting_license=None,
            notes=("Sin fotos de entrada: no hay nada que combinar.",),
        )

    blocked = tuple(b for b in (_block(f) for f in inputs) if b is not None)
    if blocked:
        # Regla 1: no se degrada la salida, se rechaza el job entero.
        return LicenseResolution(
            resulting_license=None,
            blocked=blocked,
            accepted_photo_ids=tuple(f.photo_id for f in inputs if _block(f) is None),
            notes=(
                f"{len(blocked)} foto(s) no admiten obras derivadas; "
                "quítalas de la selección y reintenta.",
            ),
        )

    infos = [license_info(f.license) for f in inputs]
    any_nc = any(not i.allows_commercial for i in infos)
    any_sa = any(i.requires_sharealike for i in infos)

    notes: list[str] = []
    result: LicenseCode
    if any_nc and any_sa:
        result = LicenseCode.CC_BY_NC_SA
        notes.append("Alguna entrada es NoComercial y alguna es CompartirIgual.")
    elif any_nc:
        result = LicenseCode.CC_BY_NC
        notes.append("Alguna entrada es NoComercial: la salida hereda NC.")
    elif any_sa:
        result = LicenseCode.CC_BY_SA
        notes.append("Alguna entrada es CompartirIgual: la salida hereda SA.")
    else:
        # Regla 4: sin NC ni SA solo quedan CC0 y CC-BY. CC0 requiere unanimidad.
        result = max(infos, key=lambda i: i.restrictiveness).code
        notes.append(
            "Todas las entradas son de dominio público."
            if result is LicenseCode.CC0
            else "Sin NC ni SA: la salida hereda la más restrictiva presente."
        )

    notes.append("Atribución obligatoria en todo caso: se emite ATTRIBUTION.md y créditos XMP.")
    return LicenseResolution(
        resulting_license=result,
        blocked=(),
        accepted_photo_ids=tuple(f.photo_id for f in inputs),
        requires_attribution=True,
        notes=tuple(notes),
    )


@dataclass(frozen=True, slots=True)
class LicenseChangeDecision:
    """¿Puede el autor cambiar la licencia de su foto?"""

    allowed: bool
    reason: str
    current: LicenseCode
    requested: LicenseCode


def can_change_license(
    current: LicenseCode,
    new: LicenseCode,
    locked_at: datetime | None,
) -> LicenseChangeDecision:
    """Congelado de licencia (``docs/licensing.md``).

    Mientras ``license_locked_at`` sea ``None`` (nadie descargó la foto ni la usó en
    una reconstrucción publicada) la licencia se cambia libremente. A partir de ahí
    solo puede **relajarse**: bajar de restrictividad. Es la misma irrevocabilidad
    que declaran las propias licencias CC para quien ya recibió la obra.
    """
    if current == new:
        return LicenseChangeDecision(True, "La licencia no cambia.", current, new)
    if locked_at is None:
        return LicenseChangeDecision(
            True,
            "La licencia no está congelada todavía: puede cambiarse libremente.",
            current,
            new,
        )
    cur_r = license_info(current).restrictiveness
    new_r = license_info(new).restrictiveness
    if new_r < cur_r:
        return LicenseChangeDecision(
            True, "La licencia se relaja, lo cual siempre está permitido.", current, new
        )
    return LicenseChangeDecision(
        False,
        (
            f"La licencia se congeló el {locked_at.isoformat()} porque la foto ya fue "
            f"descargada o usada en una reconstrucción publicada. Desde entonces solo "
            f"puede relajarse, y {new.value} es más restrictiva que {current.value}."
        ),
        current,
        new,
    )


def most_restrictive(codes: Iterable[LicenseCode]) -> LicenseCode | None:
    """La licencia de mayor ``restrictiveness`` del conjunto, o ``None`` si está vacío.

    Utilidad de presentación; **no** es la regla de combinación (esa es
    :func:`resolve_output_license`, que no es un simple máximo).
    """
    infos = [license_info(c) for c in codes]
    if not infos:
        return None
    return max(infos, key=lambda i: i.restrictiveness).code
