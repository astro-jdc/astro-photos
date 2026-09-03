"""Version identity of the package and of the pipeline contract.

``pipeline_version`` in the ``reconstructions`` table is the **git sha** of
``models/`` that ran. ``PIPELINE_API_VERSION`` is a coarser marker that is
bumped whenever the *semantics* of a stage change in a way that would alter
byte-for-byte output for the same input and params.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

__version__ = "0.1.0"

#: Bump when stage semantics change in a way that alters output bytes.
PIPELINE_API_VERSION = "1"


@lru_cache(maxsize=1)
def git_sha(repo_root: str | None = None) -> str:
    """Return the git sha of the working tree, or ``"unknown"``.

    Never raises: reconstructions must still run inside a container that has
    no ``.git`` directory. In that case the caller is expected to inject the
    sha via ``ASTROSTACK_GIT_SHA`` (see :func:`resolve_git_sha`).
    """
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[2]
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and sha else "unknown"


def resolve_git_sha() -> str:
    """git sha from the environment if set, else from the working tree."""
    import os

    return os.environ.get("ASTROSTACK_GIT_SHA") or git_sha()
