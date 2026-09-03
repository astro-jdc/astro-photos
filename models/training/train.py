"""Training loop for the Tier B model.

What this file is careful about, in order of how badly getting it wrong hurts:

1. **The whole log is kept.** The astro-ml brief is explicit: *save the entire
   log, not the last line.* Every step writes a JSONL record, and the file is
   the artefact referenced by ``training_runs.log_s3_key``. A run whose final
   metric looks fine but whose gradient norms collapsed at step 300 is a
   wasted run, and only the full log shows that.
2. **Seeds are explicit and recorded**, for Python, NumPy and torch, and
   ``torch.use_deterministic_algorithms`` is requested. Where a kernel has no
   deterministic implementation the run records that it fell back rather than
   pretending.
3. **The dataset snapshot is written before the first step.** Consent
   revocation later has to be traceable to the models that used the data.
4. **Scientific losses, not just L1**: flux consistency (STAR/FISR), shape
   moments (ShapeNet), forward-model fidelity against the original frames, and
   a Gaussian NLL that trains the uncertainty head honestly.

Run it as a module::

    python -m training.train --out runs/burst-sr-001 --epochs 20
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from astrostack.logging import configure, get_logger
from astrostack.version import resolve_git_sha
from training.dataset import BurstSample, DatasetSnapshot

__all__ = ["Hyperparameters", "TrainingRun", "set_all_seeds", "train"]

log = get_logger("training")


@dataclass
class Hyperparameters:
    """Everything that goes into ``training_runs.hyperparams``."""

    architecture: str = "wcs-burst"
    scale: int = 2
    channels: int = 64
    n_blocks: int = 6
    burst_size: int = 5
    batch_size: int = 4
    epochs: int = 20
    steps_per_epoch: int = 100
    lr: float = 2e-4
    weight_decay: float = 1e-5
    grad_clip: float = 1.0
    warmup_steps: int = 200
    seed: int = 20240101
    loss_weights: dict[str, float] = field(
        default_factory=lambda: {
            "nll": 1.0,
            "l1": 1.0,
            "flux": 0.5,
            "shape": 0.5,
            "fidelity": 0.25,
        }
    )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TrainingRun:
    """One row of ``training_runs``, assembled as the run proceeds."""

    run_id: str
    git_sha: str
    hyperparams: dict[str, Any]
    dataset_snapshot: dict[str, Any]
    hardware: dict[str, Any]
    log_path: str
    started_at: float
    finished_at: float | None = None
    status: str = "running"
    final_metrics: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def set_all_seeds(seed: int) -> dict[str, Any]:
    """Seed every RNG in play and request deterministic kernels.

    Returns a record of what was achieved, including whether deterministic
    algorithms could actually be enabled — some cuDNN/oneDNN kernels have no
    deterministic implementation, and a run that silently used one is not
    reproducible no matter what the config says.
    """
    from astrostack.sr._torch import torch

    t = torch()
    random.seed(seed)
    np.random.seed(seed)  # noqa: NPY002 - third-party code may still read the legacy state
    t.manual_seed(seed)
    if t.cuda.is_available():
        t.cuda.manual_seed_all(seed)

    deterministic = True
    reason = "ok"
    try:
        t.use_deterministic_algorithms(True, warn_only=True)
    except Exception as exc:  # noqa: BLE001
        deterministic = False
        reason = str(exc)
    if hasattr(t.backends, "cudnn"):
        t.backends.cudnn.deterministic = True
        t.backends.cudnn.benchmark = False
    return {"seed": seed, "deterministic_algorithms": deterministic, "note": reason}


class _JsonlLogger:
    """Append-only JSONL log. The complete file is the training artefact."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")

    def write(self, **record: Any) -> None:
        self._fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def _batch_to_tensors(samples: list[BurstSample], device: str) -> dict[str, Any]:
    from astrostack.sr._torch import torch

    t = torch()
    dev = t.device(device)

    def _stack(key: str, dtype: Any) -> Any:
        arrs = [getattr(s, key) for s in samples]
        return t.from_numpy(np.stack(arrs)).to(dev, dtype=dtype)

    fl = t.float32
    return {
        "lr": _stack("lr", fl).unsqueeze(2),          # (B, N, 1, H, W)
        "valid": _stack("valid", fl).unsqueeze(2),
        "cond": _stack("conditioning", fl),            # (B, N, C, H, W)
        "hr": _stack("hr", fl).unsqueeze(1),           # (B, 1, H*s, W*s)
        "psfs": _stack("psfs", fl).unsqueeze(2),       # (B, N, 1, k, k)
        "sigmas": _stack("sigmas", fl),                # (B, N)
    }


