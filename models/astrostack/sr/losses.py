r"""Scientific losses for Tier B.

Rule 3 of the astro-ml brief, and section 4 of the research note: *for
scientific images the loss must constrain the physically meaningful quantity
(flux, shape, position), not just L1/PSNR.* Three terms, each with a paper
behind it:

``flux_consistency``
    STAR / FISR (Wu et al., NeurIPS 2025 D&B, arXiv:2507.16385). Penalises
    disagreement in *integrated flux*, globally and per aperture. A network
    can win PSNR while quietly redistributing flux between sources; this term
    is what notices.

``shape_moment``
    ShapeNet (Nammour et al., A&A 663:A69, 2022, arXiv:2203.07412). Penalises
    error in the second-order moments — size and the two ellipticity
    components. Built for weak lensing, but the principle generalises: it
    forces the network to preserve the *shape* measurement, which is what a
    deconvolution network most easily corrupts.

``forward_model_fidelity``
    The data-fidelity term of the MAP multi-frame objective (Bhat et al.,
    ICCV 2021, arXiv:2108.08286), and the same idea as eht-imaging's RML
    ``Imager`` (section 5). Instead of comparing against a pseudo-HR target,
    push the estimate *back through each frame's forward model* — convolve
    with that frame's PSF, resample to that frame's grid, scale by its zero
    point — and compare against the **original observations**. This is the
    only term that is anchored in measurements rather than in another model's
    output, and it is what stops the network learning the Tier A coadd's own
    artefacts.

Every loss has a NumPy reference implementation that is unit-tested without
torch, plus a torch version built lazily. The two are kept in the same file on
purpose: if they ever disagree, that is a bug worth seeing.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from astrostack.sr._torch import torch

__all__ = [
    "combined_loss_numpy",
    "flux_consistency_numpy",
    "forward_model_fidelity_numpy",
    "shape_moment_loss_numpy",
    "shape_moments",
    "torch_losses",
]


# --------------------------------------------------------------------------
# NumPy reference implementations (tested, no torch required)
# --------------------------------------------------------------------------
def flux_consistency_numpy(
    pred: np.ndarray,
    target: np.ndarray,
    patch: int = 16,
    eps: float = 1e-8,
) -> dict[str, float]:
    r"""Global and patch-wise flux agreement.

    ``global`` is ``|sum(pred) - sum(target)| / (|sum(target)| + eps)``.
    ``local`` is the mean of the same quantity over non-overlapping patches,
    which is what catches flux being *moved* rather than lost.
    """
    p = np.asarray(pred, dtype=np.float64)
    t = np.asarray(target, dtype=np.float64)
    if p.shape != t.shape:
        raise ValueError(f"shape mismatch {p.shape} vs {t.shape}")

    g = abs(float(p.sum()) - float(t.sum())) / (abs(float(t.sum())) + eps)

    k = max(int(patch), 2)
    h, w = p.shape[-2:]
    ny, nx = h // k, w // k
    if ny < 1 or nx < 1:
        return {"global": g, "local": g, "total": g}
    pp = p[: ny * k, : nx * k].reshape(ny, k, nx, k).sum(axis=(1, 3))
    tt = t[: ny * k, : nx * k].reshape(ny, k, nx, k).sum(axis=(1, 3))
    local = float(np.mean(np.abs(pp - tt) / (np.abs(tt) + eps)))
    return {"global": g, "local": local, "total": g + local}


def shape_moments(image: np.ndarray, eps: float = 1e-12) -> dict[str, float]:
    r"""Unweighted second-order moments and the two ellipticity components.

    ``e1 = (Q_xx - Q_yy) / (Q_xx + Q_yy)``, ``e2 = 2 Q_xy / (Q_xx + Q_yy)``,
    and ``R2 = Q_xx + Q_yy`` (the size). These are the quantities ShapeNet
    constrains.
    """
    a = np.clip(np.asarray(image, dtype=np.float64), 0.0, None)
    total = float(a.sum())
    if total <= eps:
        return {"e1": 0.0, "e2": 0.0, "r2": 0.0, "flux": 0.0}
    h, w = a.shape[-2:]
    yy, xx = np.mgrid[0:h, 0:w]
    cy = float((a * yy).sum() / total)
    cx = float((a * xx).sum() / total)
    qxx = float((a * (xx - cx) ** 2).sum() / total)
    qyy = float((a * (yy - cy) ** 2).sum() / total)
    qxy = float((a * (xx - cx) * (yy - cy)).sum() / total)
    denom = qxx + qyy + eps
    return {
        "e1": (qxx - qyy) / denom,
        "e2": 2.0 * qxy / denom,
        "r2": qxx + qyy,
        "flux": total,
    }


def shape_moment_loss_numpy(pred: np.ndarray, target: np.ndarray, eps: float = 1e-8) -> dict[str, float]:
    """Squared error in ``(e1, e2)`` plus relative error in size."""
    mp = shape_moments(pred)
    mt = shape_moments(target)
    de1 = mp["e1"] - mt["e1"]
    de2 = mp["e2"] - mt["e2"]
    dr2 = abs(mp["r2"] - mt["r2"]) / (abs(mt["r2"]) + eps)
    return {
        "ellipticity": float(de1**2 + de2**2),
        "size": float(dr2),
        "total": float(de1**2 + de2**2 + dr2),
    }


def forward_model_fidelity_numpy(
    estimate: np.ndarray,
    observations: list[np.ndarray],
    psfs: list[np.ndarray],
    sigmas: list[float | np.ndarray],
    scale: int = 1,
    zero_points: list[float] | None = None,
) -> float:
    r"""Chi-squared of the estimate pushed *back* through each forward model.

    For each observation ``j``::

        model_j = zp_j * downsample( estimate ) ⊗ P_j
        chi2   += mean( (model_j - obs_j)**2 / sigma_j**2 )

    Downsampling is a box average, which is the flux-conserving adjoint of the
    pixel-shuffle upsampling used in the network — using anything else here
    would introduce a systematic the network would then learn to cancel.
    """
    from scipy.signal import fftconvolve

    est = np.asarray(estimate, dtype=np.float64)
    if scale > 1:
        h, w = est.shape
        hh, ww = h // scale, w // scale
        est = est[: hh * scale, : ww * scale].reshape(hh, scale, ww, scale).mean(axis=(1, 3))

    zps = zero_points or [1.0] * len(observations)
    total = 0.0
    for obs, psf, sigma, zp in zip(observations, psfs, sigmas, zps, strict=True):
        o = np.asarray(obs, dtype=np.float64)
        k = np.asarray(psf, dtype=np.float64)
        k = k / max(k.sum(), 1e-30)
        model = fftconvolve(est, k, mode="same") * float(zp)
        if model.shape != o.shape:
            raise ValueError(f"forward model produced {model.shape}, observation is {o.shape}")
        s = np.asarray(sigma, dtype=np.float64)
        s = np.maximum(s, 1e-12)
        total += float(np.mean((model - o) ** 2 / (s**2)))
    return total / max(len(observations), 1)


def combined_loss_numpy(
    pred: np.ndarray,
    target: np.ndarray,
    observations: list[np.ndarray] | None = None,
    psfs: list[np.ndarray] | None = None,
    sigmas: list[float | np.ndarray] | None = None,
    scale: int = 1,
    weights: dict[str, float] | None = None,
) -> dict[str, float]:
    """All terms plus the weighted total. Reference for the torch version."""
    w = {"l1": 1.0, "flux": 0.5, "shape": 0.5, "fidelity": 1.0}
    w.update(weights or {})

    l1 = float(np.mean(np.abs(np.asarray(pred, np.float64) - np.asarray(target, np.float64))))
    flux = flux_consistency_numpy(pred, target)
    shape = shape_moment_loss_numpy(pred, target)
    fidelity = 0.0
    if observations and psfs and sigmas:
        fidelity = forward_model_fidelity_numpy(pred, observations, psfs, sigmas, scale=scale)

    total = w["l1"] * l1 + w["flux"] * flux["total"] + w["shape"] * shape["total"] + w["fidelity"] * fidelity
    return {
        "l1": l1,
        "flux_global": flux["global"],
        "flux_local": flux["local"],
        "shape_ellipticity": shape["ellipticity"],
        "shape_size": shape["size"],
        "forward_model_chi2": fidelity,
        "total": float(total),
    }


# --------------------------------------------------------------------------
# torch implementations (built lazily; identical maths)
# --------------------------------------------------------------------------
_TORCH_CACHE: dict[str, Any] = {}


def torch_losses() -> dict[str, Any]:
    """Return the torch loss callables, defining them on first use."""
    if _TORCH_CACHE:
        return _TORCH_CACHE
    t = torch()
    fn = t.nn.functional

    def flux_consistency(pred, target, patch: int = 16, eps: float = 1e-8):
        g = (pred.sum(dim=(-2, -1)) - target.sum(dim=(-2, -1))).abs() / (
            target.sum(dim=(-2, -1)).abs() + eps
        )
        pp = fn.avg_pool2d(pred, patch) * patch * patch
        tt = fn.avg_pool2d(target, patch) * patch * patch
        local = ((pp - tt).abs() / (tt.abs() + eps)).mean(dim=(-3, -2, -1))
        return (g.squeeze() + local).mean()

    def _moments(image, eps: float = 1e-12):
        a = image.clamp_min(0.0)
        b, _, h, w = a.shape
        total = a.sum(dim=(-2, -1, -3)) + eps
        yy = t.arange(h, device=a.device, dtype=a.dtype).view(1, 1, h, 1)
        xx = t.arange(w, device=a.device, dtype=a.dtype).view(1, 1, 1, w)
        cy = (a * yy).sum(dim=(-2, -1, -3)) / total
        cx = (a * xx).sum(dim=(-2, -1, -3)) / total
        dy = yy - cy.view(b, 1, 1, 1)
        dx = xx - cx.view(b, 1, 1, 1)
        qxx = (a * dx**2).sum(dim=(-2, -1, -3)) / total
        qyy = (a * dy**2).sum(dim=(-2, -1, -3)) / total
        qxy = (a * dx * dy).sum(dim=(-2, -1, -3)) / total
        denom = qxx + qyy + eps
        return (qxx - qyy) / denom, 2 * qxy / denom, qxx + qyy

    def shape_moment(pred, target, eps: float = 1e-8):
        e1p, e2p, r2p = _moments(pred)
        e1t, e2t, r2t = _moments(target)
        return ((e1p - e1t) ** 2 + (e2p - e2t) ** 2 + (r2p - r2t).abs() / (r2t.abs() + eps)).mean()

    def forward_model_fidelity(estimate, observations, psfs, sigmas, scale: int = 1, zero_points=None):
        """``estimate``: (B,1,H,W); ``observations``/``psfs``: (B,N,1,h,w)/(B,N,1,k,k)."""
        est = estimate
        if scale > 1:
            est = fn.avg_pool2d(est, scale)
        b, n = observations.shape[0], observations.shape[1]
        total = est.new_zeros(())
        for j in range(n):
            k = psfs[:, j]
            k = k / k.sum(dim=(-2, -1), keepdim=True).clamp_min(1e-30)
            pad = k.shape[-1] // 2
            model = fn.conv2d(
                fn.pad(est, (pad, pad, pad, pad), mode="reflect"),
                k.flip(-1, -2).reshape(b, 1, k.shape[-2], k.shape[-1]),
                groups=1,
            )
            if zero_points is not None:
                model = model * zero_points[:, j].view(b, 1, 1, 1)
            sig = sigmas[:, j].clamp_min(1e-12)
            total = total + (((model - observations[:, j]) ** 2) / sig**2).mean()
        return total / max(n, 1)

    def heteroscedastic_nll(pred, logvar, target):
        """Gaussian NLL: the loss that trains the uncertainty head honestly."""
        return (0.5 * (t.exp(-logvar) * (pred - target) ** 2 + logvar)).mean()

    _TORCH_CACHE.update(
        {
            "flux_consistency": flux_consistency,
            "shape_moment": shape_moment,
            "forward_model_fidelity": forward_model_fidelity,
            "heteroscedastic_nll": heteroscedastic_nll,
        }
    )
    return _TORCH_CACHE
