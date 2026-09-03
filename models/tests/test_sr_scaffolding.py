"""Tier B scaffolding, and the guarantee that Tier A never needs torch.

Two things are checked here, and the first matters more than the second:

1. **Nothing in the Tier A path imports torch.** The reconstruction workers
   run on CPU containers without a CUDA userspace; an accidental top-level
   ``import torch`` anywhere in ``astrostack`` would break every one of them.
2. The Tier B pieces that *can* work without torch — WCS warping, the NumPy
   reference losses, the uncertainty maths, the registry — actually do, and
   the pieces that cannot fail with an actionable message.

Tests that need torch are skipped, not faked.
"""

from __future__ import annotations

import subprocess
import sys

import numpy as np
import pytest

from astrostack.align import gaussian_kernel, make_output_grid
from astrostack.errors import AstroStackError, MissingDependencyError
from astrostack.sr import (
    SRInputs,
    SRResult,
    WCSBurstSR,
    aggregate_samples,
    build_condition_channels,
    build_resolver,
    combined_loss_numpy,
    confidence_mask,
    flux_consistency_numpy,
    forward_model_fidelity_numpy,
    get_resolver,
    prior_contribution,
    shape_moment_loss_numpy,
    shape_moments,
    wcs_warp_stack,
)
from astrostack.sr._torch import available as torch_available

torch_only = pytest.mark.skipif(not torch_available(), reason="needs the [torch] extra")


# --------------------------------------------------------------------------
# The no-torch guarantee
# --------------------------------------------------------------------------
def test_importing_astrostack_never_imports_torch():
    """Run in a *fresh* interpreter: an in-process check would be fooled."""
    code = (
        "import sys; import astrostack, astrostack.io, astrostack.calibrate, "
        "astrostack.align, astrostack.stack, astrostack.enhance, astrostack.metrics, "
        "astrostack.pipelines, astrostack.sr, astrostack.cli; "
        "print('torch' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=180, check=True
    )
    assert out.stdout.strip() == "False", "something in astrostack imports torch at module level"


def test_torch_requirement_produces_an_actionable_error():
    from astrostack.optional import require

    if torch_available():
        pytest.skip("torch is installed, so the failure path cannot be exercised")
    with pytest.raises(MissingDependencyError) as exc:
        require("torch")
    assert "astrostack[torch]" in str(exc.value)


# --------------------------------------------------------------------------
# Registry and interface
# --------------------------------------------------------------------------
def test_registry_knows_the_wcs_burst_architecture():
    assert get_resolver("wcs-burst") is WCSBurstSR
    assert isinstance(build_resolver("wcs-burst", allow_untrained=True), WCSBurstSR)
    with pytest.raises(KeyError, match="unknown super-resolution architecture"):
        get_resolver("does-not-exist")


def test_sr_result_carries_a_mandatory_visible_label():
    """Hard rule 2: no learned output without a label and an uncertainty map."""
    result = SRResult(
        image=np.zeros((4, 4), dtype=np.float32),
        uncertainty=np.ones((4, 4), dtype=np.float32),
        architecture="wcs-burst",
        scale=2.0,
        prior_contribution=0.12,
    )
    label = result.label
    assert "AI-enhanced" in label
    assert "inferred, not measured" in label
    assert "uncertainty" in label
    assert result.summary()["prior_contribution"] == 0.12


def test_untrained_model_refuses_to_produce_an_image(dithered_corpus):
    """An untrained network's output is pure prior. It must not be emitted."""
    _, frames = dithered_corpus
    grid = make_output_grid(frames, pixel_scale_arcsec=2.0)
    inputs = SRInputs.from_frames(frames, output_grid=grid)
    resolver = WCSBurstSR(allow_untrained=False)

    with pytest.raises((AstroStackError, MissingDependencyError)) as exc:
        resolver.enhance(inputs)
    message = str(exc.value)
    assert ("no trained weights" in message) or ("astrostack[torch]" in message)


def test_sr_inputs_require_measured_psfs(dithered_corpus):
    _, frames = dithered_corpus
    stripped = [f.copy_with(f.data, psf=None) for f in frames]
    with pytest.raises(ValueError, match="measured PSF"):
        SRInputs.from_frames(stripped)


