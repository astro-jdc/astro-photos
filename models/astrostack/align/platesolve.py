"""Astrometric calibration (plate solving).

Section 9, Tier A step 2: *plate solve every image with a local astrometry.net
index set or ASTAP, seeded by the EXIF focal length. This is your entire
registration solution and it is better than any learned alignment.*

Three implementations:

``AstrometryNetSolver``
    Shells out to a **local** ``solve-field``. Never the web service: bulk
    ingest must not depend on a third-party endpoint (section 6).
``ASTAPSolver``
    Shells out to ``astap``, which is considerably faster and ships compact
    star databases.
``NoOpSolver``
    Returns the WCS the frame already carries, or synthesises a tangent-plane
    WCS from a caller-supplied centre and scale. This is what the test suite
    uses: the pipeline must be exercisable end to end without installing a
    2 GB index set.

All three share the EXIF-derived pixel-scale prior, which narrows the search
by orders of magnitude.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS

from astrostack.errors import PlateSolveError
from astrostack.io.frame import Frame
from astrostack.logging import get_logger

__all__ = [
    "ASTAPSolver",
    "AstrometryNetSolver",
    "NoOpSolver",
    "PlateSolution",
    "PlateSolver",
    "ScalePrior",
    "make_tangent_wcs",
    "scale_prior_from_frame",
]

log = get_logger(__name__)


@dataclass(slots=True)
class ScalePrior:
    """Pixel-scale bracket in arcsec/px, from EXIF optics."""

    low: float
    high: float
    nominal: float

    @classmethod
    def around(cls, nominal: float, tolerance: float = 0.25) -> ScalePrior:
        return cls(low=nominal * (1 - tolerance), high=nominal * (1 + tolerance), nominal=nominal)


def scale_prior_from_frame(frame: Frame, tolerance: float = 0.25) -> ScalePrior | None:
    """Pixel-scale prior from the frame's optics, if they are known."""
    nominal = frame.quality.pixel_scale_arcsec
    if not nominal or nominal <= 0:
        return None
    return ScalePrior.around(nominal, tolerance)


@dataclass(slots=True)
class PlateSolution:
    """The output of a solver."""

    wcs: WCS
    solver: str
    ra_deg: float
    dec_deg: float
    pixel_scale_arcsec: float
    orientation_deg: float
    parity: int
    field_radius_deg: float
    n_matched: int | None = None
    log_odds: float | None = None
    raw: dict[str, Any] | None = None


def make_tangent_wcs(
    ra_deg: float,
    dec_deg: float,
    pixel_scale_arcsec: float,
    shape: tuple[int, int],
    orientation_deg: float = 0.0,
    parity: int = -1,
) -> WCS:
    """Build a clean TAN WCS. Used by ``NoOpSolver`` and by output gridding."""
    h, w = shape
    wcs = WCS(naxis=2)
    wcs.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    wcs.wcs.crpix = [w / 2.0 + 0.5, h / 2.0 + 0.5]
    wcs.wcs.crval = [float(ra_deg), float(dec_deg)]
    scale = pixel_scale_arcsec / 3600.0
    theta = np.deg2rad(orientation_deg)
    cos_t, sin_t = np.cos(theta), np.sin(theta)
    # parity -1 = normal sky orientation (RA increases to the left)
    flip = -1.0 if parity < 0 else 1.0
    wcs.wcs.cd = np.array(
        [
            [flip * scale * cos_t, -scale * sin_t],
            [flip * scale * sin_t, scale * cos_t],
        ]
    )
    wcs.wcs.radesys = "ICRS"
    wcs.wcs.equinox = 2000.0
    wcs.pixel_shape = (w, h)
    return wcs


def describe_wcs(wcs: WCS, shape: tuple[int, int]) -> dict[str, float | int]:
    """Centre, scale, position angle, parity and field radius of a WCS."""
    from astropy.wcs.utils import proj_plane_pixel_scales

    h, w = shape
    centre = wcs.pixel_to_world(w / 2.0 - 0.5, h / 2.0 - 0.5)
    scales = proj_plane_pixel_scales(wcs) * 3600.0
    scale = float(np.mean(scales))
    cd = wcs.pixel_scale_matrix
    orientation = float(np.degrees(np.arctan2(cd[0, 1], cd[1, 1])))
    parity = -1 if np.linalg.det(cd) < 0 else 1
    corner = wcs.pixel_to_world(0.0, 0.0)
    radius = float(centre.separation(corner).deg)
    return {
        "ra_deg": float(centre.ra.deg),
        "dec_deg": float(centre.dec.deg),
        "pixel_scale_arcsec": scale,
        "orientation_deg": orientation,
        "parity": parity,
        "field_radius_deg": radius,
    }


