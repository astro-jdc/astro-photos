"""astrostack — multi-observer astronomical image reconstruction.

The package is organised along the Tier A / Tier B / Tier C roadmap fixed by
``docs/research/multi-image-astro-reconstruction.md``:

* **Tier A** (implemented): linearising ingest, calibration, plate solving,
  WCS-driven registration, drizzle and Zackay & Ofek optimal coaddition,
  bounded deconvolution, metrics with synthetic-source injection.
* **Tier B** (scaffolded): learned burst super-resolution with the alignment
  module replaced by WCS warping, scientific losses, uncertainty maps.
  Lives in :mod:`astrostack.sr` and imports ``torch`` lazily.
* **Tier C** (designed, see ``README.md``): a joint Bayesian sky model.

Importing this package must never require ``torch``, ``rawpy`` or a plate
solver binary. Optional dependencies are resolved lazily through
:mod:`astrostack.optional`.
"""

from __future__ import annotations

from astrostack.version import PIPELINE_API_VERSION, __version__

__all__ = ["PIPELINE_API_VERSION", "__version__"]
