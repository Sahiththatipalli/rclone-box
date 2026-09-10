# The `box_sub_type = enterprise` visibility gap

## What the current rclone setup actually sees

The EC2 rclone config uses:

```ini
[box_src]
type = box
box_sub_type = enterprise
token = { ... service-account token ... }
```

`box_sub_type = enterprise` means rclone authenticates as a **Box service
account** — an app-owned user identity inside the enterprise. That identity
has its own personal file tree, exactly like any human Box user does, and by
default the Box permission model only lets it see:

- Files and folders it directly owns
- Folders it has been co-collaborated onto (added as an editor, viewer, etc.)
- Whatever the Enterprise app config explicitly grants it

It does **not** automatically see every user's private "All Files" root,
even though the app was created inside your enterprise. Box treats a service
account like any other user for content-visibility purposes; enterprise
membership grants API scopes (users list, groups, etc.), not read-through
access to everyone's private content.

## Symptom pattern

The setup will look completely healthy from the outside:

- `rclone lsd box_src:` returns a top-level list — usually the service
  account's own root, plus a small handful of enterprise-shared folders
  (`000 Marketing`, `Alternative Income Group`, etc.).
- `rclone sync` transfers those without error.
- Terabytes move. Nightly cron completes. Logs are green.
- The S3 bucket fills up.

But every private folder of every Box user — the bulk of the tenant — is
simply **not in what the service account can see**, so it's not in what
rclone syncs, so it's not in S3.

## How to remediate (three options)

Any of these lifts the visibility restriction. Each has trade-offs.

### 1. As-User impersonation (recommended for enterprise-wide backup)

Configure the Box Enterprise app with the **"Generate User Access Tokens"**
scope and the **"Perform Actions as Users"** application access. Then run
rclone once per user with `--box-impersonate <user_id>` (or the equivalent
config field). This makes the service account act *as* each user for the
duration of that rclone invocation, and it will then see that user's full
tree.

Trade-offs:

- Requires a Box admin to enable the scope in the Box Developer Console.
- Requires a script to enumerate users and loop over them (rclone doesn't
  do this natively).
- Doubles the operational complexity: one rclone job per user or a wrapper
  that iterates.

### 2. Co-admin collaboration on user root folders

A Box admin can add the service account as a collaborator (Viewer / Uploader
is enough for read backup) on every user's root folder. There's a Box
admin-console workflow for this called **"Content Manager: Move users'
content"** which effectively grants the service account co-ownership.

Trade-offs:

- One-shot for existing users; needs to be re-run whenever a user is added.
- Some organizations don't want a service account listed on every user's
  collaboration list.

### 3. Switch to a personal-admin token

Change `box_sub_type = user` and OAuth as a Box admin who has **Content
Manager** rights. That admin sees every user's content already, and rclone
sees whatever they see.

Trade-offs:

- Ties the backup to one human's credentials. When they leave, the backup
  stops.
- Not really "service account" — auditing becomes messier.

## What this repo does about it

The `rclone-box-auditor` agent detects and quantifies the gap. It doesn't
fix it. When the audit report shows a large number of `gap` findings
concentrated on user-private paths, it will include a `critical` note
finding pointing at this document as the likely root cause.

Once the gap is closed, re-run the audit — a clean report is the confirmation
signal.

## Reading list

- Box docs: [Custom App with JWT auth](https://developer.box.com/guides/authentication/jwt/) — scope model
- Box docs: [Application Access levels](https://developer.box.com/guides/authentication/app-access/)
- rclone docs: [`box` backend](https://rclone.org/box/) — `--box-impersonate`,
  `--box-box-sub-type`
