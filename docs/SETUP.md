# Setup

Runs locally on macOS, Linux, or WSL. Python 3.10+.

## 1. Install

```bash
git clone <this repo>
cd rclone-box
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
playwright install chromium
```

`playwright install chromium` downloads the browser Playwright will drive.
Skip it only if you set `PLAYWRIGHT_BROWSERS_PATH` to a pre-installed
Chromium (as the CCR remote environment does).

## 2. Configure

```bash
cp .env.example .env
```

Edit `.env` and fill in:

- `ANTHROPIC_API_KEY` — your Anthropic key. Never commit this file.
- `BOX_ADMIN_URL` — usually `https://app.box.com/master`, or your
  vanity subdomain form (`https://<tenant>.app.box.com/master`).
- `BOX_BACKUP_BUCKET` — pre-filled with `ninepoint-box-backup-prod`.
- `BOX_BACKUP_PREFIX` — pre-filled with `box-backup`.

For AWS creds, use whichever the rest of your tooling uses:

- `aws configure` writes `~/.aws/credentials`. boto3 picks it up.
- SSO: `aws sso login --profile my-profile`, then set `AWS_PROFILE=my-profile`
  in `.env`.

Verify:

```bash
rclone-box-auditor check
```

Expected output shows `"ok_for_run": true` and no missing keys.

### Using AWS Secrets Manager instead of `.env`

If you'd rather keep the Anthropic key out of local files, put the same
`ANTHROPIC_API_KEY=...` in an AWS Secrets Manager secret as a JSON object,
and set `AWS_SECRET_ID=<the-secret-arn-or-id>` in `.env`. The auditor loads
that secret into the process env at startup — nothing else changes.

## 3. Capture the Box admin session

```bash
rclone-box-auditor login
```

This opens a real Chromium window at your `BOX_ADMIN_URL`. Sign in as a
Box admin (SSO, MFA, whatever your tenant requires). When you can see the
Admin Console — users list, dashboard, whatever — come back to the terminal
and press Enter.

The tool writes `storage_state.json` next to `.env` with 0600 perms.
**Treat this file as a credential.** Anyone with it can act as your Box
admin for the lifetime of the session. It's already in `.gitignore`.

You need to redo this whenever the Box session expires (typically weeks).

## 4. Run

```bash
# Default: medium depth. Takes several minutes.
rclone-box-auditor run

# Or pick a depth explicitly:
rclone-box-auditor run --depth shallow   # ~5 users, no recursion — quick pulse
rclone-box-auditor run --depth medium    # ~10 users, one level deep — default
rclone-box-auditor run --depth deep      # ~20 users, two levels deep — follow-up
```

Depth shapes only the sampling plan the agent follows. Every run enumerates
the full user list (that step is cheap); the difference is how many users
get drilled into and how far.

The output lives under `runs/<UTC-timestamp>/`:

- `run_meta.json` — depth, timestamps, config fingerprint. Written at start.
- `findings.jsonl` — every structured finding the agent recorded, one per
  line. Auditable.
- `report.md` — human-readable summary.
- `report.json` — canonical JSON bundle (schema-versioned) ready to ship to
  CloudWatch Logs or Athena. Same content as `report.md`, ingestion-shaped.
- `screenshots/` — PNGs the agent captured as evidence for specific findings.

### Shipping to CloudWatch / Athena

`report.json` and `findings.jsonl` are both JSON-shaped for downstream use:

- **CloudWatch Logs Insights** — either upload `findings.jsonl` line-by-line
  to a log group (one finding = one log event), or push `report.json` as a
  single event per run. Every field is a top-level key so
  `fields @timestamp, category, severity, box_path` works.
- **Athena** — put `findings.jsonl` under
  `s3://…/audit-findings/dt=<YYYY-MM-DD>/` and define an external table
  using the JSONSerDe. `schema_version` is on every `report.json` so a
  future schema change won't silently break your queries.

This tool doesn't ship the report anywhere — that's a step you or ops does
after review, so nothing leaves your laptop unreviewed.

Open `report.md` and read it top to bottom before acting on anything. If
the report shows the systemic pattern described in
[`BOX_ACCESS_GAP.md`](BOX_ACCESS_GAP.md), don't rush to change the rclone
config on production — fix the Box app scopes first, then re-run the audit.

## 5. Re-render a report from old findings

If you want to regenerate `report.md` from a prior run without hitting the
API again:

```bash
rclone-box-auditor report runs/20260910T140000Z/findings.jsonl \
  --overview "Re-render of 2026-09-10 audit."
```

## Running tests

```bash
pytest
```

Browser tests aren't included in v0 — they'd need a live Box tenant.
S3 and report tests use `moto` and run offline.
