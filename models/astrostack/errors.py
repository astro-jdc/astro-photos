"""Exception hierarchy.

Errors are deliberately fine-grained: the backend turns them into
``reconstructions.error_message`` and into ``reconstruction_inputs.
rejection_reason``, so the message has to be actionable for a human.
"""

from __future__ import annotations


class AstroStackError(Exception):
    """Base class for every error raised by astrostack."""


class MissingDependencyError(AstroStackError):
    """An optional dependency (torch, a plate solver binary...) is absent."""

    def __init__(self, package: str, purpose: str, install_hint: str) -> None:
        super().__init__(
            f"{package} is required for {purpose} but is not available. "
            f"Install it with: {install_hint}"
        )
        self.package = package
        self.purpose = purpose
        self.install_hint = install_hint


class UnsupportedFormatError(AstroStackError):
    """The file extension / magic bytes are not a format we can linearise."""


class FrameRejected(AstroStackError):
    """A frame cannot take part in a reconstruction.

    ``reason`` is stored verbatim in ``reconstruction_inputs.rejection_reason``.
    """

    def __init__(self, frame_id: str, reason: str) -> None:
        super().__init__(f"frame {frame_id!r} rejected: {reason}")
        self.frame_id = frame_id
        self.reason = reason


class PlateSolveError(AstroStackError):
    """Astrometric calibration failed for a frame."""


class RegistrationError(AstroStackError):
    """Geometric registration failed or failed its cross-validation."""


class PipelineConfigError(AstroStackError):
    """The declarative pipeline YAML is malformed or inconsistent."""


class LicenseViolation(AstroStackError):
    """A frame's licence forbids its use in a derivative stack.

    The authoritative licence-combination logic lives in the backend
    (``backend/app/domain/licensing.py``); astrostack only *enforces* the two
    hard exclusions (ND, ``allow_derivatives_in_stacks=false``) so that a
    locally-run pipeline cannot silently produce an unlicensable coadd.
    """