class PlateSolver(ABC):
    """Interface every solver implements."""

    name: str = "abstract"

    @abstractmethod
    def solve(self, frame: Frame, prior: ScalePrior | None = None) -> PlateSolution:
        """Return a :class:`PlateSolution` or raise :class:`PlateSolveError`."""

    @abstractmethod
    def available(self) -> bool:
        """True when the backing binary / index files are installed."""

    def solve_frame(self, frame: Frame, tolerance: float = 0.25) -> Frame:
        """Solve and attach the WCS, returning an updated frame."""
        prior = scale_prior_from_frame(frame, tolerance)
        solution = self.solve(frame, prior)
        out = frame.copy_with(frame.data, wcs=solution.wcs)
        out.quality = frame.quality.model_copy(
            update={
                "is_plate_solved": True,
                "solver": solution.solver,
                "pixel_scale_arcsec": solution.pixel_scale_arcsec,
                "orientation_deg": solution.orientation_deg,
                "parity": solution.parity,
            }
        )
        out.extra["plate_solution"] = {
            "solver": solution.solver,
            "ra_deg": solution.ra_deg,
            "dec_deg": solution.dec_deg,
            "pixel_scale_arcsec": solution.pixel_scale_arcsec,
            "orientation_deg": solution.orientation_deg,
            "parity": solution.parity,
            "field_radius_deg": solution.field_radius_deg,
            "n_matched": solution.n_matched,
        }
        out.note("align.platesolve", f"{solution.solver}: WCS attached", flux_preserving=True)
        return out


class NoOpSolver(PlateSolver):
    """Pass-through solver for tests, replays and already-solved FITS.

    Not a stub: it produces a genuine, usable WCS. It just does not *search*.
    Given a frame that already has a WCS it returns it unchanged; otherwise it
    builds a TAN projection from the supplied ``ra_deg``/``dec_deg`` and the
    frame's EXIF pixel-scale prior, which is exactly what a synthetic-truth
    test wants.
    """

    name = "noop"

    def __init__(
        self,
        ra_deg: float | None = None,
        dec_deg: float | None = None,
        pixel_scale_arcsec: float | None = None,
        orientation_deg: float = 0.0,
        parity: int = -1,
    ) -> None:
        self.ra_deg = ra_deg
        self.dec_deg = dec_deg
        self.pixel_scale_arcsec = pixel_scale_arcsec
        self.orientation_deg = orientation_deg
        self.parity = parity

    def available(self) -> bool:
        return True

    def solve(self, frame: Frame, prior: ScalePrior | None = None) -> PlateSolution:
        if frame.wcs is not None:
            info = describe_wcs(frame.wcs, frame.shape)
            return PlateSolution(wcs=frame.wcs, solver="noop:from-frame", **info)  # type: ignore[arg-type]
        scale = self.pixel_scale_arcsec or (prior.nominal if prior else None)
        if scale is None or self.ra_deg is None or self.dec_deg is None:
            raise PlateSolveError(
                f"{frame.frame_id}: NoOpSolver needs either a WCS on the frame or "
                "ra_deg/dec_deg plus a pixel scale (from the constructor or from EXIF optics)"
            )
        wcs = make_tangent_wcs(
            self.ra_deg, self.dec_deg, scale, frame.shape, self.orientation_deg, self.parity
        )
        info = describe_wcs(wcs, frame.shape)
        return PlateSolution(wcs=wcs, solver="noop:synthesised", **info)  # type: ignore[arg-type]