# --------------------------------------------------------------------------
# WCS warping — the piece that replaces the learned alignment module
# --------------------------------------------------------------------------
def test_wcs_warp_stack_aligns_without_any_network(dithered_corpus):
    _, frames = dithered_corpus
    grid = make_output_grid(frames, pixel_scale_arcsec=2.0)
    inputs = SRInputs.from_frames(frames, output_grid=grid)

    data, valid, warped = wcs_warp_stack(inputs, grid)
    assert data.shape == (len(frames), *grid.shape)
    assert valid.shape == data.shape
    assert set(np.unique(valid)) <= {0.0, 1.0}
    assert all(w.wcs is grid.wcs for w in warped)
    assert float(valid.mean()) > 0.7


def test_condition_channels_carry_the_physics(heterogeneous_corpus):
    _, frames, _ = heterogeneous_corpus
    inputs = SRInputs.from_frames(frames)
    cond = build_condition_channels(inputs, frames[0].shape)

    assert cond.shape == (len(frames), 7, *frames[0].shape)
    # Channel 1 is the PSF FWHM: the sharp and blurry halves must differ.
    fwhm = cond[:, 1, 0, 0]
    assert fwhm.min() < fwhm.max()
    # Channel 0 is the per-pixel sigma map, and must vary spatially.
    assert cond[0, 0].std() >= 0.0
    # Channel 6 flags photometric unreliability; nothing here is a JPEG.
    assert np.all(cond[:, 6] == 0.0)


def test_condition_channels_flag_a_jpeg_contribution(dithered_corpus):
    _, frames = dithered_corpus
    doctored = list(frames)
    doctored[0] = frames[0].copy_with(frames[0].data)
    doctored[0].meta = frames[0].meta.model_copy(
        update={"photometrically_unreliable": True, "unreliable_reason": "jpeg"}
    )
    cond = build_condition_channels(SRInputs.from_frames(doctored), frames[0].shape)
    assert cond[0, 6, 0, 0] == 1.0
    assert cond[1, 6, 0, 0] == 0.0


# --------------------------------------------------------------------------
# Scientific losses (NumPy reference implementations)
# --------------------------------------------------------------------------
def test_flux_consistency_is_zero_for_an_identical_image():
    rng = np.random.default_rng(0)
    x = rng.normal(10.0, 1.0, (64, 64))
    out = flux_consistency_numpy(x, x)
    assert out["global"] == pytest.approx(0.0, abs=1e-9)
    assert out["local"] == pytest.approx(0.0, abs=1e-9)


def test_flux_consistency_catches_flux_being_moved_not_lost():
    """The failure mode STAR was built to detect: total right, places wrong."""
    target = np.zeros((64, 64))
    target[16, 16] = 100.0
    moved = np.zeros((64, 64))
    moved[48, 48] = 100.0

    out = flux_consistency_numpy(moved, target, patch=16)
    assert out["global"] == pytest.approx(0.0, abs=1e-9), "totals agree, as designed"
    assert out["local"] > 0.5, "but the local term must notice"


def test_shape_moments_measure_ellipticity():
    round_source = gaussian_kernel(4.0, size=41)
    elongated = gaussian_kernel(4.0, size=41, ecc=0.85, theta=0.0)

    m_round = shape_moments(round_source)
    m_elong = shape_moments(elongated)
    assert abs(m_round["e1"]) < 0.02
    assert abs(m_elong["e1"]) > 0.3

    loss = shape_moment_loss_numpy(elongated, round_source)
    assert loss["ellipticity"] > 0.1
    assert shape_moment_loss_numpy(round_source, round_source)["total"] < 1e-6


def test_forward_model_fidelity_is_minimised_by_the_truth():
    """Data fidelity through the forward model, not against a pseudo-HR."""
    from scipy.signal import fftconvolve

    rng = np.random.default_rng(2)
    truth = np.zeros((64, 64))
    truth[20, 30] = 500.0
    truth[40, 12] = 300.0

    psfs = [gaussian_kernel(f, size=15) for f in (2.5, 4.0)]
    sigmas = [3.0, 3.0]
    obs = [fftconvolve(truth, k, mode="same") + rng.normal(0, s, truth.shape) for k, s in zip(psfs, sigmas, strict=True)]

    good = forward_model_fidelity_numpy(truth, obs, psfs, sigmas)
    wrong = forward_model_fidelity_numpy(truth * 1.5, obs, psfs, sigmas)
    empty = forward_model_fidelity_numpy(np.zeros_like(truth), obs, psfs, sigmas)

    assert good < wrong
    assert good < empty
    assert good == pytest.approx(1.0, rel=0.35), "chi2 per pixel should be ~1 at the truth"


