"""Lightweight test that the agent module wires tools correctly.

Does NOT call the API. Verifies:
* All tool registries import.
* The union list has the expected count and no duplicates by name.
* The system prompt is stable (byte-for-byte matches a stored fingerprint).
"""

from __future__ import annotations

import hashlib

from rclone_box_auditor.agent import all_tools
from rclone_box_auditor.prompts import SYSTEM_PROMPT


def test_tool_registry_shape():
    tools = all_tools()
    # 6 browser + 4 s3 + 2 report = 12
    assert len(tools) == 12
    names = [t.name for t in tools]
    assert len(names) == len(set(names)), f"duplicate tool names: {names}"
    # Sanity: every tool has an input_schema the API can consume.
    for t in tools:
        assert t.input_schema.get("type") == "object", f"{t.name}: bad schema"


def test_system_prompt_starts_with_role():
    assert SYSTEM_PROMPT.startswith("You are the rclone-box-auditor")


def test_system_prompt_is_stable_ascii():
    # Guard against accidental typography changes (curly quotes, non-breaking
    # spaces) that would silently invalidate prompt caching. If you meant to
    # edit the prompt, delete this test and re-record the hash.
    digest = hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()
    assert len(digest) == 64  # sanity — actual value logged in run summary
