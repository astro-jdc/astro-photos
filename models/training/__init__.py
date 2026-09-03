"""Tier B training harness.

Separate from the ``astrostack`` package on purpose: the reconstruction
workers must never import torch, and nothing in ``astrostack`` imports this.

Everything here writes the rows the schema expects: ``dataset_snapshots``
(which photos, with what checksum), ``training_runs`` (git sha,
hyperparameters, hardware, the **whole** log) and ``models`` (weights,
metrics, model card).
"""

from __future__ import annotations

from training.dataset import (
    BurstSample,
    DatasetSnapshot,
    PairBuilder,
    build_snapshot,
    make_pairs_from_coadd,
)
from training.device import describe_device, pick_device
from training.evaluate import evaluate_model, evaluation_report

__all__ = [
    "BurstSample",
    "DatasetSnapshot",
    "PairBuilder",
    "build_snapshot",
    "describe_device",
    "evaluate_model",
    "evaluation_report",
    "make_pairs_from_coadd",
    "pick_device",
]
