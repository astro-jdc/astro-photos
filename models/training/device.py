"""Device selection: Intel XPU first, then CUDA, then MPS, then CPU.

The project's local box is an Intel Arc B70, so XPU is tried first; long runs
go to AWS Batch spot GPU. Rule 5 of the astro-ml brief still holds in both
places: **the GPU accelerates, it does not enable.** Every code path here has
a CPU fallback, and the CPU fallback is tested.
"""

from __future__ import annotations

from typing import Any

__all__ = ["describe_device", "pick_device"]


def pick_device(preferred: str | None = None) -> str:
    """Return the torch device string to use."""
    from astrostack.sr._torch import select_device

    return select_device(preferred)


def describe_device(device: str | None = None) -> dict[str, Any]:
    """Hardware description for the ``training_runs.hardware`` column."""
    import platform

    from astrostack.sr._torch import torch

    t = torch()
    dev = device or pick_device()
    info: dict[str, Any] = {
        "device": dev,
        "torch": t.__version__,
        "platform": platform.platform(),
        "cpu_count": __import__("os").cpu_count(),
    }
    if dev == "cuda" and t.cuda.is_available():
        info["name"] = t.cuda.get_device_name(0)
        info["capability"] = ".".join(map(str, t.cuda.get_device_capability(0)))
        info["hardware_tag"] = f"cuda:{info['name']}"
    elif dev == "xpu" and getattr(t, "xpu", None) is not None and t.xpu.is_available():
        try:
            props = t.xpu.get_device_properties(0)
            info["name"] = getattr(props, "name", "intel-xpu")
        except Exception:  # noqa: BLE001
            info["name"] = "intel-xpu"
        info["hardware_tag"] = "xpu-arc-b70"
    else:
        info["name"] = platform.processor() or "cpu"
        info["hardware_tag"] = "cpu"
    return info
