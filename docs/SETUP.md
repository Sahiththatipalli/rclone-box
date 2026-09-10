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
rclone-box-auditor run
```

This prints the run directory and streams what Claude is doing. Expect it to
take several minutes on a first run — it enumerates users, samples a
handful, and cross-checks against S3.

The output lives under `runs/<UTC-timestamp>/`:

- `findings.jsonl` — every structured finding the agent recorded, one per
  line. Auditable.
- `report.md` — human-readable summary.
- `screenshots/` — PNGs the agent captured as evidence for specific findings.

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
