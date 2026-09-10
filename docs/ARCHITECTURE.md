# Architecture

## Big picture

```
                   ┌───────────────────────────────────────────┐
                   │  rclone-box-auditor  (this repo)          │
                   │                                           │
                   │   ┌───────────────────┐                   │
                   │   │  Claude API       │                   │
                   │   │  (claude-opus-5)  │                   │
                   │   │  tool_runner loop │                   │
                   │   └────────┬──────────┘                   │
                   │            │  tool_use / tool_result      │
                   │            ▼                              │
                   │   ┌───────────────────┐                   │
                   │   │  @beta_tool fns   │                   │
                   │   └───┬────────┬──┬───┘                   │
                   │       │        │  │                       │
                   │  browser_*   s3_*  report_*               │
                   │       │        │  │                       │
                   └───────┼────────┼──┼───────────────────────┘
                           ▼        ▼  ▼
                  ┌──────────────┐ ┌──────┐ ┌──────────────┐
                  │ Playwright   │ │ boto3│ │ findings.jsonl│
                  │ → Box Admin  │ │ → S3 │ │ + report.md   │
                  │   Console    │ │ read │ │ (per-run dir) │
                  └──────────────┘ └──────┘ └──────────────┘
```

## The two sources of truth

The whole audit hinges on comparing:

1. **What Box says exists** — enumerated by driving Playwright as a Box admin
   through the Admin Console. This bypasses the service-account visibility
   problem because an admin session sees everything.
2. **What S3 has** — direct boto3 listing of `s3://ninepoint-box-backup-prod/box-backup/`.

The agent's job is to line those up and record the deltas. It does not touch
either side beyond reading.

## Why an agent, not a script

A hand-written diff script would need to know how Box structures its Admin
Console DOM, how to page through 100s of users, how to decide "the count is
close enough" vs. "materially missing", and how to write a report that flags
the underlying `box_sub_type=enterprise` root cause. All of that is
open-ended reasoning; the LLM does it better than an if/else tree.

The scripted parts (list S3, extract a folder listing from a DOM, write to
JSONL) are what the tools provide. The agent orchestrates them.

## Choice of surface: Claude API + Tool Runner

Per the Anthropic guidance in [`claude-api`](https://code.claude.com/skills):

- **Not the manual loop** — we don't need a custom transport.
- **Not the Claude Agent SDK** — that's Claude Code as a library; overkill.
- **Not Managed Agents** — we want the tools to run locally with our
  laptop-side browser and our AWS creds, not in an Anthropic-hosted sandbox.
- **Tool Runner** (`client.beta.messages.tool_runner`) is the right fit:
  automates the tool call/result loop while we define the tools.

Model: `claude-opus-5` with `thinking={"type": "adaptive"}` and
`output_config={"effort": "high"}` — this is agentic reasoning over a real
tenant, not a chat turn.

Cache: the system prompt is annotated with `cache_control: ephemeral` so a
re-run of the audit reuses the cached prefix. The volatile part (run
directory, timestamp) is in the first *user* turn.

## Tool contract

Every `@beta_tool` function follows the same shape:

- Signature declares typed args (auto-generates the JSON schema).
- Returns either a success dict or `{"error": True, "op": "...", "message": "..."}`.
- Never raises — errors are values the agent can reason about.
- Reads only. No S3 writes, no Box mutations, no rclone invocations.

Tool order in `agent.all_tools()` is stable across releases — reshuffling
would invalidate the cached tool list.

## Run lifecycle

1. `cli run` → `config.load()` → `start_run()` creates
   `runs/<UTC-timestamp>/{findings.jsonl,report.md,screenshots/}`.
2. `agent.run_audit()` opens the tool runner with the full tool surface.
3. Claude iterates: opens the Admin Console, lists users, samples folders,
   diffs against S3, records findings as it goes.
4. On `pause_turn`, the runner restarts up to `MAX_PAUSE_RESTARTS` times
   with mirrored history (per the SDK guide).
5. Claude calls `finalize_report` and stops.
6. CLI prints the run summary (paths + iteration count).

## Trust boundaries

- **Anthropic API key** — read from env / Secrets Manager. Never logged.
- **Box admin session** (`storage_state.json`) — read from disk with 0600
  perms. Anyone with this file can act as your admin.
- **AWS credentials** — default boto3 chain (SSO, `~/.aws/credentials`, env,
  EC2 role). We ask for read-only S3 permissions on the backup bucket.

The generated report is Ninepoint-internal operational output. Per the
organization instructions, a human must review it before it's acted on.
