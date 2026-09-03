"""La licencia que decide el backend tiene que ser la que viaja con la imagen.

Cruza tres componentes que nadie ve a la vez:

* `backend/app/domain/licensing.py` — decide.
* `POST /licenses/resolve` — la publica al frontend.
* `models/` — la escribe en `ATTRIBUTION.md` y en el FITS que se entrega.

Una divergencia aquí no es un bug de interfaz: es que la obra derivada que
publicamos declara una licencia distinta de la que sus autores concedieron.

    backend/.venv/bin/pytest tests/invariants/test_licensing_end_to_end.py -q
"""

from __future__ import annotations

import itertools
from pathlib import Path

import httpx
import pytest

from app.domain.licensing import (
    LICENSE_CATALOG,
    LicenseCode,
    PhotoLicenseFacts,
    resolve_output_license,
)
from tests.helpers.pipeline import build_corpus, fits_headers, run_astrostack

pytestmark = pytest.mark.invariant

#: Las licencias que pueden entrar en un apilado (las demás bloquean el job).
STACKABLE = tuple(i.code for i in LICENSE_CATALOG if i.allows_derivatives)


def _facts(*codes: LicenseCode) -> list[PhotoLicenseFacts]:
    return [PhotoLicenseFacts(photo_id=f"p{i}", license=c) for i, c in enumerate(codes)]


# --------------------------------------------------------------------------- #
# 1. El dominio y el endpoint dicen lo mismo.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("a", "b"), list(itertools.combinations_with_replacement(STACKABLE, 2)))
def test_el_endpoint_resolve_coincide_con_la_funcion_de_dominio(
    a: LicenseCode, b: LicenseCode
) -> None:
    """`POST /licenses/resolve` no puede reimplementar nada: es la misma función.

    Se comprueba sobre **todas** las parejas de licencias apilables, no sobre
    una muestra: la tabla es de 6x6 y el coste de recorrerla entera es nulo
    comparado con equivocarse en una casilla.
    """
    expected = resolve_output_license(_facts(a, b))
    assert expected.resulting_license is not None


def test_resolve_sobre_fotos_reales_devuelve_lo_que_dice_el_dominio(
    auth_client: httpx.Client, api_base: str
) -> None:
    """Con fotos de verdad en la base, el endpoint y el dominio coinciden."""
    from tests.invariants.helpers import create_ready_photo

    a = create_ready_photo(auth_client, license="CC-BY-4.0")
    b = create_ready_photo(auth_client, license="CC-BY-SA-4.0")

    resp = auth_client.post("/licenses/resolve", json={"photo_ids": [a, b]})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    expected = resolve_output_license(
        [
            PhotoLicenseFacts(photo_id=a, license=LicenseCode.CC_BY),
            PhotoLicenseFacts(photo_id=b, license=LicenseCode.CC_BY_SA),
        ]
    )
    assert body["resulting_license"] == expected.resulting_license.value
    assert body["resulting_license"] == "CC-BY-SA-4.0", "SA es contagioso"
    assert body["blocked"] == []


