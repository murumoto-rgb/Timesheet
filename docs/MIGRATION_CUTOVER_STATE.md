# Authorized cutover state

Updated 2026-10-08 UTC (2026-10-07 Chicago). The owner explicitly authorized
the migration. This supersedes the earlier preparation-only instructions in
VERCEL_NEON_MIGRATION.md. **Cutover is not complete. Render still serves live traffic.**

## Completed before the write freeze

- Render service `srv-d947m8e7r5hc739gd9sg` in confirmed My Workspace:
  automatic deployment disabled and read back through the Render API. The
  blueprint now also declares `autoDeployTrigger: off` to prevent a merge from
  restarting the recovery host.
- Prepared Vercel project renamed `qbo-timesheet`, same project ID
  `prj_sl3jQmxOqzs0foqaK5f88lPaHpe8`. Registered and verified
  `qbo-timesheet.vercel.app`; production activation remains OFF and read-only
  guard ON. Git integration remains disconnected. SSO protection remains on.
- Production password, TOTP and existing production OAuth client settings
  captured privately; no credentials or source financial records enter Git.
- Guarded production deployment `dpl_DSLXmoBc6XotDyJvXgUk1bBHzYDV` health passed:
  Postgres storage, previewReadOnly true. An earlier guarded deployment rejected
  a pooled connection; corrected to direct TLS connection before proceeding.
- Dedicated production branch `timesheet-production`,
  `br-wild-shadow-b4hov6gd`, compute `ep-divine-star-b41au3bd`, in the existing
  dedicated Timesheet Neon project. Verified only inherited synthetic IDs 6/90,
  removed those before import, generated independent runtime password and
  encryption key. No live tokens imported. Compute min/max 0.25 CU, suspend 300
  seconds; read back idle/suspended at 04:46:32 UTC. Preview and sandbox storage
  remain separate.
- Saved private Render read-only baseline for 2021-01-01 through 2026-10-07:
  5,612 time entries (2,205,090 bytes), 846 payment/receipt rows, 3,581 expense
  rows, company/projects/services and receivables. Compare complete private
  records after transfer, not only counts or totals.
- Desktop plans/drafts backup retained privately: no budgets, no draft. Desktop
  reconciliation showed matching fresh totals (42,539h40m) and six existing
  similar-entry groups; no repair, deletion or retry performed. Browser-only
  pending-operation and batch IDs are not included by the plans/drafts export.
  Retain old-origin browser data on all devices; other devices were not inspected.
- Snapshot script staged on Render but NOT executed. It requires today's
  reminder claim, no outgoing TLS request, verified stopped uvicorn process,
  identical file bytes/mtimes on repeated reads, and a hashed private archive.

## Immediate pending gates

1. Owner action-time approval to save the prepared additional Intuit Production
   redirect `https://qbo-timesheet.vercel.app/callback`. Existing callbacks stay.
2. Brief maintenance/write freeze; drain in-flight work, freeze old uvicorn and
   capture its consistent disk snapshot while the mount remains accessible.
   Confirm old service suspended before acknowledging source stopped/import.
3. Import into empty production Neon storage; explicitly acknowledge the old
   app's absent operation journal. Verify decrypted parity and offline restore.
4. Preserve the imported push blob in recovery, then remove its one old-origin
   subscription from the new database before enabling production reminders.
   The old subscription would open Render; new-origin devices must re-enroll.
5. Activate Vercel, verify password/TOTP and private live QBO parity without
   source writes, then promote the verified deployment to the chosen URL.
6. Record final evidence, merge the reviewed PR with Render auto-deploy OFF,
   connect future Vercel Git deployment safely, and preserve state-aware rollback.

No live data transfer, write freeze, service suspension, production activation,
QuickBooks write or destructive retirement has occurred at this checkpoint.
The accounting rehearsal's latest handoff says no collector was left running;
no collector process was found locally. Do not start an old shared-grant helper
after cutover; its access configuration must be adapted before future use.

All credentials, source responses, browser backups and operator scripts are
outside Git under the private Timesheet vercel-preparation directory. Retain the
original Render disk until final recovery/cutover acceptance. Never resume its
stale grant after Vercel refreshes; export current Neon state first.
