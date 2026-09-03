"""Lazy torch access and device selection.

``import astrostack`` must never pull in torch: the Tier A worker runs on CPU
containers with no CUDA userspace, and the test suite is required to pass with
the ``[torch]`` extra absent. Every torch symbol in this package is therefore
reached through this module, and every torch-defined class is built inside a
function that is only called once a Tier B model is actually instantiated.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from astrostack.errors import MissingDependencyError

__all__ = ["autocast_dtype", "available", "select_device", "torch"]


def torch() -> Any:
    """Return the ``torch`` module or raise an actionable error."""
    try:
        import torch as _torch
    except ImportError as exc:
        raise MissingDependencyError(
            "torch",
            "Tier B learned super-resolution (astrostack.sr)",
            "pip install 'astrostack[torch]'  "
            "(Intel Arc: install the XPU build from https://pytorch.org)",
        ) from exc
    return _torch


def available() -> bool:
    """True when torch can be imported. Used by ``pytest.importorskip``-style guards."""
    try:
        import torch  # noqa: F401
    except ImportError:
        return False
    return True


@lru_cache(maxsize=1)
def select_device(preferred: str | None = None) -> str:
    """Pick a compute device: explicit > XPU > CUDA > MPS > CPU.

    Intel XPU comes first because the project's local training box is an Arc
    B70. Rule 5 of the astro-ml brief still applies: the GPU accelerates,
    it does not enable. Everything must run on ``cpu``.
    """
    t = torch()
    if preferred:
        return preferred
    xpu = getattr(t, "xpu", None)
    if xpu is not None and callable(getattr(xpu, "is_available", None)) and xpu.is_available():
        return "xpu"
    if t.cuda.is_available():
        return "cuda"
    mps = getattr(getattr(t, "backends", None), "mps", None)
    if mps is not None and mps.is_available():
        return "mps"
    return "cpu"


def autocast_dtype(device: str) -> Any:
    """Half precision where it helps, float32 on CPU where it does not."""
    t = torch()
    if device in ("cuda", "xpu"):
        return t.bfloat16
    return t.float32
