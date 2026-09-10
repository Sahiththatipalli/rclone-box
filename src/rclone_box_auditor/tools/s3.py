"""S3 read-only tools for the auditor agent.

Every function here is exposed to Claude via ``@beta_tool``. Return values are
strings or JSON-serializable dicts — the agent reads them in the tool-result
turn, so keep them compact and instructive when things go wrong.

Auth: the standard boto3 credential chain (env → shared creds → SSO → EC2 role).
Region comes from ``AWS_REGION`` or the caller's profile.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import boto3
from anthropic import beta_tool
from botocore.exceptions import BotoCoreError, ClientError

log = logging.getLogger(__name__)

# Hard caps so a single tool call can't blow up context or the AWS bill.
_MAX_LIST_ITEMS = 500
_MAX_COUNT_KEYS = 100_000  # ~equivalent to 100 list pages at 1000/page
_MAX_SAMPLE_KEYS = 25


@dataclass(frozen=True)
class S3Target:
    bucket: str
    prefix: str  # canonical form: no leading slash, trailing slash included when non-empty

    @classmethod
    def from_env(cls) -> "S3Target":
        bucket = os.environ.get("BOX_BACKUP_BUCKET")
        if not bucket:
            raise RuntimeError("BOX_BACKUP_BUCKET is not set")
        prefix = os.environ.get("BOX_BACKUP_PREFIX", "").strip("/")
        if prefix:
            prefix += "/"
        return cls(bucket=bucket, prefix=prefix)


@lru_cache(maxsize=1)
def _client():
    region = os.environ.get("AWS_REGION", "ca-central-1")
    return boto3.client("s3", region_name=region)


def _join_prefix(base: str, sub: str) -> str:
    """Combine two prefix fragments safely (no double-slashes, no leading slash)."""
    sub = sub.strip("/")
    if not sub:
        return base
    if base and not base.endswith("/"):
        base = base + "/"
    return f"{base}{sub}/"


def _target(sub_prefix: str = "") -> S3Target:
    base = S3Target.from_env()
    return S3Target(bucket=base.bucket, prefix=_join_prefix(base.prefix, sub_prefix))


def _wrap_client_error(op: str, err: Exception) -> dict[str, Any]:
    if isinstance(err, ClientError):
        code = err.response.get("Error", {}).get("Code", "Unknown")
        msg = err.response.get("Error", {}).get("Message", str(err))
        return {"error": True, "op": op, "code": code, "message": msg}
    return {"error": True, "op": op, "code": type(err).__name__, "message": str(err)}


# ---------------------------------------------------------------------------
# Tools exposed to Claude
# ---------------------------------------------------------------------------


@beta_tool
def s3_list_top_level(sub_prefix: str = "") -> dict[str, Any]:
    """List immediate subfolders under the backup prefix in S3.

    Uses S3 delimiter listing so it returns "directory-like" prefixes only,
    not every object underneath. Use this to see what top-level Box folders
    appear to have made it into the backup.

    Args:
        sub_prefix: Optional sub-path under ``BOX_BACKUP_PREFIX``. Empty means
            list the root of the backup. Example: "000 Marketing".

    Returns:
        A dict with ``bucket``, ``prefix``, ``folders`` (list of str, up to 500),
        ``objects`` (list of {key, size, last_modified} for direct children),
        and ``truncated`` (bool) — if truncated, refine ``sub_prefix``.
    """
    try:
        target = _target(sub_prefix)
    except RuntimeError as e:
        return {"error": True, "op": "list_top_level", "message": str(e)}

    try:
        paginator = _client().get_paginator("list_objects_v2")
        folders: list[str] = []
        objects: list[dict[str, Any]] = []
        truncated = False
        for page in paginator.paginate(
            Bucket=target.bucket, Prefix=target.prefix, Delimiter="/"
        ):
            for cp in page.get("CommonPrefixes") or []:
                # Strip the query prefix so the agent sees only the folder name.
                rel = cp["Prefix"][len(target.prefix) :].rstrip("/")
                folders.append(rel)
                if len(folders) >= _MAX_LIST_ITEMS:
                    truncated = True
                    break
            for obj in page.get("Contents") or []:
                rel_key = obj["Key"][len(target.prefix) :]
                if not rel_key or "/" in rel_key:
                    # Skip the prefix "directory marker" and nested keys.
                    continue
                objects.append(
                    {
                        "key": rel_key,
                        "size": obj["Size"],
                        "last_modified": obj["LastModified"].isoformat(),
                    }
                )
            if truncated:
                break
        return {
            "bucket": target.bucket,
            "prefix": target.prefix,
            "folders": folders,
            "objects": objects[:_MAX_LIST_ITEMS],
            "truncated": truncated,
        }
    except (ClientError, BotoCoreError) as e:
        return _wrap_client_error("list_top_level", e)


@beta_tool
def s3_count_and_size(sub_prefix: str) -> dict[str, Any]:
    """Count objects and total bytes under a sub-prefix (recursive).

    Warning: pages through up to ``_MAX_COUNT_KEYS`` (100k) keys per call.
    For a folder larger than that, returns a partial count with ``truncated=true``
    and asks you to narrow with a deeper ``sub_prefix``.

    Args:
        sub_prefix: Required sub-path under ``BOX_BACKUP_PREFIX``. Empty is
            rejected — use ``s3_list_top_level`` for the root.

    Returns:
        ``{bucket, prefix, object_count, total_bytes, truncated}``.
    """
    if not sub_prefix or not sub_prefix.strip("/"):
        return {
            "error": True,
            "op": "count_and_size",
            "message": "sub_prefix is required; use s3_list_top_level for the bucket root.",
        }
    try:
        target = _target(sub_prefix)
    except RuntimeError as e:
        return {"error": True, "op": "count_and_size", "message": str(e)}

    try:
        paginator = _client().get_paginator("list_objects_v2")
        count = 0
        total = 0
        truncated = False
        for page in paginator.paginate(Bucket=target.bucket, Prefix=target.prefix):
            for obj in page.get("Contents") or []:
                count += 1
                total += obj["Size"]
                if count >= _MAX_COUNT_KEYS:
                    truncated = True
                    break
            if truncated:
                break
        return {
            "bucket": target.bucket,
            "prefix": target.prefix,
            "object_count": count,
            "total_bytes": total,
            "truncated": truncated,
        }
    except (ClientError, BotoCoreError) as e:
        return _wrap_client_error("count_and_size", e)


@beta_tool
def s3_sample_keys(sub_prefix: str, n: int = 5) -> dict[str, Any]:
    """Return the first N object keys under a sub-prefix, as evidence.

    Useful to sanity-check that S3 content matches what Box shows for a folder,
    without pulling every key. Not a random sample — just the first N in listing
    order (which for S3 is lexicographic).

    Args:
        sub_prefix: Required sub-path under ``BOX_BACKUP_PREFIX``.
        n: How many keys to return (default 5, max 25).

    Returns:
        ``{bucket, prefix, keys: [{key, size, last_modified}, ...]}``.
    """
    if not sub_prefix or not sub_prefix.strip("/"):
        return {"error": True, "op": "sample_keys", "message": "sub_prefix is required."}
    n = max(1, min(int(n), _MAX_SAMPLE_KEYS))
    try:
        target = _target(sub_prefix)
    except RuntimeError as e:
        return {"error": True, "op": "sample_keys", "message": str(e)}

    try:
        resp = _client().list_objects_v2(Bucket=target.bucket, Prefix=target.prefix, MaxKeys=n)
        keys = [
            {
                "key": obj["Key"][len(target.prefix) :],
                "size": obj["Size"],
                "last_modified": obj["LastModified"].isoformat(),
            }
            for obj in (resp.get("Contents") or [])
        ]
        return {"bucket": target.bucket, "prefix": target.prefix, "keys": keys}
    except (ClientError, BotoCoreError) as e:
        return _wrap_client_error("sample_keys", e)


@beta_tool
def s3_head_object(key: str) -> dict[str, Any]:
    """Check whether a specific object exists in the backup bucket.

    Args:
        key: Path under ``BOX_BACKUP_PREFIX``. Example: "000 Marketing/logo.png".

    Returns:
        ``{exists: true, size, etag, last_modified, storage_class}`` on hit,
        ``{exists: false}`` on miss, ``{error: true, ...}`` on failure.
    """
    if not key:
        return {"error": True, "op": "head_object", "message": "key is required."}
    try:
        target = _target()
    except RuntimeError as e:
        return {"error": True, "op": "head_object", "message": str(e)}

    full_key = target.prefix + key.lstrip("/")
    try:
        resp = _client().head_object(Bucket=target.bucket, Key=full_key)
        return {
            "exists": True,
            "key": full_key,
            "size": resp["ContentLength"],
            "etag": resp.get("ETag", "").strip('"'),
            "last_modified": resp["LastModified"].isoformat(),
            "storage_class": resp.get("StorageClass", "STANDARD"),
        }
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return {"exists": False, "key": full_key}
        return _wrap_client_error("head_object", e)
    except BotoCoreError as e:
        return _wrap_client_error("head_object", e)


# Registry the agent module imports.
S3_TOOLS = [s3_list_top_level, s3_count_and_size, s3_sample_keys, s3_head_object]
