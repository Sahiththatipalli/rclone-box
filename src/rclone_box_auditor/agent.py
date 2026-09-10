"""Claude tool-runner wiring.

Design choices (all backed by the claude-api skill guidance):

* Model: ``claude-opus-5`` — most capable for reasoning through the diff.
* Adaptive thinking with ``effort=high`` — this is agentic reasoning, not chat.
* System prompt cached (``cache_control=ephemeral``) so repeated runs replay
  the prefix cheaply.
* Tool runner (``client.beta.messages.tool_runner``) so we don't hand-roll the
  loop; ``pause_turn`` handled per the guide.
* Streaming with ``get_final_message`` isn't needed here — the tool runner
  yields per-iteration messages we log as we go.
"""

from __future__ import annotations

import logging
from typing import Any

import anthropic

from .prompts import SYSTEM_PROMPT, initial_user_message
from .tools.browser import BROWSER_TOOLS
from .tools.report import REPORT_TOOLS, current_run, load_meta
from .tools.s3 import S3_TOOLS

log = logging.getLogger(__name__)

MODEL = "claude-opus-5"
MAX_TOKENS = 16_000
MAX_PAUSE_RESTARTS = 5


def all_tools() -> list[Any]:
    """The full tool surface handed to Claude, in a deterministic order.

    Order matters for prompt caching: any reshuffle invalidates the cached
    tool list. Keep this stable across releases.
    """
    return [*BROWSER_TOOLS, *S3_TOOLS, *REPORT_TOOLS]


def run_audit(client: anthropic.Anthropic | None = None) -> dict[str, Any]:
    """Run one audit end-to-end. Returns a small dict summarizing the run.

    Depth is read from the run's ``run_meta.json`` (written by ``start_run``).

    Blocks until Claude decides it's done (calls ``finalize_report`` and stops).
    """
    client = client or anthropic.Anthropic()
    run = current_run()
    tools = all_tools()
    depth = load_meta().get("depth", "medium")

    system = [
        {
            "type": "text",
            "text": SYSTEM_PROMPT,
            # Cache the frozen system prompt so replays are cheap.
            "cache_control": {"type": "ephemeral"},
        }
    ]
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": initial_user_message(str(run.run_dir), depth=depth)},
    ]

    iterations = 0
    pause_restarts = 0
    final_stop_reason: str | None = None

    while True:
        runner = client.beta.messages.tool_runner(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": "high"},
            tools=tools,
            messages=messages,
        )

        last_message = None
        for message in runner:
            iterations += 1
            last_message = message
            # Mirror history for pause_turn resume support; the runner keeps its
            # own copy internally but doesn't expose it.
            messages.append({"role": "assistant", "content": message.content})
            tool_response = runner.generate_tool_call_response()
            if tool_response is not None:
                messages.append(tool_response)
            log.info(
                "iteration=%d stop_reason=%s content_blocks=%d",
                iterations,
                message.stop_reason,
                len(message.content),
            )

        if last_message is None:
            log.warning("runner yielded no messages; giving up.")
            break

        final_stop_reason = last_message.stop_reason
        if last_message.stop_reason != "pause_turn":
            break

        pause_restarts += 1
        if pause_restarts > MAX_PAUSE_RESTARTS:
            log.warning("pause_turn restarts exhausted; ending run.")
            break
        log.info("pause_turn detected — restarting runner (%d/%d)",
                 pause_restarts, MAX_PAUSE_RESTARTS)

    return {
        "iterations": iterations,
        "pause_restarts": pause_restarts,
        "stop_reason": final_stop_reason,
        "depth": depth,
        "run_dir": str(run.run_dir),
        "report_md_path": str(run.report_path),
        "report_json_path": str(run.report_json_path),
        "findings_path": str(run.findings_path),
        "meta_path": str(run.meta_path),
    }
