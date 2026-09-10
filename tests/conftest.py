"""Shared pytest fixtures."""

from __future__ import annotations

import os

import pytest

# Neutralize any real AWS creds the developer has in their shell so moto stays
# in control during tests.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_REGION", "ca-central-1")


@pytest.fixture(autouse=True)
def _isolate_run_dir(tmp_path, monkeypatch):
    """Give every test its own AGENT_RUN_DIR so findings don't bleed across."""
    monkeypatch.setenv("AGENT_RUN_DIR", str(tmp_path / "runs"))
    from rclone_box_auditor.tools import report as report_mod

    report_mod.reset_for_tests()
    yield
    report_mod.reset_for_tests()
