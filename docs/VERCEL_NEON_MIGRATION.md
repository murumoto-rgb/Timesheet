# Render to Vercel + Neon: staged migration

Status: preparation only. Render remains the live authority. Do not merge this
branch, change Render's branch/environment, copy live OAuth credentials to a
running preview, or retire the Render service during preparation.

## The simplest approach

Keep the existing GitHub repository. Develop on `codex/vercel-neon-migration`
in an isolated worktree. Create a separate Vercel project with no Git deployment
connection during the trial; deploy reviewed commits manually as previews.
Render continues deploying its existing main branch. A duplicate repository
adds maintenance and is unnecessary. No repository decommissioning is needed.

Timesheet has its own Neon project, database, compute and restricted runtime
role. Ledgerline and other projects are untouched. Production and test storage
must be separate before connecting a sandbox company or testing writes.

## Sleep is required

Neon supports automatic suspension after five idle minutes, and resumes on a
new query: https://neon.com/docs/introduction/scale-to-zero

Actual new-project defaults inherited always-on compute. Preparation explicitly
sets the Timesheet endpoint and future-endpoint defaults to 300 seconds and
0.25 CU min/max. Never infer sleep from the provider's default documentation.

The app opens short-lived connections and closes them. It has no warm-up job,
keepalive query, heartbeat, continuous token refresh or in-process reminder
thread on Vercel. `/api/health` never touches the database. `/api/status` DOES
read storage; do not use it for continuous external uptime monitoring.

The daily reminder uses Vercel Cron at 22:00 and 23:00 UTC to cover Chicago's
summer/winter 17:00. Persisted claim prevents duplicate notifications. These
requests wake Neon briefly even with no users; sleep resumes afterwards. This
is intermittent compute, not an always-on requirement. Disable the schedule if
reminders are unwanted. No cron executes on a preview deployment.

Storage costs remain while compute sleeps. The existing paid Neon organization
bills actual small compute/storage usage; this is not a promise of zero cost.

Verification gate: perform a synthetic write/read, close connections, observe
endpoint `idle` after >300 seconds through the management API (without SQL),
then read again and verify persisted data, measured wake latency and `active`.
Repeat against the deployed app and confirm no unexpected recurring traffic.

## Security and safety

`DATABASE_URL` uses a direct TLS connection with certificate/hostname
verification. Direct connections are intentional: session advisory locks fence
OAuth rotation and company changes across QuickBooks HTTP requests while each
write-journal transition is committed BEFORE an external side effect. Neon
transaction-mode pooling cannot preserve a session advisory lock. Connections
are short-lived, reentrant within a request and always closed; never keep a
session alive to prevent Neon sleep.

All six stored blobs (tokens, push data, audit, operations, OAuth state, login
failures) are encrypted with Fernet before entering Postgres. The separate
`TIMESHEET_ENCRYPTION_KEY` is a server-side secret. Preserve that key in an
independent private backup; losing it makes database ciphertext unrecoverable.
Use a dedicated runtime role with access only to the Timesheet tables; migrations
use the owner separately. Never place credentials in frontend code, Git, logs,
PRs, screenshots, or fixture data.

The Vercel trial has `TIMESHEET_PREVIEW_READ_ONLY=1`, no QuickBooks client
credentials, no production tokens, and Vercel account authentication protection.
It cannot reconnect QuickBooks, send push notifications or write accounting data.
Preview access and app sign-in are separate protections. Production Vercel env
variables remain unset until authorized cutover. Keep the application's existing
password and TOTP protection at cutover; no paid Vercel password add-on required.

## Test gates before cutover

1. Regression suite and real Postgres concurrent journal/token/company fencing.
2. Encryption/corruption, durable uncertain saves, shared single-use OAuth state,
   shared login rate limits, reminder claims and fail-closed preview checks.
3. Vercel deployment passes; login, asset loading, Log/Week/Report/Projects/Dash
   navigation and responsive layout work. Empty isolated preview is not proof
   of production-data parity.
4. Neon suspend/resume readback through deployed requests; no warm-up traffic.
5. Separate sandbox storage and sandbox QuickBooks connection for create/edit/
   delete and retry tests, requiring sandbox authorization. Never test writes on
   real QuickBooks. No production connection is cloned into an active test host.
