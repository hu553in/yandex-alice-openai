from __future__ import annotations

import logging
import sys
from typing import cast

import structlog


def configure_logging(log_level: str) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=getattr(logging, log_level))
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, log_level)),
        context_class=dict,
    )


def get_logger() -> structlog.BoundLogger:
    return cast(structlog.BoundLogger, structlog.get_logger())
