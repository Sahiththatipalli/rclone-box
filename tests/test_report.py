"""Tests for the findings recorder + report renderer."""

from __future__ import annotations

import json

from rclone_box_auditor.tools.report import (
    finalize_report,
    record_finding,
)


def _call(tool, **kwargs):
    """Invoke a @beta_tool-decorated function directly.

    The decorator wraps the function in a ``BetaFunctionTool`` that exposes
    the original callable as ``.func``. Fall back to ``__wrapped__`` and to
    the object itself for older SDK versions.
    """
    fn = getattr(tool, "func", None) or getattr(tool, "__wrapped__", tool)
    return fn(**kwargs)


def test_record_finding_rejects_bad_category():
    resp = _call(
        record_finding,
        category="oops",
        box_path="Users/alice",
        s3_prefix="Users/alice",
        summary="bad category",
    )
    assert resp["error"] is True


def test_record_finding_requires_summary():
    resp = _call(
        record_finding,
        category="covered",
        box_path="Users/alice",
        s3_prefix="Users/alice",
        summary="",
    )
    assert resp["error"] is True


def test_record_finding_writes_jsonl(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_RUN_DIR", str(tmp_path))
    resp = _call(
        record_finding,
        category="gap",
        box_path="Users/alice",
        s3_prefix="Users/alice",
        severity="high",
        summary="alice's private folder has no backup",
        evidence={"box_top_level_count": 12, "s3_top_level_count": 0},
    )
    assert resp["recorded"] is True
    assert resp["id"] == 1
    path = resp["path"]
    with open(path, encoding="utf-8") as f:
        line = f.readline().strip()
    entry = json.loads(line)
    assert entry["category"] == "gap"
    assert entry["evidence"]["box_top_level_count"] == 12


def test_finalize_report_writes_markdown_with_sections(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_RUN_DIR", str(tmp_path))
    _call(
        record_finding,
        category="gap",
        box_path="Users/alice",
        s3_prefix="Users/alice",
        severity="critical",
        summary="No backup for alice.",
    )
    _call(
        record_finding,
        category="covered",
        box_path="Marketing",
        s3_prefix="Marketing",
        summary="Marketing folder matches.",
        evidence={"box": 500, "s3": 500},
    )
    _call(
        record_finding,
        category="note",
        box_path="global",
        summary="Service account probably lacks user impersonation.",
    )
    resp = _call(finalize_report, overview="Overview text goes here.")
    assert resp["written"] is True
    md_path = resp["path"]
    with open(md_path, encoding="utf-8") as f:
        content = f.read()
    assert "# Box → S3 backup audit" in content
    assert "## Gap (1)" in content
    assert "## Covered (1)" in content
    assert "## Note (1)" in content
    # Overview text present
    assert "Overview text goes here." in content
    # Human-review disclaimer present
    assert "human must review" in content.lower()
    # Counts sanity check
    assert resp["counts"]["gap"] == 1
    assert resp["counts"]["covered"] == 1
    assert resp["counts"]["note"] == 1
