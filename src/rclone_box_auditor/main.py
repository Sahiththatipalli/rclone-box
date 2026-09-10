"""Command-line entry point for the auditor.

Subcommands:

* ``login``  — headed browser flow to save the Box admin session state.
* ``run``    — run the Claude agent end-to-end; writes findings + report.
* ``report`` — re-render the Markdown report from an existing findings.jsonl.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from . import config, logging_setup
from .tools.report import start_run


@click.group()
@click.option("--log-level", default=None, help="DEBUG | INFO | WARNING | ERROR")
def cli(log_level: str | None) -> None:
    """rclone-box-auditor — audit Box → S3 backup coverage."""
    logging_setup.configure(log_level)
    config.load()


@cli.command()
def login() -> None:
    """Open a headed browser, sign into Box as admin, save the session."""
    missing = config.check(config.REQUIRED_FOR_LOGIN)
    if missing:
        raise click.UsageError(
            f"Missing required env vars: {', '.join(missing)}. "
            "Copy .env.example to .env and fill them in."
        )
    # Import here so `login` doesn't fail if playwright is missing until run.
    from .tools.browser import BrowserConfig, capture_admin_session

    cfg = BrowserConfig.from_env(headless=False)
    path = capture_admin_session(cfg)
    click.echo(f"Saved Box admin session to {path}")
    click.echo("Treat this file as a credential — anyone with it can act as admin.")


@cli.command()
@click.option(
    "--depth",
    type=click.Choice(["shallow", "medium", "deep"]),
    default="medium",
    show_default=True,
    help="How aggressively to sample. shallow=quick pulse, medium=default, "
    "deep=higher confidence for follow-up runs.",
)
def run(depth: str) -> None:
    """Run the Claude agent audit end-to-end."""
    missing = config.check(config.REQUIRED_FOR_RUN)
    if missing:
        raise click.UsageError(
            f"Missing required env vars: {', '.join(missing)}. "
            "Copy .env.example to .env, or set AWS_SECRET_ID to load from Secrets Manager."
        )
    run_paths = start_run(depth=depth)
    click.echo(f"Run directory: {run_paths.run_dir} (depth={depth})")

    # Import late so failing to import anthropic doesn't break `login`.
    from .agent import run_audit
    from .tools.browser import shutdown_session

    try:
        summary = run_audit()
    finally:
        shutdown_session()

    click.echo(json.dumps(summary, indent=2))


@cli.command()
@click.argument("findings_file", type=click.Path(exists=True, path_type=Path))
@click.option("--overview", default="Report re-rendered from existing findings.")
def report(findings_file: Path, overview: str) -> None:
    """Re-render report.md AND report.json from an existing findings.jsonl.

    No LLM call. Uses the run_meta.json next to findings_file when present.
    """
    from datetime import datetime, timezone

    from .tools.report import (
        REPORT_SCHEMA_VERSION,
        VALID_CATEGORIES,
        VALID_SEVERITIES,
        _load_findings,
        _render_markdown,
    )

    findings = _load_findings(findings_file)

    counts = {c: 0 for c in VALID_CATEGORIES}
    severity_counts = {s: 0 for s in VALID_SEVERITIES}
    for f in findings:
        counts[f["category"]] = counts.get(f["category"], 0) + 1
        severity_counts[f["severity"]] = severity_counts.get(f["severity"], 0) + 1

    md_out = findings_file.parent / "report.md"
    md_out.write_text(_render_markdown(overview, findings), encoding="utf-8")

    meta_path = findings_file.parent / "run_meta.json"
    run_meta = {}
    if meta_path.exists():
        try:
            run_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    bundle = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "run_id": run_meta.get("run_id"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_meta": run_meta,
        "overview": overview,
        "counts": counts,
        "severity_counts": severity_counts,
        "findings_total": len(findings),
        "findings": findings,
    }
    json_out = findings_file.parent / "report.json"
    json_out.write_text(
        json.dumps(bundle, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    click.echo(f"Wrote {md_out} and {json_out} ({len(findings)} findings).")


@cli.command()
def check() -> None:
    """Check env config without doing anything else."""
    import os

    missing_run = config.check(config.REQUIRED_FOR_RUN)
    missing_login = config.check(config.REQUIRED_FOR_LOGIN)
    click.echo(
        json.dumps(
            {
                "ok_for_run": not missing_run,
                "ok_for_login": not missing_login,
                "missing_for_run": missing_run,
                "missing_for_login": missing_login,
                "run_dir_setting": os.environ.get("AGENT_RUN_DIR", "./runs"),
            },
            indent=2,
        )
    )


def main() -> None:  # pragma: no cover
    cli()


if __name__ == "__main__":  # pragma: no cover
    main()
    sys.exit(0)
