"""Deterministic randomness.

Hard rule 3 of ``CLAUDE.md``: same input + pipeline_version + params -> same
output, bit for bit. That forbids the global ``random`` / ``np.random`` state
entirely. Every stochastic operation takes an explicit
:class:`numpy.random.Generator` derived from the run seed and the *name of the
stage*, so that adding a stage never shifts the stream consumed by another.
"""

from __future__ import annotations

import hashlib

import numpy as np

__all__ = ["derive_seed", "generator", "stable_order"]


def derive_seed(root_seed: int, *labels: str) -> int:
    """Derive a stable 64-bit seed from a root seed and string labels.

    Uses BLAKE2b rather than :func:`hash` because CPython string hashing is
    salted per process (``PYTHONHASHSEED``) and would break reproducibility.
    """
    h = hashlib.blake2b(digest_size=8)
    h.update(str(int(root_seed)).encode("utf-8"))
    for label in labels:
        h.update(b"\x00")
        h.update(str(label).encode("utf-8"))
    return int.from_bytes(h.digest(), "big")


def generator(root_seed: int, *labels: str) -> np.random.Generator:
    """A PCG64 generator for a named sub-stream of the run."""
    return np.random.default_rng(derive_seed(root_seed, *labels))


def stable_order(items, key):
    """Sort ``items`` by ``key`` with a total order.

    Filesystem listing order is never acceptable as input order (rule 1 of the
    astro-ml agent brief). Every collection of frames is passed through this
    before it is combined.
    """
    return sorted(items, key=lambda it: (str(key(it)),))