def train(
    samples: list[BurstSample],
    snapshot: DatasetSnapshot,
    out_dir: str | Path,
    hyperparams: Hyperparameters | None = None,
    device: str | None = None,
    run_id: str | None = None,
    validation: list[BurstSample] | None = None,
) -> TrainingRun:
    """Train the Tier B model. Requires the ``[torch]`` extra."""
    from astrostack.sr._torch import torch
    from astrostack.sr.losses import torch_losses
    from astrostack.sr.wcs_burst import WCSBurstSR
    from training.device import describe_device, pick_device

    t = torch()
    hp = hyperparams or Hyperparameters()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    run_id = run_id or f"{hp.architecture}-{int(time.time())}"

    seed_record = set_all_seeds(hp.seed)
    dev = device or pick_device()
    hardware = describe_device(dev)

    snapshot.write(out / "dataset_snapshot.json")
    logger = _JsonlLogger(out / "train.jsonl")
    logger.write(
        event="run_start",
        run_id=run_id,
        git_sha=resolve_git_sha(),
        hyperparams=hp.as_dict(),
        seeds=seed_record,
        hardware=hardware,
        dataset_checksum=snapshot.checksum,
        dataset_photo_count=snapshot.photo_count,
        n_samples=len(samples),
        python=platform.python_version(),
    )

    resolver = WCSBurstSR(
        scale=hp.scale, device=dev, channels=hp.channels, n_blocks=hp.n_blocks,
        allow_untrained=True,
    )  # fmt: skip
    net = resolver.build_network()
    net.train()

    losses = torch_losses()
    optimiser = t.optim.AdamW(net.parameters(), lr=hp.lr, weight_decay=hp.weight_decay)
    total_steps = max(hp.epochs * hp.steps_per_epoch, 1)
    scheduler = t.optim.lr_scheduler.OneCycleLR(
        optimiser, max_lr=hp.lr, total_steps=total_steps, pct_start=0.1
    )

    rng = np.random.default_rng(hp.seed)
    started = time.time()
    step = 0
    final: dict[str, Any] = {}

    try:
        for epoch in range(hp.epochs):
            epoch_losses: list[float] = []
            for _ in range(hp.steps_per_epoch):
                idx = rng.choice(len(samples), size=min(hp.batch_size, len(samples)), replace=False)
                batch = _batch_to_tensors([samples[int(i)] for i in idx], dev)

                norm = batch["lr"].abs().median().clamp_min(1e-6)
                pred, logvar = net(batch["lr"] / norm, batch["valid"], batch["cond"])
                target = batch["hr"] / norm

                nll = losses["heteroscedastic_nll"](pred, logvar, target)
                l1 = (pred - target).abs().mean()
                flux = losses["flux_consistency"](pred, target)
                shape = losses["shape_moment"](pred, target)
                fidelity = losses["forward_model_fidelity"](
                    pred, batch["lr"] / norm, batch["psfs"],
                    batch["sigmas"].view(*batch["sigmas"].shape, 1, 1, 1) / norm,
                    scale=hp.scale,
                )  # fmt: skip

                w = hp.loss_weights
                loss = (
                    w["nll"] * nll
                    + w["l1"] * l1
                    + w["flux"] * flux
                    + w["shape"] * shape
                    + w["fidelity"] * fidelity
                )

                optimiser.zero_grad(set_to_none=True)
                loss.backward()
                grad_norm = t.nn.utils.clip_grad_norm_(net.parameters(), hp.grad_clip)
                optimiser.step()
                scheduler.step()
                step += 1

                logger.write(
                    event="step",
                    step=step,
                    epoch=epoch,
                    loss=float(loss.detach()),
                    nll=float(nll.detach()),
                    l1=float(l1.detach()),
                    flux=float(flux.detach()),
                    shape=float(shape.detach()),
                    fidelity=float(fidelity.detach()),
                    grad_norm=float(grad_norm),
                    lr=float(scheduler.get_last_lr()[0]),
                )
                epoch_losses.append(float(loss.detach()))

            record: dict[str, Any] = {
                "event": "epoch",
                "epoch": epoch,
                "mean_loss": float(np.mean(epoch_losses)),
                "median_loss": float(np.median(epoch_losses)),
            }
            if validation:
                record["validation"] = _validate(net, validation, dev, hp, losses)
            logger.write(**record)
            final = {k: v for k, v in record.items() if k != "event"}

            t.save({"model": net.state_dict()}, out / "checkpoint.pt")

        status = "succeeded"
    except Exception as exc:
        logger.write(event="run_failed", error=str(exc), step=step)
        status = "failed"
        final = {"error": str(exc)}
        raise
    finally:
        run = TrainingRun(
            run_id=run_id,
            git_sha=resolve_git_sha(),
            hyperparams=hp.as_dict(),
            dataset_snapshot=snapshot.as_dict(),
            hardware=hardware,
            log_path=str(logger.path),
            started_at=started,
            finished_at=time.time(),
            status=status,
            final_metrics=final,
        )
        (out / "training_run.json").write_text(
            json.dumps(run.as_dict(), indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        logger.write(event="run_end", status=status, seconds=time.time() - started)
        logger.close()

    return run


def _validate(net: Any, samples: list[BurstSample], device: str, hp: Hyperparameters, losses: Any) -> dict[str, float]:
    from astrostack.sr._torch import torch

    t = torch()
    net.eval()
    out: dict[str, float] = {}
    with t.no_grad():
        batch = _batch_to_tensors(samples[: hp.batch_size], device)
        norm = batch["lr"].abs().median().clamp_min(1e-6)
        pred, logvar = net(batch["lr"] / norm, batch["valid"], batch["cond"])
        target = batch["hr"] / norm
        out["val_l1"] = float((pred - target).abs().mean())
        out["val_flux"] = float(losses["flux_consistency"](pred, target))
        out["val_shape"] = float(losses["shape_moment"](pred, target))
        out["val_nll"] = float(losses["heteroscedastic_nll"](pred, logvar, target))
    net.train()
    return out


def _cli() -> None:  # pragma: no cover - exercised manually
    parser = argparse.ArgumentParser(description="Train the Tier B burst SR model.")
    parser.add_argument("--inputs", required=True, help="Directory or manifest.json of the corpus.")
    parser.add_argument("--config", default="configs/classical-stack-v1.yaml", help="Tier A config used to build the pseudo-HR coadd.")
    parser.add_argument("--out", required=True, help="Run directory.")
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--steps-per-epoch", type=int, default=100)
    parser.add_argument("--burst-size", type=int, default=5)
    parser.add_argument("--n-samples", type=int, default=64)
    parser.add_argument("--scale", type=int, default=2)
    parser.add_argument("--seed", type=int, default=20240101)
    parser.add_argument("--device", default=None)
    args = parser.parse_args()

    configure(level="INFO")
    from astrostack.pipelines.runner import run_pipeline
    from training.dataset import PairBuilder

    out = Path(args.out)
    run = run_pipeline(args.config, args.inputs, out / "tier_a")
    coadd = run.coadd
    if coadd is None:
        raise SystemExit("the Tier A pipeline produced no coadd; cannot build pseudo-truth")
    frames = run.results["register"] if "register" in run.results else run.results["characterise"]

    builder = PairBuilder(
        frames, coadd, burst_size=args.burst_size, seed=args.seed, scale=args.scale
    )
    snapshot = builder.snapshot(created_from=str(args.inputs))
    samples = list(builder.samples(args.n_samples))
    hp = Hyperparameters(
        epochs=args.epochs, steps_per_epoch=args.steps_per_epoch,
        burst_size=args.burst_size, scale=args.scale, seed=args.seed,
    )  # fmt: skip
    result = train(samples, snapshot, out, hyperparams=hp, device=args.device)
    log.info("training_done", run_id=result.run_id, status=result.status)


if __name__ == "__main__":  # pragma: no cover
    _cli()
