"""Config + secret resolution.

Priority:

1. If ``AWS_SECRET_ID`` is set, read that Secrets Manager entry and load the
   JSON keys into the process environment (existing env vars keep their value
   only if not overridden by the secret).
2. Otherwise, ``.env`` in the current directory is loaded via ``python-dotenv``.
3. Everything then reads from ``os.environ`` normally.

This module NEVER prompts for a key and never logs secret values.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Iterable

log = logging.getLogger(__name__)

REQUIRED_FOR_RUN: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "BOX_BACKUP_BUCKET",
    "BOX_ADMIN_URL",
)

REQUIRED_FOR_LOGIN: tuple[str, ...] = ("BOX_ADMIN_URL",)


def load() -> None:
    """Load config from AWS Secrets Manager or .env into os.environ."""
    secret_id = os.environ.get("AWS_SECRET_ID")
    if secret_id:
        _load_from_secrets_manager(secret_id)
    else:
        _load_from_dotenv()


def check(required: Iterable[str]) -> list[str]:
    """Return the list of required keys that are missing / empty."""
    return [k for k in required if not os.environ.get(k)]


def _load_from_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        log.debug("python-dotenv not installed; skipping .env load")
        return
    load_dotenv()


def _load_from_secrets_manager(secret_id: str) -> None:
    try:
        import boto3
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("boto3 is required to use AWS_SECRET_ID") from e
    region = os.environ.get("AWS_REGION", "ca-central-1")
    sm = boto3.client("secretsmanager", region_name=region)
    resp = sm.get_secret_value(SecretId=secret_id)
    raw = resp.get("SecretString")
    if not raw:
        raise RuntimeError(f"Secrets Manager entry {secret_id!r} has no SecretString.")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Secrets Manager entry {secret_id!r} must be a JSON object of key/value pairs."
        ) from e
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"Secrets Manager entry {secret_id!r} must decode to an object, got {type(payload).__name__}."
        )
    for key, value in payload.items():
        if value is None:
            continue
        # Overwrite env — SM is the authority when it's in play.
        os.environ[str(key)] = str(value)
    # NEVER log values.
    log.info("loaded %d keys from Secrets Manager entry %s", len(payload), secret_id)
