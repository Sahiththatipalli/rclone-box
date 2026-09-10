"""Tests for the S3 tool. Uses moto's in-memory S3 mock."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from rclone_box_auditor.tools import s3 as s3_tool


def _call(tool, **kwargs):
    fn = getattr(tool, "func", None) or getattr(tool, "__wrapped__", tool)
    return fn(**kwargs)


BUCKET = "ninepoint-box-backup-prod"
PREFIX = "box-backup"


@pytest.fixture
def mock_bucket(monkeypatch):
    monkeypatch.setenv("BOX_BACKUP_BUCKET", BUCKET)
    monkeypatch.setenv("BOX_BACKUP_PREFIX", PREFIX)
    # Force the cached boto3 client to rebuild inside the moto context.
    s3_tool._client.cache_clear()
    with mock_aws():
        client = boto3.client("s3", region_name="ca-central-1")
        client.create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": "ca-central-1"},
        )
        # Seed a plausible tree:
        # box-backup/000 Marketing/logo.png
        # box-backup/000 Marketing/deck.pptx
        # box-backup/Alternative Income Group/report.xlsx
        # box-backup/Alternative Income Group/subfolder/foo.txt
        # box-backup/direct-child.txt   (a file directly under the prefix)
        for key, body in [
            ("box-backup/000 Marketing/logo.png", b"a" * 10),
            ("box-backup/000 Marketing/deck.pptx", b"b" * 100),
            ("box-backup/Alternative Income Group/report.xlsx", b"c" * 500),
            ("box-backup/Alternative Income Group/subfolder/foo.txt", b"d" * 5),
            ("box-backup/direct-child.txt", b"e" * 7),
        ]:
            client.put_object(Bucket=BUCKET, Key=key, Body=body)
        yield
    s3_tool._client.cache_clear()


def test_list_top_level_returns_folders_and_direct_children(mock_bucket):
    resp = _call(s3_tool.s3_list_top_level, sub_prefix="")
    assert set(resp["folders"]) == {"000 Marketing", "Alternative Income Group"}
    direct = {o["key"] for o in resp["objects"]}
    assert direct == {"direct-child.txt"}
    assert resp["truncated"] is False


def test_list_top_level_scoped_to_subfolder(mock_bucket):
    resp = _call(s3_tool.s3_list_top_level, sub_prefix="Alternative Income Group")
    assert resp["folders"] == ["subfolder"]
    assert {o["key"] for o in resp["objects"]} == {"report.xlsx"}


def test_count_and_size_recurses(mock_bucket):
    resp = _call(s3_tool.s3_count_and_size, sub_prefix="Alternative Income Group")
    assert resp["object_count"] == 2
    assert resp["total_bytes"] == 505
    assert resp["truncated"] is False


def test_count_and_size_requires_prefix(mock_bucket):
    resp = _call(s3_tool.s3_count_and_size, sub_prefix="")
    assert resp["error"] is True


def test_sample_keys_returns_keys_and_sizes(mock_bucket):
    resp = _call(s3_tool.s3_sample_keys, sub_prefix="000 Marketing", n=5)
    got = {k["key"]: k["size"] for k in resp["keys"]}
    assert got == {"deck.pptx": 100, "logo.png": 10}


def test_head_object_hit_and_miss(mock_bucket):
    hit = _call(s3_tool.s3_head_object, key="000 Marketing/logo.png")
    assert hit["exists"] is True
    assert hit["size"] == 10

    miss = _call(s3_tool.s3_head_object, key="does/not/exist.txt")
    assert miss["exists"] is False


def test_list_top_level_missing_bucket_env(monkeypatch):
    monkeypatch.delenv("BOX_BACKUP_BUCKET", raising=False)
    resp = _call(s3_tool.s3_list_top_level, sub_prefix="")
    assert resp["error"] is True
    assert "BOX_BACKUP_BUCKET" in resp["message"]