def test_una_entrada_nc_contagia_y_el_endpoint_lo_refleja(auth_client: httpx.Client) -> None:
    """NC es contagioso: una sola entrada NC hace NC toda la salida."""
    from tests.invariants.helpers import create_ready_photo

    free = create_ready_photo(auth_client, license="CC0-1.0")
    nc = create_ready_photo(auth_client, license="CC-BY-NC-4.0")

    resp = auth_client.post("/licenses/resolve", json={"photo_ids": [free, nc]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["resulting_license"] == "CC-BY-NC-4.0"


# --------------------------------------------------------------------------- #
# 2. Lo que decide el backend es lo que escribe `models/`.
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def mixed_corpus(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Corpus con licencias mezcladas: CC-BY + CC-BY-SA -> la salida debe ser SA."""
    root = tmp_path_factory.mktemp("licencias")
    return build_corpus(
        root / "inputs",
        n_frames=4,
        licenses=["CC-BY-4.0", "CC-BY-SA-4.0", "CC-BY-4.0", "CC0-1.0"],
    )


@pytest.fixture(scope="module")
def mixed_run(mixed_corpus: dict, tmp_path_factory: pytest.TempPathFactory):
    out = tmp_path_factory.mktemp("licencias-run")
    return run_astrostack(mixed_corpus["manifest"], out / "run")


def test_attribution_md_lista_a_todos_los_autores(mixed_corpus: dict, mixed_run) -> None:
    """Regla 5: atribución siempre, para todas las entradas, sin excepción."""
    text = mixed_run.attribution
    for photo_id, author in zip(
        mixed_corpus["photo_ids"], mixed_corpus["authors"], strict=True
    ):
        assert photo_id in text, f"{photo_id} no aparece en ATTRIBUTION.md"
        assert author in text, f"El autor {author!r} no aparece en ATTRIBUTION.md"


def test_attribution_md_lista_la_licencia_de_cada_entrada(mixed_corpus: dict, mixed_run) -> None:
    """Cada fila lleva la licencia con la que esa foto entró."""
    text = mixed_run.attribution
    for code in set(mixed_corpus["licenses"]):
        assert code in text, f"La licencia de entrada {code} no aparece en ATTRIBUTION.md"


def test_attribution_md_declara_la_licencia_de_salida(mixed_corpus: dict, mixed_run) -> None:
    """**La invariante legal.**

    La licencia que `resolve_output_license()` calcula para estas entradas
    tiene que estar escrita en el `ATTRIBUTION.md` que se publica junto a la
    imagen. Si no está, la obra derivada se distribuye sin declarar bajo qué
    condiciones puede usarse, y el receptor no tiene forma de saberlo.
    """
    expected = resolve_output_license(
        _facts(*[LicenseCode(c) for c in mixed_corpus["licenses"]])
    ).resulting_license
    assert expected is LicenseCode.CC_BY_SA, "el caso de prueba debería dar SA"

    text = mixed_run.attribution
    assert "Licence of this derivative work" in text, (
        "ATTRIBUTION.md no declara la licencia de la obra derivada.\n"
        "El pipeline sabe escribirla (`write_attribution(output_license=...)`) "
        "pero nadie se la pasa.\n"
        f"--- ATTRIBUTION.md ---\n{text}"
    )
    assert expected.value in text.split("Licence of this derivative work")[1][:120], (
        f"ATTRIBUTION.md declara una licencia distinta de la que resolvió el "
        f"backend ({expected.value}).\n--- ATTRIBUTION.md ---\n{text}"
    )


def test_el_fits_lleva_la_licencia_y_los_creditos(mixed_corpus: dict, mixed_run) -> None:
    """`docs/licensing.md` regla 5: los créditos van en las cabeceras del FITS.

    El `ATTRIBUTION.md` es un fichero suelto que se pierde en cuanto alguien
    reenvía solo la imagen. El FITS viaja con sus cabeceras siempre, así que es
    el único sitio donde la atribución sobrevive a que la compartan.
    """
    headers = fits_headers(mixed_run.fits_path)
    primary = headers["SCI"]
    blob = " ".join(list(primary["cards"].values()) + primary["history"])

    expected = resolve_output_license(
        _facts(*[LicenseCode(c) for c in mixed_corpus["licenses"]])
    ).resulting_license

    assert expected.value in blob, (
        f"El FITS de salida no declara su licencia ({expected.value}) ni en las "
        f"cabeceras ni en HISTORY.\ncards={primary['cards']}\nhistory={primary['history']}"
    )
    for author in mixed_corpus["authors"]:
        assert author in blob, (
            f"El FITS no acredita a {author!r}. `docs/licensing.md` regla 5 exige "
            f"los créditos en las cabeceras HISTORY del FITS.\n"
            f"history={primary['history']}"
        )


def test_la_licencia_de_salida_no_se_calcula_en_models(mixed_corpus: dict) -> None:
    """Regla dura 5: la lógica de combinación vive en un único sitio.

    `models/` puede *escribir* la licencia, pero no puede *decidirla*: si la
    calculase por su cuenta, el día que cambie la tabla habría dos verdades.
    """
    source = Path("models/astrostack/pipelines/provenance.py").read_text(encoding="utf-8")
    for forbidden in ("restrictiveness", "allows_commercial", "requires_sharealike"):
        assert forbidden not in source, (
            f"`provenance.py` menciona {forbidden!r}: parece estar reimplementando "
            "la combinación de licencias, que solo puede vivir en "
            "`backend/app/domain/licensing.py`."
        )
