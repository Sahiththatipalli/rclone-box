"""Tests for the depth-shaped user message."""

from __future__ import annotations

import pytest

from rclone_box_auditor.prompts import DEPTH_GUIDANCE, initial_user_message


@pytest.mark.parametrize("depth", ["shallow", "medium", "deep"])
def test_depth_guidance_present(depth):
    msg = initial_user_message("/tmp/run", depth=depth)
    assert depth.upper() in msg
    # The run dir is echoed so the agent knows where to write.
    assert "/tmp/run" in msg


def test_unknown_depth_falls_back_to_medium():
    msg = initial_user_message("/tmp/run", depth="nonsense")
    # We didn't blow up, and the message carries the medium plan.
    assert DEPTH_GUIDANCE["medium"] in msg


def test_all_depths_documented():
    assert set(DEPTH_GUIDANCE) == {"shallow", "medium", "deep"}
