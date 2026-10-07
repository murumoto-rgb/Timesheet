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
6. Large historical reports: verify execution time, complete response delivery
   on the deployed Python runtime, and exact synthetic/source totals. The generic
   buffered-body limit must not be assumed to describe streamed Python delivery;
   record empirical results and recheck them after runtime/configuration changes.
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

Keep the old service available for recovery until the authorized cutover acceptance,
collector compatibility and backup restore are confirmed. Only then authorize
Render retirement, verify final recovery artifacts and remove its paid compute
and disk. Retaining a paid suspended disk can still cost money. Retain the GitHub
repository as the ongoing app source. No destructive retirement occurs in this PR.

## Prepared resources and evidence

- Neon project: `timesheet-vercel` (`gentle-resonance-75021927`), Ohio region.
  Runtime has only schema usage and table select/insert/update privileges.
- Vercel project: `timesheet-vercel-preview`, protected by Vercel authentication,
  no Git integration and no production credentials. Preview variables only.
- Preview: https://timesheet-vercel-preview-jrkprjccy-murumoto-rgbs-projects.vercel.app
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

This establishes preparation and synthetic behavior. Sandbox acceptance results
are recorded below. Report capacity passed within the measured synthetic envelope below. Collector
coordination and final live backup/cutover remain gates. The owner waived device/reminder acceptance testing
on October 6, 2026; it is not a required migration gate.

The real loopback Postgres restore drill passed: all four encrypted blobs imported
into empty storage, exported, and restored through the existing recovery helper;
a repeated import was refused. Historical company records and transient OAuth/
login data have focused coverage. All live-data transfer remains deferred.

Automatic sleep was observed through the Neon management API at
2026-10-07 03:00:47 UTC after deployed requests ended, without manual suspension.
The deployed `/api/status` request woke the endpoint at 03:01:22 UTC and passed
(3.16 seconds including CLI authentication/network overhead). A subsequent
encrypted synthetic read verified persistence (0.42 seconds). Health checks and
preview-side-effect guards were also verified. Reminder cron will wake production
briefly on its schedule; storage billing continues during sleep.

Implementation commit `d41924f` passed both GitHub CI runs, including backend and
75 browser tests plus both vulnerability audits. Local Postgres checks additionally
verified cross-connection locks, durable journal claims, audit appends, atomic
import rollback on a later insert failure, and the offline restore round trip.

## Sandbox acceptance setup (October 6, Chicago time)

The owner authorized sandbox acceptance testing. Created a separate Neon child
branch `timesheet-sandbox-acceptance` (`br-fragrant-voice-b4exbjgg`) with endpoint
`ep-quiet-brook-b49lcuwn`, capped at 0.25 CU and 300-second idle suspension.
The parent contained only synthetic probe/login records; the copied records
were verified by ID and removed from the disposable child before configuration.
No production data or tokens were copied. Child runtime password and encryption
key are independent. The parent branch and Render were not changed.

Protected Vercel project `timesheet-sandbox-acceptance`
(`prj_GtJyU9nAiR4B8J2s5SEl8uiYe41J`) runs the tested migration code at
https://timesheet-sandbox-acceptance.vercel.app . Its Vercel Production target
is used only to enable write acceptance against QuickBooks **sandbox**; this
is not the live Timesheet production project or a cutover. It has no Git
integration and no cron jobs. Health confirms Postgres and activated mode.
Development-only Intuit keys are configured; no live OAuth grant is present.

The owner approved saving the additional Development callback,
`https://timesheet-sandbox-acceptance.vercel.app/callback`; it was saved and read
back. Existing callbacks and all Production Intuit settings remain unchanged.
The deployed app connected to the separately verified QuickBooks sandbox company.

Actual deployed acceptance passed: exact 17-minute nonbillable create, identical
UUID replay, changed-payload rejection, edit, stale-version rejection, delete,
delete replay, readback proving removal, and three corresponding audit events.
The test entry was removed. Historical sandbox time, payment, bill, receivable,
and project financial reports returned successfully. Browser navigation through
Log, Week, Reports, Projects and Dashboard passed. These small sample datasets
do not establish production-size payload/runtime behavior.

A reminder request without its secret was rejected; the authenticated request
passed with no subscribed devices. No reminders were delivered and sandbox cron
remains disabled. Four encrypted sandbox blobs were exported and restored offline.
Three concurrent deployed reads passed after deliberately expiring the sandbox
access token; refreshed credentials persisted with the same company identity.
The restored archive was not run or used to refresh an OAuth grant.

One initial reconciliation request returned an unattributed plain HTTP 500;
subsequent requests repeatedly passed. Its cause has not been established.
Build `2026.10.06.2` adds safe correlated provider/reconciliation failures, rejects
malformed successful provider responses, preserves credentials on transient
refresh failures, and handles nullable descriptions and unknown/nonfinite rates.
These changes address independently reproduced defects; they do not prove the
cause of that initial deployed failure. Provider bodies and credentials are not
logged. Runtime failures now include a support reference and safe failure stage.

Build 2026.10.06.2 passed deployed sandbox CRUD, report and reconciliation checks,
and both CI runs. Report capacity passed within the envelope below. Before cutover:
coordinate collector ownership and revalidate current source/configuration; then coordinate a
short write freeze, final encrypted backup and import, company/data readback,
domain switch and rollback window. Render stays live until explicit cutover.
PR #4 remains a draft and must not be merged as part of preparation.


## State of play (October 6, 2026, Chicago time)

The owner directed production-size report testing and then a recorded holding
state. Migration itself remains deferred. Device/reminder testing was explicitly
waived; that waiver is not proof of device or reminder delivery. It does not change
any permissions or notification configuration. The new origin still requires
browser drafts/local state to be carried over and push enrollment if desired.

