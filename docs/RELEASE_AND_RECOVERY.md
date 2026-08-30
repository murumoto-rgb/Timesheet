# Release and recovery

## Reproducible verification

The checked-in runtime is Python 3.13.7 (`.python-version`) and Node 22 or later.
`requirements.lock` pins the Python runtime and test dependencies. Runtime-only
installs use `requirements.txt` with the lock as constraints. `package-lock.json`
pins Playwright and its matching browser installer.

From the repository root, with Python 3.13 installed:

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock -r requirements-dev.txt
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q
.venv/bin/pip-audit -r requirements.lock
npm ci
npm audit --audit-level=high
npx playwright install chromium
node --test --test-concurrency=2 tests/frontend/*.test.mjs
```

Tests use synthetic fixtures and prohibit live accounting requests. To use an
already installed browser, set `PLAYWRIGHT_CHROMIUM` to its executable path.
Otherwise the harness uses the browser installed by Playwright.
CI installs the lock, runs both suites and both vulnerability checks. A failing
check stops the workflow; tests never need production credentials.

## Deployment limits

Run **one app instance and one worker**. The operation journal uses file locks
or Supabase revision comparisons, but OAuth refresh, reminder scheduling and the
legacy audit/push blobs still require a single running process. `WEB_CONCURRENCY`
must be `1`; the app refuses another value. The launcher and Render blueprint
explicitly start one worker. Do not run a second local server connected to the
same production company/storage while the hosted server is running.

The Render blueprint pins Python, constrains dependencies, specifies one
instance with a persistent disk, and requests `autoDeployTrigger: checksPass`.
[Render documents this deployment setting](https://render.com/docs/blueprint-spec#autodeploytrigger)
and [Python version selection](https://render.com/docs/python-version).
These repository settings are **not proof** that an existing remote service has
synchronized the blueprint, branch protection is enabled, or a deployment has
passed CI. Verify those separately when publishing. Never replace the
dashboard-managed QuickBooks environment or credentials while syncing a blueprint.
Hosted/production startup requires an app password. Browser writes require
HTTPS or localhost and a browser supporting Web Locks; unsupported contexts fail
closed instead of risking simultaneous save recovery changes.

The Mac launcher preserves uncommitted files, skips updates on another branch,
and only fast-forwards `main`. It never resets or force-cleans the checkout.
If first-time setup finds an older installation, it stops without copying its
connection or starting a new empty store. The connection, journal, reminders and
audit history must be reviewed and moved together with both copies stopped;
the launcher displays a plain-language request you can give Codex for that move.
It requires Python 3.13.7 or a newer 3.13 patch and preserves an incompatible
virtual environment before rebuilding it. CI and Render use the exact tested
patch. Python dependencies are version-pinned; the lock does not include wheel
hashes. Install from the trusted package index and review dependency updates.

## Local data backup

Stop the app first so the files describe one consistent point in time. The
backup helper reads **only local files**, never QuickBooks or Supabase. Its
default names are:

- `qbo_tokens.json` — OAuth connection and credentials.
- `qbo_operations.json` — original save IDs, payloads and durable outcomes.
- `qbo_push.json` — reminder keys and subscriptions.
- `qbo_audit.json` — retained activity history.

Use the directory containing `QBO_TOKENS_FILE` as the source. For the default
Mac launcher it is `~/TimesheetApp`; for the disk-based Render blueprint it is
`/data`. Explicitly configured alternative filenames require a tailored backup.
For example, this creates a **new** backup directory and refuses to replace an
existing backup:

```bash
python3 scripts/recovery.py backup "$HOME/TimesheetApp" "$HOME/TimesheetBackups/release-2026-08-30"
```

The archive includes a SHA-256 manifest. Its directory has owner-only access;
each file is created with mode `0600`. Empty sources, links and unsafe names are
rejected. A checksum detects changes, not a maliciously replaced manifest: keep
the backup in trusted, encrypted storage. Never attach it to a public issue or
commit it to Git. It contains credentials and private work descriptions.

## Restore drill and live recovery

First restore into a **new, empty offline directory**, with the app stopped:

```bash
python3 scripts/recovery.py restore "$HOME/TimesheetBackups/release-2026-08-30" "$HOME/TimesheetRestoreCheck"
```

Every source file, checksum, size and destination is validated before writing.
Restoration refuses existing files by default, including a file that appears
after validation. Each file is installed atomically. The whole collection is
**not** a multi-file transaction; an I/O failure during installation can leave a
partial destination. Keep the original archive and use a fresh destination if
the drill fails. `--overwrite` is an explicit administrative option, not the
recommended first recovery step.

Do not point a running production app at a restored folder. An old journal may
omit saves that reached QuickBooks after the backup, and old OAuth tokens may
no longer be usable. Preserve the current files, compare the restored journal
with current QuickBooks time and the read-only reconciliation report, and resolve
all uncertain operations before permitting writes again. Never delete pending
operations or invent replacement UUIDs to bypass a blocked save. The app fails
closed on corrupt journals and on old nonempty journals without verifiable
company binding. Unreadable token, audit and reminder files also fail closed;
the app never treats a corrupt existing file as an empty store to overwrite.

For Supabase storage, the file helper is insufficient. A consistent private
database backup must include the `qbo_tokens` table and all four blobs (tokens,
push, audit and operations), plus separately protected service configuration.
Restore and verify it in an isolated environment first. No live database restore
is performed by these scripts. Keep database backups or Render disk snapshots
in addition to the browser's separate plans-and-drafts export.

None of these are a full QuickBooks accounting backup. They do not reverse
invoices, payments or time changes already accepted by QuickBooks, replace
accounting exports, or rotate compromised credentials.