6. Large historical reports: verify runtime and 4.5 MB response limits; paginate
   before adoption if necessary. Match key read-only totals against source.
7. Restore encrypted data into an isolated database and verify the encryption
   key, journal identities, company binding and audit/push records together.

## Owner-approved cutover (not authorized yet)

Before choosing a maintenance window, coordinate with the active QuickBooks
migration collector: it currently reuses Timesheet's authorized connection.
Do not interrupt that collector or change its access bridge without coordination.

- Back up browser plans/drafts on every device using Tools & settings. A new
  origin does not share localStorage, installed PWA state or push permissions.
- Freeze writes and stop the old application/collectors briefly. Export one
  consistent final disk snapshot (tokens, journal, audit, push) with hashes.
  A snapshot copied while OAuth refresh/writes continue is not a cutover backup.
- Import into EMPTY production storage in one transaction. Verify hashes and
  company binding privately; refuse overwrite. Keep the original disk backup.
- Configure reviewed Vercel production environment, password/TOTP and external
  encryption-key backup; add exact Intuit callback without removing Render's.
  Decide whether to transfer existing credentials or reconnect only after old
  processes stop. Never run both hosts refreshing the same live token grant.
- Deploy the tested production commit without assigning the live domain. Verify
  protected health/DB/login and read-only parity during the maintenance window.
  Set `TIMESHEET_PRODUCTION_ACTIVATED=1` and clear the explicit preview guard
  only as part of authorized production activation. Every Vercel preview
  remains read-only regardless of that variable. Sandbox write acceptance needs
  a separate explicitly activated sandbox deployment and separate storage.
- Route the chosen live URL/domain, update access collectors, restore browser
  backups explicitly, install the new home-screen app and re-enroll push devices.
  Old-origin push subscriptions cannot silently become new-origin permissions.
- Test an ordinary user-approved live save only if separately requested. Source
  QuickBooks is otherwise read-only during migration verification.

## Rollback and retirement

Never resume Render using its stale pre-cutover token/journal files after Vercel
has refreshed credentials or accepted writes. Stop the new host, export the
latest Neon tokens/journal/audit/push together, reconcile uncertain writes, and
restore that current state privately before restarting Render. Origin/browser
backups need preservation too. This is a state-aware rollback, not a DNS-only
switch. If state transfer is uncertain, remain stopped and investigate.

Keep the old service available for recovery until actual user/device acceptance,
collector compatibility and backup restore are confirmed. Only then authorize
Render retirement, verify final recovery artifacts and remove its paid compute
and disk. Retaining a paid suspended disk can still cost money. Retain the GitHub
repository as the ongoing app source. No destructive retirement occurs in this PR.

## Prepared resources and evidence

- Neon project: `timesheet-vercel` (`gentle-resonance-75021927`), Ohio region.
  Runtime has only schema usage and table select/insert/update privileges.
- Vercel project: `timesheet-vercel-preview`, protected by Vercel authentication,
  no Git integration and no production credentials. Preview variables only.
- Preview: https://timesheet-vercel-preview-9yolad6nx-murumoto-rgbs-projects.vercel.app
  Build `2026.10.06.1`. Health reports Postgres and read-only preview; login
  succeeds against encrypted Neon storage; `/connect` is blocked with HTTP 403.
- Secrets and encryption key are private outside Git. No live connection,
  history, journal or push subscription has been copied.
- Python 3.13 family pin resolves the observed Vercel build failure with the
  original exact 3.13.7 pin; Render retains its explicit blueprint patch pin.
- Transfer helper: `python -m scripts.neon_transfer`; validate and import default
  to offline dry-run, execution requires source-stopped acknowledgment, and
  destination must be empty. Missing optional source files are reported and need
  a separate acknowledgment after verifying the source inventory; missing tokens
  fail closed. Export creates a private recovery-compatible archive.

This establishes preparation and synthetic behavior only. Sandbox accounting,
large reports, owner/device acceptance, collector coordination and final live
backup/cutover have not occurred.

The real loopback Postgres restore drill passed: all four encrypted blobs imported
into empty storage, exported, and restored through the existing recovery helper;
a repeated import was refused. Historical company records and transient OAuth/
login data have focused coverage. All live-data transfer remains deferred.