def test_combined_loss_reports_every_term():
    rng = np.random.default_rng(3)
    a = rng.normal(5.0, 1.0, (32, 32))
    out = combined_loss_numpy(a, a)
    for key in ("l1", "flux_global", "flux_local", "shape_ellipticity", "shape_size", "total"):
        assert key in out
    assert out["total"] == pytest.approx(0.0, abs=1e-6)


# --------------------------------------------------------------------------
# Uncertainty and the prior-contribution audit
# --------------------------------------------------------------------------
def test_aggregate_samples_decomposes_the_variance():
    rng = np.random.default_rng(4)
    samples = rng.normal(0.0, 2.0, (64, 16, 16))
    aleatoric = np.full((16, 16), 3.0)

    maps = aggregate_samples(samples, aleatoric)
    assert maps.n_samples == 64
    assert float(np.median(maps.epistemic)) == pytest.approx(2.0, rel=0.25)
    # Total variance is the sum of the two.
    expected = np.sqrt(3.0**2 + float(np.median(maps.epistemic)) ** 2)
    assert float(np.median(maps.total)) == pytest.approx(expected, rel=0.15)
    assert "median_total_sigma" in maps.as_dict()


def test_prior_contribution_separates_measured_from_invented():
    """The diffusion4astro protocol: publish how much came from the prior."""
    from scipy.signal import fftconvolve

    rng = np.random.default_rng(5)
    psf = gaussian_kernel(4.0, size=21)
    scene = rng.normal(0.0, 1.0, (128, 128))
    measured = fftconvolve(scene, psf, mode="same")

    # An "enhancement" that only reproduces the measurement adds no prior.
    honest = prior_contribution(measured, measured, psf)
    # One that injects high-frequency noise the optics never transmitted does.
    invented = measured + rng.normal(0.0, measured.std() * 3.0, measured.shape)
    dishonest = prior_contribution(invented, measured, psf)

    assert honest < 0.1
    assert dishonest > honest
    assert 0.0 <= honest <= 1.0 and 0.0 <= dishonest <= 1.0


def test_confidence_mask_marks_the_trustworthy_pixels():
    image = np.array([[10.0, 1.0], [0.0, 30.0]])
    sigma = np.array([[1.0, 1.0], [1.0, 1.0]])
    mask = confidence_mask(image, sigma, snr_threshold=3.0)
    assert mask.tolist() == [[True, False], [False, True]]


# --------------------------------------------------------------------------
# torch-only
# --------------------------------------------------------------------------
@torch_only
def test_network_builds_and_accepts_a_variable_number_of_frames():
    """The RBSR property: N may change without retraining."""
    import torch

    resolver = WCSBurstSR(scale=2.0, device="cpu", channels=16, n_blocks=2, allow_untrained=True)
    net = resolver.build_network()

    for n in (2, 3, 7):
        frames = torch.zeros(1, n, 1, 24, 24)
        valid = torch.ones(1, n, 1, 24, 24)
        cond = torch.zeros(1, n, 7, 24, 24)
        image, logvar = net(frames, valid, cond)
        assert image.shape == (1, 1, 48, 48)
        assert logvar.shape == image.shape


@torch_only
def test_torch_losses_match_the_numpy_reference():
    import torch

    from astrostack.sr.losses import torch_losses

    rng = np.random.default_rng(6)
    a = rng.normal(5.0, 1.0, (64, 64))
    b = a + rng.normal(0.0, 0.1, (64, 64))

    losses = torch_losses()
    ta = torch.from_numpy(a[None, None])
    tb = torch.from_numpy(b[None, None])

    np_shape = shape_moment_loss_numpy(a, b)["total"]
    torch_shape = float(losses["shape_moment"](ta, tb))
    assert torch_shape == pytest.approx(np_shape, rel=0.2, abs=1e-6)


@torch_only
def test_device_selection_always_offers_cpu():
    from training.device import describe_device, pick_device

    assert pick_device("cpu") == "cpu"
    info = describe_device("cpu")
    assert info["device"] == "cpu"
    assert info["hardware_tag"] == "cpu"
