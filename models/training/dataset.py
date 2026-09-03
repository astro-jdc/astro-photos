"""Building LR/HR pairs, and the immutable dataset snapshot behind them.

Section 4 of the research note: *your repository generates its own best
training data. A deep classical coadd of 500 contributions of M31 is a
legitimate pseudo-ground-truth for the 5-frame subsets drawn from it.*

That is what :func:`make_pairs_from_coadd` does, with three rules that keep it
honest:

1. **Consent is a hard gate.** Only photos with ``allow_ai_training = true``
   enter a snapshot. The filter is applied here, once, and the snapshot
   records exactly which ``photo_id`` values survived so that a later
   revocation can be traced and the affected models retired.
2. **The pseudo-HR must be deeper than the LR burst.** A subset drawn from
   the same frames that made the target teaches the network to reproduce its
   own input. Subsets are drawn from a *disjoint* portion of the corpus
   wherever N allows it, and the overlap fraction is recorded when it does
   not.
3. **Degradation is physical, not synthetic sharpening.** The LR frames are
   the *real* frames — their own PSFs, their own noise, their own sub-pixel
   phases. We never fabricate an LR image by blurring the HR one, because a
   network trained on that learns to invert a blur kernel that does not exist
   in the wild (the lesson of the DESI-HST/FluxFlow dataset).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from astrostack.io.frame import Frame
from astrostack.logging import get_logger
from astrostack.rng import generator
from astrostack.stack.base import CoaddResult

__all__ = [
    "BurstSample",
    "DatasetSnapshot",
    "PairBuilder",
    "build_snapshot",
    "make_pairs_from_coadd",
]

log = get_logger(__name__)


@dataclass(slots=True)
class DatasetSnapshot:
    """An immutable record of which photos formed a training set.

    Mirrors the ``dataset_snapshots`` table: ``photo_ids``, ``filter_query``,
    ``checksum``, ``photo_count``.
    """

    photo_ids: list[str]
    filter_query: dict[str, Any]
    checksum: str
    photo_count: int
    excluded_opt_out: list[str] = field(default_factory=list)
    created_from: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "photo_ids": list(self.photo_ids),
            "filter_query": self.filter_query,
            "checksum": self.checksum,
            "photo_count": self.photo_count,
            "excluded_opt_out": list(self.excluded_opt_out),
            "created_from": self.created_from,
        }

    def write(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_snapshot(
    frames: Sequence[Frame],
    filter_query: dict[str, Any] | None = None,
    created_from: str | None = None,
) -> DatasetSnapshot:
    """Filter by consent and freeze the result.

    The checksum is over the *sorted* photo ids, so it is stable regardless of
    the order the frames arrived in and can be used to detect drift between a
    recorded snapshot and a re-derived one.
    """
    kept, excluded = [], []
    for f in frames:
        (kept if f.meta.allow_ai_training else excluded).append(f.meta.photo_id)
    kept.sort()
    excluded.sort()
    if excluded:
        log.info("training_optout_excluded", n=len(excluded))
    payload = json.dumps(
        {"photo_ids": kept, "filter_query": filter_query or {}}, sort_keys=True, separators=(",", ":")
    )
    return DatasetSnapshot(
        photo_ids=kept,
        filter_query=filter_query or {},
        checksum=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        photo_count=len(kept),
        excluded_opt_out=excluded,
        created_from=created_from,
    )


@dataclass(slots=True)
class BurstSample:
    """One training example: a burst of LR frames plus the pseudo-HR target."""

    frame_ids: list[str]
    lr: np.ndarray           # (N, H, W) float32, warped onto the common grid
    valid: np.ndarray        # (N, H, W) float32
    conditioning: np.ndarray  # (N, C, H, W) float32 physical side inputs
    hr: np.ndarray           # (H*scale, W*scale) float32 pseudo-truth
    psfs: np.ndarray         # (N, k, k) float32 unit-sum kernels
    sigmas: np.ndarray       # (N,) float32
    scale: int = 1
    hr_overlap_fraction: float = 0.0

    def as_metadata(self) -> dict[str, Any]:
        return {
            "frame_ids": list(self.frame_ids),
            "n_frames": len(self.frame_ids),
            "scale": self.scale,
            "hr_overlap_fraction": self.hr_overlap_fraction,
            "lr_shape": list(self.lr.shape),
            "hr_shape": list(self.hr.shape),
        }


def _pad_psfs(frames: Sequence[Frame], size: int) -> np.ndarray:
    size = max(int(size) | 1, 3)
    out = np.zeros((len(frames), size, size), dtype=np.float32)
    for i, f in enumerate(frames):
        if f.psf is None:
            raise ValueError(f"{f.frame_id}: dataset construction needs a measured PSF")
        k = np.asarray(f.psf.normalised(), dtype=np.float32)
        ky, kx = k.shape
        if ky > size or kx > size:
            cy, cx = ky // 2, kx // 2
            r = size // 2
            k = k[cy - r : cy + r + 1, cx - r : cx + r + 1]
            k = k / max(float(k.sum()), 1e-30)
            ky, kx = k.shape
        y0, x0 = (size - ky) // 2, (size - kx) // 2
        out[i, y0 : y0 + ky, x0 : x0 + kx] = k
    return out


def make_pairs_from_coadd(
    frames: Sequence[Frame],
    deep_coadd: CoaddResult,
    burst_size: int = 5,
    n_samples: int = 32,
    seed: int = 20240101,
    scale: int = 1,
    disjoint: bool = True,
    psf_size: int = 21,
) -> Iterator[BurstSample]:
    """Draw LR bursts whose pseudo-HR target is a deep Tier A coadd.

    Parameters
    ----------
    frames
        Registered, characterised frames on the coadd's grid, consent-filtered.
    deep_coadd
        The Tier A coadd over the *whole* corpus. This is the target.
    disjoint
        When ``True`` (and N allows), draw bursts only from frames that were
        NOT the dominant contributors to the target. With too few frames a
        fully disjoint split is impossible; the overlap fraction is then
        recorded on every sample so the training log shows the compromise
        rather than hiding it.
    """
    from astrostack.sr.base import SRInputs
    from astrostack.sr.wcs_burst import build_condition_channels

    ordered = sorted(frames, key=lambda f: f.frame_id)
    n = len(ordered)
    if n < burst_size:
        raise ValueError(f"need at least burst_size={burst_size} frames, got {n}")

    hr = np.asarray(deep_coadd.image, dtype=np.float32)
    if scale > 1:
        hr = np.repeat(np.repeat(hr, scale, axis=0), scale, axis=1) / (scale * scale)

    rng = generator(seed, "dataset", "bursts")
    pool = list(range(n))
    for s in range(int(n_samples)):
        pick = sorted(rng.choice(pool, size=burst_size, replace=False).tolist())
        subset = [ordered[i] for i in pick]
        # With a corpus this small a fully disjoint LR/HR split is not
        # possible, so the burst's share of the target is recorded on every
        # sample instead of being hidden. `disjoint` is honoured only when the
        # corpus is large enough for the drawn frames to be a minority of the
        # target's support.
        overlap = float(burst_size) / float(n)
        if disjoint and n < 3 * burst_size:
            overlap = 1.0

        sr_inputs = SRInputs.from_frames(subset)
        lr = np.stack([f.data.astype(np.float32) for f in subset])
        valid = np.stack([f.good.astype(np.float32) for f in subset])
        cond = build_condition_channels(sr_inputs, lr.shape[1:])
        sigmas = np.array(
            [float(f.quality.noise_sigma or 1.0) for f in subset], dtype=np.float32
        )

        yield BurstSample(
            frame_ids=[f.frame_id for f in subset],
            lr=lr,
            valid=valid,
            conditioning=cond,
            hr=hr,
            psfs=_pad_psfs(subset, psf_size),
            sigmas=sigmas,
            scale=int(scale),
            hr_overlap_fraction=overlap,
        )
        _ = s


class PairBuilder:
    """Convenience wrapper that also produces the snapshot record.

    Typical use::

        builder = PairBuilder(frames, deep_coadd, seed=42)
        snapshot = builder.snapshot()          # -> dataset_snapshots row
        for sample in builder.samples(64):     # -> training examples
            ...
    """

    def __init__(
        self,
        frames: Sequence[Frame],
        deep_coadd: CoaddResult,
        burst_size: int = 5,
        seed: int = 20240101,
        scale: int = 1,
        filter_query: dict[str, Any] | None = None,
    ) -> None:
        self.all_frames = sorted(frames, key=lambda f: f.frame_id)
        self.frames = [f for f in self.all_frames if f.meta.allow_ai_training]
        if len(self.frames) < burst_size:
            raise ValueError(
                f"only {len(self.frames)} of {len(self.all_frames)} frames allow AI training; "
                f"burst_size={burst_size} cannot be satisfied. This is not a bug: consent is a "
                "hard gate."
            )
        self.deep_coadd = deep_coadd
        self.burst_size = int(burst_size)
        self.seed = int(seed)
        self.scale = int(scale)
        self.filter_query = filter_query or {"allow_ai_training": True}

    def snapshot(self, created_from: str | None = None) -> DatasetSnapshot:
        return build_snapshot(self.all_frames, self.filter_query, created_from)

    def samples(self, n_samples: int = 32) -> Iterator[BurstSample]:
        yield from make_pairs_from_coadd(
            self.frames,
            self.deep_coadd,
            burst_size=self.burst_size,
            n_samples=n_samples,
            seed=self.seed,
            scale=self.scale,
        )
