"""Misma entrada -> mismos bytes, venga en el orden que venga.

`models/tests/test_reproducibility.py` ya cubre dos ejecuciones seguidas y el
manifiesto invertido. Aquí se aprieta un poco más, y desde fuera del componente:

* se **baraja** el orden (invertir es un solo caso; barajar recorre varios),
* se comparan los **bytes del FITS**, no solo los arrays del coadd en memoria,
* se comprueba que el bloque determinista de `provenance.json` no arrastra
  nada volátil (rutas absolutas, horas, versiones).

La suma en coma flotante no es asociativa, así que el orden de entrada es una
amenaza real: si el pipeline no ordena, dos usuarios que pidan el mismo trabajo
obtienen imágenes distintas y `pipeline_version` deja de significar nada.

    backend/.venv/bin/pytest tests/invariants/test_reproducibility.py -q
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path

import pytest

from tests.helpers.pipeline import build_corpus, run_astrostack

pytestmark = [pytest.mark.invariant, pytest.mark.slow]


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> dict:
    return build_corpus(tmp_path_factory.mktemp("repro") / "inputs", n_frames=4, seed=424242)


def _read(manifest: str) -> tuple[dict, list]:
    """Devuelve `(documento, entradas)` del manifiesto, en cualquiera de sus formas."""
    payload = json.loads(Path(manifest).read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        return payload, list(payload.get("inputs") or payload.get("photos") or [])
    return {}, list(payload)


def _derived_manifest(manifest: str, name: str, entries: list) -> Path:
    """Escribe un manifiesto derivado **junto al original**.

    Las rutas de las entradas son relativas al directorio del manifiesto
    (`load_manifest` las resuelve contra `path.parent`), así que un manifiesto
    derivado no puede vivir en otro sitio o no encontraría los FITS. Se conserva
    el resto del documento (en particular `output_license`) para que lo único
    que cambie entre el original y el derivado sea lo que el test quiere probar.
    """
    document, _ = _read(manifest)
    dest = Path(manifest).parent / name
    dest.write_text(json.dumps({**document, "inputs": entries}, indent=2), encoding="utf-8")
    return dest


def _shuffled_manifest(manifest: str, seed: int) -> Path:
    """Copia del manifiesto con las entradas barajadas de forma reproducible."""
    _, entries = _read(manifest)
    rng = random.Random(seed)
    shuffled = list(entries)
    rng.shuffle(shuffled)
    return _derived_manifest(manifest, f"shuffled-{seed}.json", shuffled)


def test_dos_ejecuciones_dan_el_mismo_checksum_y_el_mismo_fits(
    corpus: dict, tmp_path: Path
) -> None:
    """El caso base de la regla dura 3."""
    first = run_astrostack(corpus["manifest"], tmp_path / "a")
    second = run_astrostack(corpus["manifest"], tmp_path / "b")

    assert first.run_checksum == second.run_checksum, (
        "El bloque determinista de provenance.json difiere entre dos ejecuciones "
        "idénticas."
    )
    assert hashlib.sha256(first.fits_bytes()).hexdigest() == (
        hashlib.sha256(second.fits_bytes()).hexdigest()
    ), "El FITS no es byte a byte idéntico entre dos ejecuciones idénticas."


@pytest.mark.parametrize("seed", [1, 7, 99])
def test_barajar_las_entradas_no_cambia_la_salida(
    corpus: dict, tmp_path: Path, seed: int
) -> None:
    """Tres barajados distintos, la misma salida exacta.

    Invertir el manifiesto prueba un solo reordenamiento; barajar con varias
    semillas recorre permutaciones que un `sorted()` mal puesto sí distinguiría.
    """
    reference = run_astrostack(corpus["manifest"], tmp_path / "ref")
    shuffled = _shuffled_manifest(corpus["manifest"], seed)
    other = run_astrostack(shuffled, tmp_path / f"shuf-{seed}")

    assert reference.run_checksum == other.run_checksum, (
        f"Barajar el manifiesto (semilla {seed}) cambia el checksum determinista: "
        "el pipeline depende del orden de entrada."
    )
    assert hashlib.sha256(reference.fits_bytes()).hexdigest() == (
        hashlib.sha256(other.fits_bytes()).hexdigest()
    ), f"Barajar el manifiesto (semilla {seed}) cambia los bytes del FITS."


def test_el_bloque_determinista_no_lleva_nada_volatil(corpus: dict, tmp_path: Path) -> None:
    """Ni horas, ni rutas absolutas, ni versiones de Python en lo que se checksuma.

    Si algo de eso entrase, dos ejecuciones correctas darían checksums distintos
    y el contrato de reproducibilidad sería inservible en la práctica.
    """
    run = run_astrostack(corpus["manifest"], tmp_path / "prov")
    payload = run.provenance()
    det = json.dumps(payload["deterministic"])

    assert str(tmp_path) not in det, "El bloque determinista lleva rutas absolutas."
    for volatile_key in ("started_at", "finished_at", "compute_seconds", "platform"):
        assert volatile_key not in payload["deterministic"], (
            f"{volatile_key!r} está en el bloque determinista; debe ir en el volátil."
        )
        assert volatile_key in payload["volatile"], (
            f"{volatile_key!r} no está en el bloque volátil."
        )


def test_el_checksum_es_sensible(corpus: dict, tmp_path: Path) -> None:
    """Un checksum que nunca cambia no prueba nada.

    Control positivo: si se quita un frame, el checksum **tiene** que cambiar.
    """
    reference = run_astrostack(corpus["manifest"], tmp_path / "full")

    _, entries = _read(corpus["manifest"])
    fewer = _derived_manifest(corpus["manifest"], "fewer.json", entries[:-1])
    reduced = run_astrostack(fewer, tmp_path / "fewer-run")

    assert reference.run_checksum != reduced.run_checksum, (
        "Quitar un frame no cambia el checksum: no está checksumando la entrada."
    )
