# rclone-box-auditor

A Claude-powered agent that answers one question:

> **Is `rclone` actually backing up every user's Box content — including private folders — to our S3 bucket?**

The agent drives a real browser (Playwright) into the Box **Admin Console** to enumerate users
and their folder trees the way a human admin would, then diffs that against what's under
`s3://ninepoint-box-backup-prod/box-backup/`. It writes a structured findings report you review
before acting on anything.

## Why this exists

Our EC2 rclone setup uses `box_sub_type = enterprise`, which authenticates as a **service account**.
A Box service account only sees folders it owns or has been collaborated into — it does **not**
automatically see every user's private root folder. If that's how the tenant is configured today,
`rclone sync` will look successful (no errors, TBs transferred) while quietly missing every user's
private content. See [`docs/BOX_ACCESS_GAP.md`](docs/BOX_ACCESS_GAP.md) for the full explanation.

This agent's job is to **detect and quantify** that gap. It does not fix it — the fix is a Box
app-configuration change that a human has to make and verify.

## What's here (v0)

- A Python package (`rclone_box_auditor`) with three tools the agent uses:
  - `browser` — Playwright over Box Admin Console (headed for first login, then re-uses saved session)
  - `s3` — boto3 lookups against the backup bucket
  - `report` — append-only findings store + Markdown renderer
- A CLI (`rclone-box-auditor`) with `login`, `run`, and `report` subcommands
- Tests and docs

## Quick start (local)

```bash
# 1. install
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium

# 2. configure
cp .env.example .env
# then edit .env: set ANTHROPIC_API_KEY, BOX_ADMIN_URL, and AWS creds via `aws configure`

# 3. capture an admin session (one-time, opens a real browser window)
rclone-box-auditor login

# 4. run the agent — writes runs/<timestamp>/findings.jsonl and report.md
rclone-box-auditor run
```

See [`docs/SETUP.md`](docs/SETUP.md) for the step-by-step and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
for how the agent, tools, and Claude API fit together.

## What the agent produces

For every run under `runs/<UTC-timestamp>/`:

- `run_meta.json` — depth, timestamps, config fingerprint.
- `findings.jsonl` — every structured finding, one JSON object per line.
- `report.md` — human-readable summary.
- `report.json` — same content as `report.md`, canonical shape for
  CloudWatch Logs / Athena ingestion (`schema_version` field on every
  bundle so you can migrate later).
- `screenshots/` — evidence PNGs.

Findings are classified into:

- **covered** — present in Box, present in S3 with matching bytes/count within tolerance
- **partial** — present in both but S3 count/size is materially lower
- **gap** — present in Box, entirely missing from S3
- **inaccessible** — the agent could not enumerate this in Box (e.g., permission error in Admin Console)

Every finding cites its Box path and the S3 prefix it was compared to. A human has to look at
the report before you act on any of it (see [`docs/BOX_ACCESS_GAP.md`](docs/BOX_ACCESS_GAP.md)
for what remediation actually looks like).

## Sampling depth

Pass `--depth {shallow|medium|deep}` on `rclone-box-auditor run`:

| Depth | Users drilled into | Recursion | When to use |
| --- | --- | --- | --- |
| `shallow` | ~5 | root only | Quick pulse check |
| `medium` (default) | ~10 | +1 level | Everyday audit |
| `deep` | ~20 | +2 levels | Follow-up when a prior run turned up worrying gaps |

## Guardrails

- The agent **reads only**. No S3 writes, no Box mutations, no rclone invocations.
- Anthropic API key and Box admin session are treated as credentials. `.env`,
  `storage_state.json`, and `runs/` are all gitignored.
- Output is a report for you to review — this is not an autonomous remediation tool.
