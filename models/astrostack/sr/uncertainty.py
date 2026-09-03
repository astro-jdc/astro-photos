"""Per-pixel uncertainty, and how much of the output came from the prior.

Hard rule 2 of ``CLAUDE.md``: *nothing generated without a label. In astronomy
a hallucinated source is a false discovery, not an aesthetic defect. Every
output of a learned model carries an uncertainty map and a visible label.*

Three quantities, deliberately kept separate because they mean different
things:

**Aleatoric** — the noise the data itself carries. Predicted by the network's
heteroscedastic head (``log sigma**2``), trained with a Gaussian NLL.

**Epistemic** — the model's own ignorance. Estimated by sampling: MC dropout
(Gal & Ghahramani) or a deep ensemble. Large where the model is extrapolating,
which for us is exactly the "is this structure real?" question.

**Prior contribution** — following the protocol of *Bayesian Deconvolution of
Astronomical Images with Diffusion Models* (arXiv:2411.19158), which the
research note singles out for *its methodology of honesty*: measure, and
publish, how much of the output structure came from the prior rather than from
the data. :func:`prior_contribution` does that by comparing the enhanced image
against the measured Tier A coadd through the forward model — power that the
output has and the data does not constrain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from astrostack.sr._torch import torch

__all__ = [
    "UncertaintyMaps",
    "aggregate_samples",
    "confidence_mask",
    "mc_dropout_samples",
    "prior_contribution",
]


@dataclass(slots=True)
class UncertaintyMaps:
    """The three maps, plus the scalars that go into ``reconstructions.metrics``."""

    aleatoric: np.ndarray | None
    epistemic: np.ndarray | None
    total: np.ndarray
    prior_contribution: float | None = None
    n_samples: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_samples": self.n_samples,
            "prior_contribution": self.prior_contribution,
            "median_total_sigma": float(np.median(self.total)) if self.total.size else None,
            "p95_total_sigma": float(np.percentile(self.total, 95)) if self.total.size else None,
        }


def aggregate_samples(
    samples: np.ndarray,
    aleatoric: np.ndarray | None = None,
) -> UncertaintyMaps:
    """Combine ``(S, H, W)`` posterior samples into uncertainty maps.

    Total variance is aleatoric + epistemic, the standard decomposition of the
    predictive variance for a Gaussian likelihood.
    """
    s = np.asarray(samples, dtype=np.float64)
    if s.ndim != 3:
        raise ValueError(f"expected (S, H, W) samples, got shape {s.shape}")
    epistemic = s.std(axis=0)
    if aleatoric is not None:
        al = np.asarray(aleatoric, dtype=np.float64)
        total = np.sqrt(al**2 + epistemic**2)
    else:
        al = None
        total = epistemic
    return UncertaintyMaps(
        aleatoric=None if al is None else al.astype(np.float32),
        epistemic=epistemic.astype(np.float32),
        total=total.astype(np.float32),
        n_samples=int(s.shape[0]),
    )


def prior_contribution(
    enhanced: np.ndarray,
    measured: np.ndarray,
    psf: np.ndarray,
    high_frequency_only: bool = True,
) -> float:
    """Fraction of the output's power that the data does not constrain.

    The measured coadd has an effective PSF whose transfer function rolls off
    and eventually reaches zero. Anything the enhanced image contains at
    frequencies where ``|PSF_hat|`` is negligible was *not measured*; it came
    from the model's prior. This returns that power fraction, in [0, 1].

    A value near 0 means the enhancement is essentially a deconvolution inside
    the measured band. A value near 1 means the picture is mostly invention,
    and the product must say so.
    """
    e = np.asarray(enhanced, dtype=np.float64)
    m = np.asarray(measured, dtype=np.float64)
    if e.shape != m.shape:
        # Compare on the measured grid: box-average the enhancement down.
        fy = e.shape[0] // m.shape[0]
        fx = e.shape[1] // m.shape[1]
        if fy >= 1 and fx >= 1 and e.shape[0] % m.shape[0] == 0 and e.shape[1] % m.shape[1] == 0:
            e = e.reshape(m.shape[0], fy, m.shape[1], fx).mean(axis=(1, 3))
        else:
            raise ValueError(f"cannot compare shapes {e.shape} and {m.shape}")

    h, w = m.shape
    k = np.asarray(psf, dtype=np.float64)
    k = k / max(k.sum(), 1e-30)
    pad = np.zeros((h, w))
    ky, kx = k.shape
    y0, x0 = (h - ky) // 2, (w - kx) // 2
    pad[y0 : y0 + ky, x0 : x0 + kx] = k
    pad = np.roll(pad, (-(y0 + ky // 2), -(x0 + kx // 2)), axis=(0, 1))

    otf = np.abs(np.fft.rfft2(pad))
    e_hat = np.abs(np.fft.rfft2(e - e.mean())) ** 2

    if high_frequency_only:
        # "Unconstrained" = the transfer function is below 1% of its peak.
        unconstrained = otf < 0.01 * otf.max()
    else:
        unconstrained = otf < 0.5 * otf.max()

    total = float(e_hat.sum())
    if total <= 0:
        return 0.0
    return float(e_hat[unconstrained].sum() / total)


def confidence_mask(
    image: np.ndarray,
    uncertainty: np.ndarray,
    snr_threshold: float = 3.0,
) -> np.ndarray:
    """Boolean map of pixels the model is actually confident about.

    This is the "confidence overlay so users can see which structure is
    measured and which is inferred" of section 9, Tier B step 5.
    """
    img = np.asarray(image, dtype=np.float64)
    sig = np.maximum(np.asarray(uncertainty, dtype=np.float64), 1e-12)
    return (img / sig) >= float(snr_threshold)


def mc_dropout_samples(
    net: Any,
    forward: Any,
    n_samples: int = 16,
    seed: int = 0,
) -> np.ndarray:
    """Draw MC-dropout samples from a torch network.

    ``forward`` is a zero-argument callable returning a ``(H, W)`` tensor.
    Dropout layers are put back in *train* mode while every other layer stays
    in eval mode, which is what makes the samples posterior draws rather than
    a batch-norm artefact.
    """
    t = torch()
    net.eval()
    n_dropout = 0
    for module in net.modules():
        if module.__class__.__name__.startswith("Dropout"):
            module.train()
            n_dropout += 1
    if n_dropout == 0:
        raise ValueError(
            "the network has no Dropout layers, so MC dropout cannot estimate epistemic "
            "uncertainty. Use a deep ensemble (train N models with different seeds) instead."
        )
    t.manual_seed(int(seed))
    out = []
    with t.no_grad():
        for _ in range(int(n_samples)):
            out.append(np.asarray(forward().detach().cpu().numpy(), dtype=np.float32))
    return np.stack(out)
