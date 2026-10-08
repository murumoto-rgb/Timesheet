# Vercel and Neon production cutover

Updated 2026-10-08 UTC (2026-10-08 Chicago). The owner authorized migration and
approved the additional Intuit Production callback. **Production now runs at
https://qbo-timesheet.vercel.app on Vercel with dedicated Neon storage. Render is
suspended, in maintenance mode, with automatic deployment disabled.**

## Production and evidence

- Vercel project `qbo-timesheet`, `prj_sl3jQmxOqzs0foqaK5f88lPaHpe8`, activated
  production deployment `dpl_43ixWw8zPKNDg1kYVyG2JUwhDPtg`, initial build `2026.10.06.2`.
  The verified deployment was promoted to the production domain. Standard Vercel
  protection restricts generated deployment URLs and previews; the production
  domain retains the app's existing password and TOTP protection. No-session,
  invalid-password and missing-TOTP requests returned 401; browser login passed.
- Intuit Production callback saved and read back:
  `https://qbo-timesheet.vercel.app/callback`. Existing callbacks were preserved.
  Existing production OAuth credentials and company binding were transferred;
  no fresh company consent or QuickBooks accounting write was performed.
- Neon project `gentle-resonance-75021927`, production branch
  `br-wild-shadow-b4hov6gd` (`timesheet-production`), compute
  `ep-divine-star-b41au3bd`: independent runtime password/encryption key, direct
  TLS connection, compute min/max 0.25 CU, auto-suspend 300 seconds. The production
  compute was observed idle/suspended before import. Preview and sandbox storage
  remain separate. Health checks deliberately do not query Neon.
- Private complete-record comparison over 2021-01-01 through 2026-10-07 passed
  for 5,612 time entries, 846 payment/receipt rows, 3,581 expense rows, company,
  projects and services. The time response was 2,205,090 bytes and completed in
  15.45 seconds including CLI overhead. Receivables differed only in date-derived
  fields after midnight; a current-Neon read at the original as-of date matched
  the entire original Render response exactly.
- Fresh public-origin browser login and full historical report passed, showing
  42,539h40m; no browser errors were captured. Screenshot retained privately.
  Device/reminder testing was waived by the owner and is not claimed as passed.

## Consistent transfer and recovery

Render service `srv-d947m8e7r5hc739gd9sg`, disk `dsk-d947m8m7r5hc739gda10`,
was put into maintenance at 04:53:54 UTC and in-flight work drained. The initial
stopped-process snapshot was stable, but Render health checks restarted that
process during retrieval. Before import, the replacement writer was verified
against every snapshot file hash, stopped, checked again, and the service was
suspended. Render API readback confirmed suspension at 05:00:33.782725 UTC.
Only then was the snapshot imported into empty production Neon storage.

- Decrypted imported tokens, push state and audit matched the source JSON.
  The legacy app had no operation journal; the empty journal was explicitly
  acknowledged. All four exported blobs validated and offline restore passed.
- The original push blob, including its one Render-origin subscription, was
  retained in private recovery. That subscription was removed only from the new
  database before activation, so reminders cannot reopen the old Render origin.
  Devices must re-enroll reminders on the new origin.
- A further current-Neon four-blob export after activation validated and restored
  offline with exact file parity. Recovery includes the current OAuth grant.
- Credentials, source responses, hashes, browser exports and recovery files are
  outside Git under the private Timesheet vercel-preparation directory.

## Owner layout requirement

The owner requires the phone layout on desktop as well. The inherited
`workspace.css` desktop media block expanded selected views to 1,120px and two
columns; it was present in both the retained Render page and initial Vercel page.
That block is removed in build `2026.10.08.1`, restoring the base 520px maximum
width and single-column layout for every screen. Phone CSS, colors and controls
are unchanged. Responsive checks cover 320px, 390px and 1280px, including all main
views, project detail and reconciliation.

## Browser-local state and future operation

The inspected desktop's plans/drafts export contained no budget or draft. Its
reconciliation had six existing similar-entry groups and matching fresh totals;
no repair, deletion or retry was performed. Plans/drafts export does not include
browser-only pending operations or batch IDs. Retain the old-origin browser data
on all devices; other devices were not inspected. Open the new URL and sign in
with the existing password/authenticator. An installed old-origin shortcut must
be replaced with the new URL; new-origin reminders require enrollment.

The accounting rehearsal's latest handoff reported no collector left running,
and no local collector process was found. Do not start an old shared-grant helper;
future helpers must use the current Neon-backed configuration and locking.

## Rollback and Render retirement

Do not simply resume Render: its disk now holds a stale OAuth grant. To roll back,
stop Vercel writers/reminders, export current Neon state, validate all four blobs,
restore those current files to Render, reconcile uncertain operations, then
resume exactly one authoritative writer. Retain private backups and the original
Render disk until recovery acceptance. Permanent service/disk deletion has not
been performed. Suspension stops the running service charge; retained disk
storage may still incur its separate storage charge.

Render auto-deploy is OFF both live and in `render.yaml`, preventing a merge from
restarting the recovery host. Merge the reviewed migration PR before connecting
Vercel Git to repository main; verify the resulting production deployment and
retain this state-aware rollback boundary.
