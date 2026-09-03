"""Same input + same config -> same bytes. Non-negotiable.

Hard rule 3 of ``CLAUDE.md``. The threats this file pins down, one test each:

* **Wall-clock leaking into the output.** ``provenance.json`` separates a
  deterministic block (checksummed) from a volatile one (timestamps, host,
  library versions), and no timestamp is written into the FITS header.
* **Filesystem ordering.** The manifest is sorted by ``photo_id``; the same
  files presented in a different order must give the same bytes. Float
  addition is not associative, so this is a real hazard, not a theoretical
  one.
* **Global RNG state.** Nothing uses ``random`` or the legacy
  ``np.random`` singleton; every stochastic step derives from the run seed
  and the stage name.
* **Stage ordering.** The topological sort breaks ties by declaration index,
  so a graph with parallel branches still executes in one fixed order.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import numpy as np
import pytest

from astrostack.io.writers import checksum_arrays
from astrostack.pipelines.graph import StageSpec, load_pipeline, topological_order
from astrostack.pipelines.provenance import file_sha256
from astrostack.pipelines.runner import run_pipeline
from astrostack.rng import derive_seed, generator
from astrostack.stack import optimal_coadd
from tests.synthetic import make_corpus, write_corpus

CONFIG = Path(__file__).resolve().parent.parent / "configs" / "classical-stack-v1.yaml"


@pytest.fixture(scope="module")
def corpus_on_disk(tmp_path_factory):
    root = tmp_path_factory.mktemp("repro")
    field = make_corpus(
        n_frames=5, shape=(96, 96), n_stars=14, seed=555,
        fwhm_pixels=3.0, sky_level=250.0, dither_pixels=1.0, sky_gradient=0.2,
        n_cosmic_rays=3,
    )  # fmt: skip
    _, manifest = write_corpus(field, root / "inputs")
    return field, Path(manifest)


def test_two_runs_produce_identical_bytes(corpus_on_disk, tmp_path):
    """The whole point. Two runs, byte-identical FITS and equal run checksum."""
    _, manifest = corpus_on_disk

    first = run_pipeline(CONFIG, manifest, tmp_path / "run1")
    second = run_pipeline(CONFIG, manifest, tmp_path / "run2")

    assert first.run_checksum == second.run_checksum, "deterministic provenance block differs"

    fits1 = tmp_path / "run1" / "coadd.fits"
    fits2 = tmp_path / "run2" / "coadd.fits"
    assert file_sha256(fits1) == file_sha256(fits2), "the FITS files differ byte for byte"

    png1 = tmp_path / "run1" / "preview.png"
    png2 = tmp_path / "run2" / "preview.png"
    assert file_sha256(png1) == file_sha256(png2)

    text1 = (tmp_path / "run1" / "ATTRIBUTION.md").read_text()
    text2 = (tmp_path / "run2" / "ATTRIBUTION.md").read_text()
    assert text1 == text2


def test_provenance_isolates_the_volatile_block(corpus_on_disk, tmp_path):
    """Timestamps must exist, and must be outside the checksum."""
    _, manifest = corpus_on_disk
    run = run_pipeline(CONFIG, manifest, tmp_path / "run")
    payload = json.loads((tmp_path / "run" / "provenance.json").read_text())

    assert payload["run_checksum"] == run.run_checksum
    assert "started_at" in payload["volatile"]
    assert "finished_at" in payload["volatile"]
    assert "packages" in payload["volatile"]

    blob = json.dumps(payload["deterministic"])
    assert "started_at" not in blob
    assert "compute_seconds" not in blob

    det = payload["deterministic"]
    assert det["seed"] == run.config.seed
    assert det["inputs"] == sorted(det["inputs"], key=lambda r: r["photo_id"])
    assert all(row["file_sha256"] for row in det["inputs"])
    assert all(row["data_sha256"] for row in det["inputs"])
    assert det["effective_weights"]
    assert det["outputs"]["fits"]["sha256"]


def test_global_rng_state_does_not_leak_into_the_result(corpus_on_disk, tmp_path):
    """Perturbing the global RNG between runs must change nothing.

    This is the test that catches a stray ``np.random.normal`` or
    ``random.shuffle`` anywhere in the pipeline.
    """
    _, manifest = corpus_on_disk

    random.seed(1)
    np.random.seed(1)  # noqa: NPY002 - deliberately poisoning the legacy global state
    first = run_pipeline(CONFIG, manifest, tmp_path / "a")

    random.seed(999999)
    np.random.seed(999999)  # noqa: NPY002
    [random.random() for _ in range(1000)]
    np.random.random(1000)  # noqa: NPY002
    second = run_pipeline(CONFIG, manifest, tmp_path / "b")

    assert first.run_checksum == second.run_checksum
    assert file_sha256(tmp_path / "a" / "coadd.fits") == file_sha256(tmp_path / "b" / "coadd.fits")


def test_input_order_does_not_change_the_result(corpus_on_disk, tmp_path):
    """A manifest listed in reverse must produce the same coadd."""
    _, manifest = corpus_on_disk
    entries = json.loads(manifest.read_text())

    reversed_manifest = manifest.parent / "reversed.json"
    reversed_manifest.write_text(json.dumps(list(reversed(entries)), indent=2))

    forward = run_pipeline(CONFIG, manifest, tmp_path / "fwd")
    backward = run_pipeline(CONFIG, reversed_manifest, tmp_path / "rev")

    a = forward.coadd
    b = backward.coadd
    assert a is not None and b is not None
    assert checksum_arrays(image=a.image, weight=a.weight) == checksum_arrays(
        image=b.image, weight=b.weight
    )


def test_changing_a_parameter_changes_the_checksum(corpus_on_disk, tmp_path):
    """The checksum must be sensitive, or it proves nothing."""
    _, manifest = corpus_on_disk
    base = run_pipeline(CONFIG, manifest, tmp_path / "base")
    tweaked = run_pipeline(
        CONFIG, manifest, tmp_path / "tweaked", overrides={"coadd": {"epsilon": 1e-2}}
    )
    assert base.run_checksum != tweaked.run_checksum
    assert base.coadd is not None and tweaked.coadd is not None
    assert not np.allclose(base.coadd.image, tweaked.coadd.image)


def test_changing_the_seed_changes_the_audit_but_not_the_coadd(corpus_on_disk, tmp_path):
    """The seed drives the injection audit; the classical coadd is seed-free."""
    _, manifest = corpus_on_disk
    a = run_pipeline(CONFIG, manifest, tmp_path / "s1", seed=1)
    b = run_pipeline(CONFIG, manifest, tmp_path / "s2", seed=2)

    assert a.run_checksum != b.run_checksum
    assert a.coadd is not None and b.coadd is not None
    assert checksum_arrays(image=a.coadd.image) == checksum_arrays(image=b.coadd.image)


def test_coaddition_alone_is_deterministic(homogeneous_corpus):
    """No filesystem, no config: the numerics themselves must repeat."""
    _, frames, _ = homogeneous_corpus
    a = optimal_coadd(frames)
    b = optimal_coadd(frames)
    assert checksum_arrays(
        image=a.image, weight=a.weight, psf=a.psf, uncertainty=a.uncertainty
    ) == checksum_arrays(image=b.image, weight=b.weight, psf=b.psf, uncertainty=b.uncertainty)


def test_seed_derivation_is_stable_across_processes():
    """Seeds must not depend on PYTHONHASHSEED.

    ``hash("stage")`` is salted per process in CPython, so deriving a seed
    from it would silently make every run irreproducible on a fresh
    interpreter. BLAKE2b is not.
    """
    assert derive_seed(42, "coadd", "inject") == derive_seed(42, "coadd", "inject")
    assert derive_seed(42, "coadd") != derive_seed(42, "register")
    assert derive_seed(42, "coadd") != derive_seed(43, "coadd")
    # Pinned literal: a change here means every previously stored run's audit
    # can no longer be reproduced, which must be a deliberate act.
    assert derive_seed(20240101, "injection", "positions") == 14517401142139154957

    a = generator(7, "x").normal(size=5)
    b = generator(7, "x").normal(size=5)
    assert np.array_equal(a, b)


def test_topological_order_is_deterministic():
    """Independent branches must always execute in declaration order."""
    stages = [
        StageSpec(id="load", op="io.load"),
        StageSpec(id="b", op="io.load", needs=["load"]),
        StageSpec(id="a", op="io.load", needs=["load"]),
        StageSpec(id="join", op="io.load", needs=["a", "b"]),
    ]
    order = [s.id for s in topological_order(stages)]
    assert order == ["load", "b", "a", "join"]
    assert [s.id for s in topological_order(stages)] == order


def test_pipeline_fingerprint_ignores_prose():
    """Editing a comment must not invalidate a cached reconstruction."""
    cfg = load_pipeline(CONFIG)
    described = cfg.model_copy(update={"description": "a completely different description"})
    assert cfg.fingerprint() == described.fingerprint()

    changed = cfg.model_copy(update={"seed": cfg.seed + 1})
    assert cfg.fingerprint() != changed.fingerprint()