- Live authority: Render at `https://qbo-timesheet.onrender.com`, existing `main`
  branch and production QuickBooks connection. It stays active and unchanged.
- Candidate: draft [PR #4](https://github.com/murumoto-rgb/Timesheet/pull/4), branch
  `codex/vercel-neon-migration`, unmerged. Tested app build `2026.10.06.2`, source
  commit `17db4c69b3eb1b13b8ef643b72a77596c3d115c6`.
- Neon: separate Timesheet project `gentle-resonance-75021927`; 0.25 CU cap,
  300-second idle suspension verified. Restricted runtime role and independent
  encryption key are configured. Live credentials/history have not been moved.
- Read-only preview: deployment `dpl_BroeGpcHmRUjWbFePhrMCcKncukZ` at
  https://timesheet-vercel-preview-gkdqd1mmn-murumoto-rgbs-projects.vercel.app .
- Sandbox: separate Neon child `br-fragrant-voice-b4exbjgg`; Vercel deployment
  `dpl_8BWoUqgbFLjDqS6ckXzbBHKRJD7W` at
  https://timesheet-sandbox-acceptance.vercel.app . Development-only credentials,
  independent storage/key, disabled cron and no Git auto-deployment.
- Primary/sandbox secrets and backup artifacts remain private outside Git under
  the owner's Application Support/Timesheet directory. Do not print or copy them
  into the runbook, PR, frontend, or a capacity fixture.
- The earlier unattributed reconciliation 500 remains documented. Later repeated
  requests passed; correlated safe diagnostics address reproduced defects, but
  the original cause is unproven.

The report-capacity measurements below use entirely synthetic records through the
unchanged report code. They measure a workload envelope, not production data
parity, the current company's exact record count, or real QuickBooks latency.
No production grant or financial records are loaded by the capacity harness.


### Report capacity: passed within the tested envelope

Source `main.py` SHA-256:
`4443d86db944151dc12107173f8a925dc482a904f4f94795026a6426b08063c4`.
No application runtime change was necessary for this check; build remains
`2026.10.06.2`. [Aggregate measurements](REPORT_CAPACITY_RESULTS.json) contain
only generated-data counts, totals, timings, bytes, and source/deployment IDs.

| Deployed workload | Complete result | Response bytes | HTTP seconds |
| --- | --- | ---: | ---: |
| Time, 25,000 rows / 128-byte notes | 25,000 unique IDs, 1,925,000 exact minutes | 12,599,998 | 7.05 |
| Time, 5,000 rows / 1,024-byte notes | 5,000 unique IDs, 385,000 exact minutes | 7,000,000 | 1.01 |
| Payments + sales receipts, 25,000 each | 50,000 rows and exact combined amount | 4,549,441 | 2.58 |
| Bills + purchases, 25,000 each | 47,728 rows; 2,272 credits excluded; exact cost | 5,411,877 | 3.04 |
| Receivables, 25,000 invoices | 12,500 open invoices; exact trailing-year billed total | 2,828,937 | 1.63 |
| Project financials, 25,000 invoices/payments each | 2,500 exact-project invoices and linked payments; exact payment total | 608,313 | 1.47 |
| Reconciliation, 25,000 time rows | fresh count and exact minutes, read-only | 908 | 1.14 |
| Payments, 5,000 each / 750 ms injected per provider page | 10,000 complete rows; 12 provider reads | 909,884 | 9.51 |

All returned HTTP 200. HTTP seconds measure curl's complete request/transfer,
excluding CLI setup; injected delay is synthetic, not observed QuickBooks latency.
The local endpoints also preserved counts/totals at 999, 1,000, 1,001, 5,000,
10,000 and 25,000 source records, including the terminal empty pagination read.
The browser rendered every one of 10,000 five-year-report rows with 770,000 exact
minutes, aggregated the two-year WIP subset, and compared two concurrent
5,000-row periods. Failed current/comparison requests hid stale totals and showed
an error. Browser and deployed API checks are separate evidence, not a claim
that 25,000 rows were rendered on an actual device.

The generic [Vercel Functions limits](https://vercel.com/docs/functions/limitations)
page describes a 4.5 MB request/response body limit. This Python deployment
successfully delivered and parsed larger complete responses. Vercel separately
[documents default Python streaming](https://vercel.com/changelog/python-vercel-functions-now-have-streaming-enabled-by-default).
That runtime behavior is consistent with the measurements; it is not an unlimited
payload guarantee. Keep the tested Python runtime/configuration and repeat the
large-response probe if they change. No request-body limit was tested or bypassed.

Reproduce using `scripts/report_capacity.py`: build into a new temporary directory
outside Git and explicitly deploy with `--target preview` to the protected preview
project; never deploy the fixture wrapper to the sandbox alias or live project.
The wrapper strips actual database/QBO credentials, uses only generated records,
blocks all outbound requests and accounting/push/OAuth routes, keeps app sign-in,
and has no cron. The source is copied byte-for-byte and hashed in a manifest.
The optional local command needs test-only `httpx2==2.13.1`; it is not an added app
runtime dependency. The temporary capacity deployment is removed after evidence
capture; the actual preview and sandbox deployments remain intact.

This clears the synthetic report-size/runtime acceptance check for the recorded
workload. Before actual migration, revalidate the candidate and configurations,
coordinate the production collector, freeze writers, take a fresh consistent
backup, transfer to empty Neon production storage, verify private source/data
parity, and switch the live URL with a state-aware rollback window. Migration and
Render retirement still require the owner's later instruction. No work is queued
to perform a cutover automatically.
