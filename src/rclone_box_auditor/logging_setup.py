"""Logging setup. Boring — but so we can grep run logs cleanly."""

from __future__ import annotations

import logging
import os


def configure(level: str | None = None) -> None:
    log_level = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    # These are chatty at DEBUG and we don't need them.
    for noisy in ("botocore", "urllib3", "httpx", "anthropic._base_client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
