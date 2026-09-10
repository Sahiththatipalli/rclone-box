"""Findings recorder + Markdown renderer.

The agent uses ``record_finding`` to accumulate structured evidence as it works,
then ``finalize_report`` to write a human-readable summary. Findings live in an
append-only JSONL so the audit trail survives interruptions.

Categories the agent must choose from:

* ``covered``      — folder present in Box, present in S3, counts roughly match
* ``partial``      — present in both, but S3 is materially smaller
* ``gap``          — present in Box, entirely missing from S3
* ``inaccessible`` — could not enumerate in Box (admin permission error, etc.)
* ``note``         — supporting observation that isn't itself a coverage finding
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from anthropic import beta_tool

log = logging.getLogger(__name__)

VALID_CATEGORIES = {"covered", "partial", "gap", "inaccessible", "note"}
VALID_SEVERITIES = {"info", "low", "medium", "high", "critical"}


@dataclass
class RunPaths:
    run_dir: Path
    findings_path: Path
    report_path: Path


_active: RunPaths | None = None


def start_run(base_dir: str | os.PathLike[str] | None = None) -> RunPaths:
    """Initialize a new audit run. Idempotent within a process."""
    global _active
    if _active is not None:
        return _active
    base = Path(base_dir or os.environ.get("AGENT_RUN_DIR", "./runs")).expanduser()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = base / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    _active = RunPaths(
        run_dir=run_dir,
        findings_path=run_dir / "findings.jsonl",
        report_path=run_dir / "report.md",
    )
    # Touch the findings file so downstream tooling can rely on it existing.
    _active.findings_path.touch(exist_ok=True)
    log.info("audit run started at %s", run_dir)
    return _active


def current_run() -> RunPaths:
    if _active is None:
        return start_run()
    return _active


def reset_for_tests() -> None:
    """Clear the module-level run state. Test-use only."""
    global _active
    _active = None


# ---------------------------------------------------------------------------
# Tools exposed to Claude
# ---------------------------------------------------------------------------


@beta_tool
def record_finding(
    category: str,
    box_path: str,
    s3_prefix: str = "",
    severity: str = "info",
    summary: str = "",
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record one structured finding to the audit run.

    Call this every time you have a concrete conclusion about a Box path
    relative to what's in S3 — do NOT batch many folders into one call.

    Args:
        category: One of ``covered``, ``partial``, ``gap``, ``inaccessible``,
            ``note``.
        box_path: The Box path this finding is about (as shown in Admin
            Console), e.g. "Users/jdoe@ninepoint.com/2025/Deals".
        s3_prefix: The S3 prefix you compared against (relative to
            ``BOX_BACKUP_PREFIX``). Empty is only valid for ``note`` findings.
        severity: ``info`` | ``low`` | ``medium`` | ``high`` | ``critical``.
            Use ``critical`` for a whole-user or whole-tenant gap.
        summary: One sentence a human can read without any other context.
        evidence: Dict of supporting numbers/observations you already gathered
            (Box counts, S3 counts, sampled key names). Kept verbatim in the
            audit log.

    Returns:
        ``{recorded: true, id, path}`` on success.
    """
    if category not in VALID_CATEGORIES:
        return {
            "error": True,
            "message": f"category must be one of {sorted(VALID_CATEGORIES)}",
        }
    if severity not in VALID_SEVERITIES:
        return {
            "error": True,
            "message": f"severity must be one of {sorted(VALID_SEVERITIES)}",
        }
    if not box_path:
        return {"error": True, "message": "box_path is required."}
    if category != "note" and not s3_prefix:
        return {"error": True, "message": "s3_prefix is required for non-note findings."}
    if not summary:
        return {"error": True, "message": "summary is required."}

    run = current_run()
    finding = {
        "id": _next_id(run.findings_path),
        "ts": datetime.now(timezone.utc).isoformat(),
        "category": category,
        "severity": severity,
        "box_path": box_path,
        "s3_prefix": s3_prefix,
        "summary": summary,
        "evidence": evidence or {},
    }
    with run.findings_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(finding, ensure_ascii=False) + "\n")
    return {"recorded": True, "id": finding["id"], "path": str(run.findings_path)}


@beta_tool
def finalize_report(overview: str) -> dict[str, Any]:
    """Render the accumulated findings as a Markdown report.

    Call this exactly once, at the end. The ``overview`` string becomes the
    executive summary at the top of the report — write it as if a manager will
    read only that paragraph.

    Args:
        overview: 3-6 sentence executive summary of what the audit found overall.

    Returns:
        ``{written: true, path, counts}`` where ``counts`` breaks down findings
        by category.
    """
    if not overview:
        return {"error": True, "message": "overview is required."}
    run = current_run()
    findings = _load_findings(run.findings_path)
    md = _render_markdown(overview, findings)
    run.report_path.write_text(md, encoding="utf-8")
    counts: dict[str, int] = {c: 0 for c in VALID_CATEGORIES}
    for f in findings:
        counts[f["category"]] = counts.get(f["category"], 0) + 1
    return {"written": True, "path": str(run.report_path), "counts": counts}


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _next_id(findings_path: Path) -> int:
    if not findings_path.exists():
        return 1
    # Count existing lines cheaply; fine at the volumes we expect (<10k).
    with findings_path.open("rb") as f:
        return sum(1 for _ in f) + 1


def _load_findings(findings_path: Path) -> list[dict[str, Any]]:
    if not findings_path.exists():
        return []
    out: list[dict[str, Any]] = []
    with findings_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    log.warning("skipping malformed findings line: %r", line[:80])
    return out


_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
_CATEGORY_ORDER = ["gap", "inaccessible", "partial", "covered", "note"]


def _render_markdown(overview: str, findings: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {c: 0 for c in VALID_CATEGORIES}
    for f in findings:
        counts[f["category"]] = counts.get(f["category"], 0) + 1

    lines: list[str] = []
    lines.append("# Box → S3 backup audit")
    lines.append("")
    lines.append(f"_Generated: {datetime.now(timezone.utc).isoformat()}_")
    lines.append("")
    lines.append("## Overview")
    lines.append("")
    lines.append(overview.strip())
    lines.append("")
    lines.append("## Findings summary")
    lines.append("")
    lines.append("| Category | Count |")
    lines.append("| --- | --- |")
    for cat in _CATEGORY_ORDER:
        lines.append(f"| {cat} | {counts.get(cat, 0)} |")
    lines.append("")

    for cat in _CATEGORY_ORDER:
        bucket = [f for f in findings if f["category"] == cat]
        if not bucket:
            continue
        bucket.sort(key=lambda f: (_SEVERITY_ORDER.get(f["severity"], 99), f["box_path"]))
        lines.append(f"## {cat.title()} ({len(bucket)})")
        lines.append("")
        for f in bucket:
            lines.append(
                f"### `{f['box_path']}`  \n"
                f"_severity: **{f['severity']}** · id #{f['id']} · {f['ts']}_"
            )
            lines.append("")
            lines.append(f["summary"])
            lines.append("")
            if f.get("s3_prefix"):
                lines.append(f"- **S3 prefix compared:** `{f['s3_prefix']}`")
            if f.get("evidence"):
                lines.append("- **Evidence:**")
                lines.append("")
                lines.append("```json")
                lines.append(json.dumps(f["evidence"], indent=2, ensure_ascii=False))
                lines.append("```")
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "This report was produced by an automated agent. "
        "A human must review it before any remediation action is taken."
    )
    return "\n".join(lines) + "\n"


REPORT_TOOLS = [record_finding, finalize_report]
