"""Lazy resolution of optional dependencies.

Everything in the base install list *should* be present, but a reconstruction
worker must still start (and the test suite must still run) when ``rawpy`` has
no wheel for the platform, when ``sep`` fails to build, or when ``torch`` is
deliberately absent. Every consumer therefore goes through this module and
gets a :class:`~astrostack.errors.MissingDependencyError` with an install hint
instead of a bare ``ImportError`` deep inside a stage.
"""

from __future__ import annotations

import importlib
from types import ModuleType

from astrostack.errors import MissingDependencyError

_HINTS: dict[str, tuple[str, str]] = {
    "rawpy": ("decoding camera RAW files", "pip install rawpy"),
    "astroalign": ("triangle-matching registration fallback", "pip install astroalign"),
    "torch": ("Tier B learned super-resolution", "pip install 'astrostack[torch]'"),
}


def try_import(name: str) -> ModuleType | None:
    """Import ``name``, returning ``None`` if it is not installed."""
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def require(name: str) -> ModuleType:
    """Import ``name`` or raise an actionable :class:`MissingDependencyError`."""
    mod = try_import(name)
    if mod is not None:
        return mod
    purpose, hint = _HINTS.get(name, (f"the {name} feature", f"pip install {name}"))
    raise MissingDependencyError(name, purpose, hint)


def require_sep() -> ModuleType:
    """Return the SExtractor core library.

    Upstream renamed itself twice: ``sep`` -> ``sep-pjw`` (fork, exposed as
    ``sep_pjw``) -> ``sep>=1.4`` again. Accept either import name.
    """
    for name in ("sep", "sep_pjw"):
        mod = try_import(name)
        if mod is not None:
            return mod
    raise MissingDependencyError(
        "sep",
        "fast source extraction",
        "pip install 'sep>=1.4'  (or 'sep-pjw' on older platforms)",
    )


def have(name: str) -> bool:
    """True if ``name`` is importable. Used by tests to skip cleanly."""
    if name == "sep":
        return try_import("sep") is not None or try_import("sep_pjw") is not None
    return try_import(name) is not None
