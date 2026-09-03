"""structlog configuration.

Two renderers: human-readable for the CLI, JSON for AWS Batch (CloudWatch).
The log of a training run is archived whole (``training_runs.log_s3_key``), so
it must be machine-parseable.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

_CONFIGURED = False


def configure(level: str = "INFO", json_logs: bool = False) -> None:
    """Configure structlog once per process."""
    global _CONFIGURED
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=getattr(logging, level.upper(), logging.INFO),
        force=True,
    )
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    if json_logs:
        processors.append(structlog.processors.JSONRenderer(sort_keys=True))
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(sys.stderr),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


def get_logger(name: str = "astrostack") -> Any:
    """Return a bound structlog logger, configuring lazily on first use."""
    if not _CONFIGURED:
        configure()
    return structlog.get_logger(name)
