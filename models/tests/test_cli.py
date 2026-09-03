"""End-to-end CLI, on synthetic data, with no AWS and no GPU.

Rule 5 of the astro-ml brief: every pipeline runs from the command line on a
laptop. These tests execute the shipped configs unmodified — if a config drifts
away from the stage vocabulary, this is what notices.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from click.testing import CliRunner

from astrostack.cli import main
from tests.synthetic import make_corpus, write_corpus

CONFIG_DIR = Path(__file__).resolve().parent.parent / "configs"


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    root = tmp_path_factory.mktemp("cli")
    field = make_corpus(
        n_frames=6, shape=(96, 96), n_stars=16, seed=31337,
        fwhm_pixels=3.0, sky_level=220.0, dither_pixels=1.2, sky_gradient=0.25,
        n_cosmic_rays=4, trail_frames=(2,),
    )  # fmt: skip
    directory, manifest = write_corpus(field, root / "inputs")
    return field, Path(directory), Path(manifest)


@pytest.fixture
def runner():
    return CliRunner()


# NOTE: the JSON payload is read from ``result.stdout``, never from
# ``result.output``. structlog writes to stderr on purpose so that
# ``astrostack run ... | jq`` works; mixing the two here would mean asserting
# on the log format instead of on the contract.


def test_help_lists_the_three_commands(runner):
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0, result.output
    for command in ("run", "inspect", "metrics"):
        assert command in result.output


@pytest.mark.parametrize("config", ["classical-stack-v1.yaml", "drizzle-v1.yaml", "burst-sr-v1.yaml"])
def test_shipped_configs_validate(runner, config):
    """Every config must parse and reference only real ops."""
    result = runner.invoke(main, ["validate", str(CONFIG_DIR / config)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["valid"]
    assert payload["execution_order"][0]["op"] == "io.load"


def test_run_classical_stack_end_to_end(runner, corpus, tmp_path):
    _, _, manifest = corpus
    out = tmp_path / "out"
    result = runner.invoke(
        main, ["run", str(CONFIG_DIR / "classical-stack-v1.yaml"), "--inputs", str(manifest), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)

    assert payload["pipeline"] == "classical-stack-v1"
    assert payload["n_inputs"] == 6
    assert payload["coadd"]["method"] == "zackay-ofek:proper-coadd"
    assert payload["coadd"]["flux_preserving"] is True
    assert payload["run_checksum"]

    for name in ("coadd.fits", "preview.png", "provenance.json", "ATTRIBUTION.md"):
        assert (out / name).is_file(), f"{name} was not written"

    # The audit must have run and must be linear.
    audit = payload["metrics"]["injection_audit"]
    assert audit["is_linear"], audit
    assert audit["slope"] == pytest.approx(1.0, abs=0.2)

    # And the coadd must beat the honest baseline, not lose to it.
    assert payload["metrics"]["snr_gain_db"] > -0.5


def test_output_fits_has_every_mandated_extension(runner, corpus, tmp_path):
    """Linear 32-bit FITS with WCS, weight map, uncertainty map and PSF."""
    from astropy.io import fits
    from astropy.wcs import WCS

    _, _, manifest = corpus
    out = tmp_path / "out"
    result = runner.invoke(
        main, ["run", str(CONFIG_DIR / "classical-stack-v1.yaml"), "--inputs", str(manifest), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output

    with fits.open(out / "coadd.fits") as hdul:
        names = [hdu.header.get("EXTNAME") for hdu in hdul]
        assert names[0] == "SCI"
        assert "WEIGHT" in names
        assert "UNCERT" in names
        assert "PSF" in names

        # FITS stores big-endian; what matters is that it is 32-bit float.
        assert hdul[0].data.dtype.kind == "f"
        assert hdul[0].data.dtype.itemsize == 4
        wcs = WCS(hdul[0].header)
        assert wcs.has_celestial
        assert hdul[0].header["PIPELINE"] == "classical-stack-v1"
        assert hdul[0].header["FLUXCONS"] is True
        # No wall-clock stamp: it would break byte-for-byte reproducibility.
        assert "DATE" not in hdul[0].header

        psf = hdul["PSF"].data
        assert float(psf.sum()) == pytest.approx(1.0, abs=1e-4)


def test_run_drizzle_config(runner, corpus, tmp_path):
    _, _, manifest = corpus
    out = tmp_path / "drizzle"
    result = runner.invoke(
        main, ["run", str(CONFIG_DIR / "drizzle-v1.yaml"), "--inputs", str(manifest), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["coadd"]["method"] == "drizzle"
    assert payload["coadd"]["flux_preserving"] is True
    assert (out / "drizzle.fits").is_file()

    metrics = payload["coadd"]["metrics"]
    assert 0.0 < metrics["pixfrac"] <= 1.0
    assert metrics["hole_fraction"] < 0.2
    # Oversampling is only granted when the measured dither justifies it.
    assert metrics["oversample"] <= 2.0
    if metrics["oversample"] > 1.0:
        assert metrics["dither_score"] >= 0.4


def test_run_accepts_a_directory_of_images(runner, corpus, tmp_path):
    """A bare directory works too, with photo_id taken from the filename."""
    _, directory, _ = corpus
    out = tmp_path / "dir"
    result = runner.invoke(
        main,
        ["run", str(CONFIG_DIR / "classical-stack-v1.yaml"), "--inputs", str(directory), "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert (out / "coadd.fits").is_file()


def test_set_overrides_a_stage_parameter(runner, corpus, tmp_path):
    _, _, manifest = corpus
    out = tmp_path / "over"
    result = runner.invoke(
        main,
        [
            "run", str(CONFIG_DIR / "classical-stack-v1.yaml"),
            "--inputs", str(manifest), "--out", str(out),
            "--set", "coadd.epsilon=0.01",
            "--set", "audit.enabled=false",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    provenance = json.loads((out / "provenance.json").read_text())
    stages = {s["stage_id"]: s for s in provenance["deterministic"]["stages"]}
    assert stages["coadd"]["params"]["epsilon"] == 0.01
    assert stages["audit"]["params"]["enabled"] is False


def test_set_rejects_malformed_overrides(runner, corpus, tmp_path):
    _, _, manifest = corpus
    result = runner.invoke(
        main,
        [
            "run", str(CONFIG_DIR / "classical-stack-v1.yaml"),
            "--inputs", str(manifest), "--out", str(tmp_path / "bad"),
            "--set", "nonsense",
        ],
    )  # fmt: skip
    assert result.exit_code != 0
    assert "STAGE.PARAM=VALUE" in result.output


def test_attribution_lists_every_contributor(runner, corpus, tmp_path):
    _, _, manifest = corpus
    out = tmp_path / "attr"
    runner.invoke(
        main, ["run", str(CONFIG_DIR / "classical-stack-v1.yaml"), "--inputs", str(manifest), "--out", str(out)]
    )
    text = (out / "ATTRIBUTION.md").read_text()
    for i in range(6):
        assert f"synthetic-{i:03d}" in text
        assert f"observer-{i:03d}" in text
    assert "CC-BY-4.0" in text
    assert "effective weight" in text


def test_inspect_reports_metadata_and_quality(runner, corpus):
    _, directory, _ = corpus
    image = directory / "synthetic-000.fits"
    result = runner.invoke(main, ["inspect", str(image)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)

    assert payload["format"] == "fits"
    assert payload["shape"] == [96, 96]
    assert payload["photometrically_unreliable"] is False
    assert payload["astrometry"]["pixel_scale_arcsec"] == pytest.approx(2.0, rel=1e-3)
    # A single 60 s frame of a power-law luminosity function detects only its
    # bright end; the point of the corpus is that the stack goes deeper.
    assert payload["quality"]["star_count"] >= 1
    assert payload["quality"]["fwhm_pixels"] > 1.0
    assert payload["background"]["median_level"] > 0
    assert payload["data_sha256"]


def test_metrics_compares_two_images(runner, corpus):
    _, directory, _ = corpus
    a = directory / "synthetic-000.fits"
    b = directory / "synthetic-001.fits"
    result = runner.invoke(main, ["metrics", str(a), str(b)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)

    assert payload["comparison"]["psnr_db"] > 10
    assert 0.0 <= payload["comparison"]["ssim"] <= 1.0
    assert payload["comparison"]["total_flux_ratio"] == pytest.approx(1.0, rel=0.05)
    assert "PSNR" in payload["note"]


def test_metrics_refuses_mismatched_shapes(runner, corpus, tmp_path):
    from astropy.io import fits

    _, directory, _ = corpus
    small = tmp_path / "small.fits"
    fits.PrimaryHDU(data=np.zeros((32, 32), dtype=np.float32)).writeto(small, overwrite=True)
    result = runner.invoke(main, ["metrics", str(directory / "synthetic-000.fits"), str(small)])
    assert result.exit_code != 0
    assert "shape mismatch" in result.output


def test_module_entrypoint_works_with_overrides(corpus, tmp_path):
    """``python -m astrostack.cli`` executes the module body top to bottom.

    A helper defined *below* the ``__main__`` guard exists when the console
    script imports the module, but not when it is run as ``-m`` — which is how
    the Docker image and the CI job invoke it. Regression test for exactly
    that: it fails with NameError if a helper drifts below the guard.
    """
    import subprocess
    import sys

    _, _, manifest = corpus
    out = tmp_path / "module"
    result = subprocess.run(
        [
            sys.executable, "-m", "astrostack.cli", "--log-level", "ERROR", "run",
            str(CONFIG_DIR / "classical-stack-v1.yaml"),
            "--inputs", str(manifest), "--out", str(out),
            "--set", "audit.enabled=false",
        ],
        capture_output=True, text=True, timeout=600, check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr[-2000:]
    payload = json.loads(result.stdout)
    assert payload["pipeline"] == "classical-stack-v1"
    assert (out / "coadd.fits").is_file()


def test_ops_lists_the_stage_vocabulary(runner):
    result = runner.invoke(main, ["ops"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    for op in ("io.load", "align.platesolve", "stack.optimal", "stack.drizzle", "io.write"):
        assert op in payload
        assert payload[op], f"{op} has no docstring summary"


def test_burst_sr_config_refuses_to_run_untrained(runner, corpus, tmp_path):
    """Tier B without weights must fail loudly, not emit a pretty picture."""
    _, _, manifest = corpus
    result = runner.invoke(
        main, ["run", str(CONFIG_DIR / "burst-sr-v1.yaml"), "--inputs", str(manifest), "--out", str(tmp_path / "sr")]
    )
    assert result.exit_code != 0
    message = str(result.exception) if result.exception else result.output
    # Either torch is absent (actionable install hint) or the model has no
    # weights (refusal). Both are correct outcomes; silently producing an
    # image is not.
    assert ("astrostack[torch]" in message) or ("no trained weights" in message)
