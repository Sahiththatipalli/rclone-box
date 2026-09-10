"""Tests for the config/secrets loader."""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from rclone_box_auditor import config


def test_check_returns_missing_keys(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("BOX_BACKUP_BUCKET", "b")
    monkeypatch.setenv("BOX_ADMIN_URL", "u")
    missing = config.check(config.REQUIRED_FOR_RUN)
    assert missing == ["ANTHROPIC_API_KEY"]


def test_check_all_present(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-xxx")
    monkeypatch.setenv("BOX_BACKUP_BUCKET", "b")
    monkeypatch.setenv("BOX_ADMIN_URL", "u")
    assert config.check(config.REQUIRED_FOR_RUN) == []


def test_secrets_manager_populates_env(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "ca-central-1")
    monkeypatch.setenv("AWS_SECRET_ID", "rclone-box-auditor/test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with mock_aws():
        sm = boto3.client("secretsmanager", region_name="ca-central-1")
        sm.create_secret(
            Name="rclone-box-auditor/test",
            SecretString=json.dumps(
                {
                    "ANTHROPIC_API_KEY": "sk-from-sm",
                    "BOX_BACKUP_BUCKET": "bucket-from-sm",
                    "BOX_ADMIN_URL": "https://url-from-sm",
                }
            ),
        )
        config.load()
    assert config.check(config.REQUIRED_FOR_RUN) == []


def test_secrets_manager_rejects_non_object(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "ca-central-1")
    monkeypatch.setenv("AWS_SECRET_ID", "rclone-box-auditor/bad")
    with mock_aws():
        sm = boto3.client("secretsmanager", region_name="ca-central-1")
        sm.create_secret(
            Name="rclone-box-auditor/bad",
            SecretString=json.dumps(["not", "an", "object"]),
        )
        with pytest.raises(RuntimeError, match="must decode to an object"):
            config.load()