class _SubprocessSolver(PlateSolver):
    """Shared plumbing for solvers that shell out to a binary."""

    binary: str = ""

    def __init__(self, binary: str | None = None, timeout_s: float = 300.0) -> None:
        self.binary_path = binary or self.binary
        self.timeout_s = timeout_s

    def available(self) -> bool:
        return shutil.which(self.binary_path) is not None

    def _write_temp_fits(self, frame: Frame, directory: Path) -> Path:
        path = directory / f"{frame.frame_id}.fits"
        hdu = fits.PrimaryHDU(data=np.asarray(frame.data, dtype=np.float32))
        hdu.writeto(path, overwrite=True)
        return path

    def _run(self, argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        log.debug("platesolve_exec", argv=argv)
        return subprocess.run(
            argv, cwd=str(cwd), capture_output=True, text=True, timeout=self.timeout_s, check=False
        )


class AstrometryNetSolver(_SubprocessSolver):
    """Local astrometry.net (``solve-field``), Lang et al. 2010, AJ 139:1782."""

    name = "astrometry.net"
    binary = "solve-field"

    def __init__(
        self,
        binary: str | None = None,
        timeout_s: float = 300.0,
        downsample: int = 2,
        depth: str = "20,40,60",
        extra_args: tuple[str, ...] = (),
    ) -> None:
        super().__init__(binary, timeout_s)
        self.downsample = downsample
        self.depth = depth
        self.extra_args = tuple(extra_args)

    def solve(self, frame: Frame, prior: ScalePrior | None = None) -> PlateSolution:
        if not self.available():
            raise PlateSolveError(
                f"{self.binary_path!r} not found on PATH. Install astrometry.net and its "
                "index files, or use ASTAPSolver / NoOpSolver."
            )
        with tempfile.TemporaryDirectory(prefix="astrostack-solve-") as tmp:
            tmpdir = Path(tmp)
            image = self._write_temp_fits(frame, tmpdir)
            argv = [
                self.binary_path,
                "--overwrite",
                "--no-plots",
                "--no-verify",
                "--crpix-center",
                "--downsample", str(self.downsample),
                "--depth", self.depth,
                "--cpulimit", str(int(self.timeout_s)),
                "--dir", str(tmpdir),
                "--new-fits", "none",
                "--solved", "none",
                "--corr", "none",
                "--rdls", "none",
                "--match", "none",
                "--index-xyls", "none",
            ]  # fmt: skip
            if prior is not None:
                argv += [
                    "--scale-units", "arcsecperpix",
                    "--scale-low", f"{prior.low:.6f}",
                    "--scale-high", f"{prior.high:.6f}",
                ]  # fmt: skip
            argv += list(self.extra_args)
            argv.append(str(image))

            proc = self._run(argv, tmpdir)
            wcs_path = tmpdir / f"{frame.frame_id}.wcs"
            if not wcs_path.exists():
                raise PlateSolveError(
                    f"{frame.frame_id}: solve-field found no solution "
                    f"(rc={proc.returncode}). tail: {proc.stdout[-400:]!r}"
                )
            header = fits.getheader(wcs_path)
            wcs = WCS(header, relax=True)

        info = describe_wcs(wcs, frame.shape)
        return PlateSolution(wcs=wcs, solver=self.name, **info)  # type: ignore[arg-type]


class ASTAPSolver(_SubprocessSolver):
    """ASTAP (https://www.hnsky.org/astap.htm), the fast amateur solver."""

    name = "astap"
    binary = "astap"

    def __init__(
        self,
        binary: str | None = None,
        timeout_s: float = 120.0,
        search_radius_deg: float = 180.0,
    ) -> None:
        super().__init__(binary, timeout_s)
        self.search_radius_deg = search_radius_deg

    def solve(self, frame: Frame, prior: ScalePrior | None = None) -> PlateSolution:
        if not self.available():
            raise PlateSolveError(
                f"{self.binary_path!r} not found on PATH. Install ASTAP with a star "
                "database (D50/D80), or use AstrometryNetSolver / NoOpSolver."
            )
        with tempfile.TemporaryDirectory(prefix="astrostack-astap-") as tmp:
            tmpdir = Path(tmp)
            image = self._write_temp_fits(frame, tmpdir)
            argv = [self.binary_path, "-f", str(image), "-r", f"{self.search_radius_deg:g}", "-wcs"]
            if prior is not None:
                fov_deg = prior.nominal * frame.shape[0] / 3600.0
                argv += ["-fov", f"{fov_deg:.6f}"]
            proc = self._run(argv, tmpdir)
            wcs_path = image.with_suffix(".wcs")
            if not wcs_path.exists():
                raise PlateSolveError(
                    f"{frame.frame_id}: astap found no solution "
                    f"(rc={proc.returncode}). tail: {proc.stdout[-400:]!r}"
                )
            # ASTAP writes a keyword=value text file in FITS card style.
            header = fits.Header.fromstring(
                "".join(f"{line:<80}" for line in wcs_path.read_text().splitlines() if line.strip())
            )
            wcs = WCS(header, relax=True)

        info = describe_wcs(wcs, frame.shape)
        return PlateSolution(wcs=wcs, solver=self.name, **info)  # type: ignore[arg-type]


def build_solver(name: str, **kwargs: Any) -> PlateSolver:
    """Factory used by the declarative pipeline config."""
    table: dict[str, type[PlateSolver]] = {
        "noop": NoOpSolver,
        "astrometry.net": AstrometryNetSolver,
        "astrometry": AstrometryNetSolver,
        "solve-field": AstrometryNetSolver,
        "astap": ASTAPSolver,
    }
    key = str(name).lower()
    if key not in table:
        raise PlateSolveError(f"unknown solver {name!r}; known: {sorted(table)}")
    return table[key](**kwargs)
