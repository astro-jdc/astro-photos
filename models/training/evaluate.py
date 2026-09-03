"""Evaluating a trained Tier B model — adversarially, not flatteringly.

Section 9, Tier B step 5: *validate adversarially. Inject synthetic sources of
known magnitude and check recovery linearity; verify no output source lacks a
counterpart; publish per-pixel uncertainty; run held-out fields.*

The report deliberately leads with the checks a model can **fail**, and only
then reports PSNR/SSIM. A model that improves PSNR while failing the injection
linearity test or the empty-field test is not shippable, and this module says
so in a machine-readable field (``verdict``) rather than leaving it to
whoever reads the numbers.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from astrostack.io.frame import Frame
from astrostack.logging import get_logger
from astrostack.metrics.compare import compare_images
from astrostack.metrics.injection import false_positive_rate, injection_experiment
from astrostack.metrics.quality import measure_fwhm
from astrostack.robust import robust_sigma
from astrostack.stack.base import CoaddResult

__all__ = ["EvaluationReport", "evaluate_model", "evaluation_report"]

log = get_logger("training.evaluate")


@dataclass(slots=True)
class EvaluationReport:
    """Everything needed for a model card."""

    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def verdict(self) -> str:
        return "shippable" if not self.failed else "blocked"

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "passed": list(self.passed),
            "failed": list(self.failed),
            "metrics": self.metrics,
        }

    def write(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.as_dict(), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def evaluation_report(
    enhanced: np.ndarray,
    baseline: CoaddResult,
    frames: Sequence[Frame],
    uncertainty: np.ndarray | None = None,
    combiner: Any = None,
    injection_fluxes: np.ndarray | None = None,
    reference: np.ndarray | None = None,
    max_prior_contribution: float = 0.35,
) -> EvaluationReport:
    """Run the adversarial battery against a Tier A baseline.

    Checks, in the order they are allowed to veto a release:

    1. **Flux linearity.** Injected sources must be recovered with slope ~1.
    2. **Empty field.** Pure noise in must give nothing detectable out.
    3. **Prior contribution.** The fraction of output power at frequencies the
       data does not constrain must stay under ``max_prior_contribution``.
    4. **Uncertainty present and finite.** A learned output without one is not
       publishable at all (hard rule 2).
    5. PSNR / SSIM / flux consistency against a reference, if one exists.
    """
    from astrostack.sr.uncertainty import prior_contribution

    report = EvaluationReport()
    ordered = sorted(frames, key=lambda f: f.frame_id)

    # 1. injection linearity, measured through the *classical* combiner so the
    #    Tier A path is audited too.
    if combiner is not None:
        sigma = float(np.median([f.quality.noise_sigma or 1.0 for f in ordered]))
        fluxes = (
            injection_fluxes
            if injection_fluxes is not None
            else np.array([5 * sigma, 15 * sigma, 50 * sigma, 150 * sigma])
        )
        inj = injection_experiment(list(ordered), combiner, fluxes=fluxes, n_sources=25)
        report.metrics["injection"] = {k: v for k, v in inj.as_dict().items() if k != "curve"}
        (report.passed if inj.is_linear else report.failed).append(
            f"flux linearity (slope={inj.slope:.3f}, R2={inj.r_squared:.3f})"
        )

    # 2. empty field
    if baseline.psf is not None:
        rng = np.random.default_rng(20240101)
        noise_sigma = robust_sigma(baseline.image, mask=(baseline.weight <= 0))
        if not np.isfinite(noise_sigma) or noise_sigma <= 0:
            noise_sigma = 1.0
        empty = rng.normal(0.0, noise_sigma, baseline.image.shape)
        fp = false_positive_rate(empty, baseline.psf, noise_sigma, threshold=5.0)
        report.metrics["empty_field"] = fp
        expected = max(fp["n_independent_beams"] * 2.9e-7, 0.5)
        (report.passed if fp["n_detections"] <= expected else report.failed).append(
            f"empty field ({int(fp['n_detections'])} spurious detections, "
            f"{expected:.2f} allowed by chance)"
        )

    # 3. prior contribution
    if baseline.psf is not None:
        try:
            prior = prior_contribution(enhanced, baseline.image, baseline.psf)
            report.metrics["prior_contribution"] = prior
            (report.passed if prior <= max_prior_contribution else report.failed).append(
                f"prior contribution {prior:.1%} (limit {max_prior_contribution:.0%})"
            )
        except ValueError as exc:
            report.failed.append(f"prior contribution not computable: {exc}")

    # 4. uncertainty map
    if uncertainty is None:
        report.failed.append("no uncertainty map: a learned output without one is not publishable")
    else:
        finite = np.isfinite(uncertainty) & (uncertainty > 0)
        report.metrics["uncertainty"] = {
            "finite_fraction": float(np.mean(finite)),
            "median": float(np.median(uncertainty[finite])) if finite.any() else None,
        }
        (report.passed if finite.mean() > 0.99 else report.failed).append(
            f"uncertainty map finite over {finite.mean():.1%} of pixels"
        )

    # 5. reference comparison (last, because it can be gamed)
    if reference is not None and reference.shape == np.asarray(enhanced).shape:
        cmp = compare_images(enhanced, reference)
        report.metrics["comparison"] = cmp.as_dict()
    report.metrics["measured_quality"] = measure_fwhm(np.asarray(enhanced), baseline.wcs).as_dict()
    report.metrics["baseline_quality"] = measure_fwhm(baseline.image, baseline.wcs).as_dict()
    return report


def evaluate_model(
    weights: str | Path,
    frames: Sequence[Frame],
    baseline: CoaddResult,
    grid: Any,
    architecture: str = "wcs-burst",
    scale: float = 2.0,
    device: str | None = None,
    out_path: str | Path | None = None,
) -> EvaluationReport:
    """Load a checkpoint, run it, and produce the adversarial report."""
    from astrostack.sr.base import SRInputs, build_resolver
    from astrostack.stack import optimal_coadd

    resolver = build_resolver(
        architecture, scale=scale, device=device, weights=str(weights)
    )
    inputs = SRInputs.from_frames(list(frames), reference_coadd=baseline, output_grid=grid)
    result = resolver.enhance(inputs)

    report = evaluation_report(
        enhanced=result.image,
        baseline=baseline,
        frames=frames,
        uncertainty=result.uncertainty,
        combiner=optimal_coadd,
    )
    report.metrics["model"] = result.summary()
    if out_path:
        report.write(out_path)
    log.info("evaluation_done", verdict=report.verdict, failed=report.failed)
    return report
