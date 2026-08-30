# Audit implementation — build 2026.08.30.3

Verified locally on August 30, 2026 before publication. No live QuickBooks
records were created, edited or deleted during implementation. Deployment status
is tracked separately from this implementation verification record.

## Five improvements

| Area | Implemented behavior |
| --- | --- |
| Safe accounting changes | Original save IDs survive uncertain responses and reloads. Version and company checks protect edits/deletes. Billed entries stay read-only. Multi-day and bulk actions retain each outcome and retry only unconfirmed work. Cross-tab recovery uses browser locks. |
| Accurate figures | Exact whole minutes; cents preserved; unknown rates and invoice amounts stay distinct from zero. People are separated by type and ID. Future-dated financial activity is excluded from current summaries, and hours, time value, invoices and cash are labeled separately. |
| Clearer organization | Searchable, pinnable Projects directory; project/date links; visible project context, section jumps and return navigation; Tools & settings groups recovery and configuration. |
| More usable layouts | Wider desktop analyses, compact phone summaries, long-note wrapping, a centered desktop navigation bar and keyboard-accessible dialogs. |
| Reliable operation and recovery | Stale-response guards, durable write receipts, private atomic file writes, corrupt-storage rejection, pinned dependencies, CI and offline backup/restore. The launcher preserves local edits and refuses unsafe partial migration of an older connection. |

## Five additions

| Addition | Access |
| --- | --- |
| Time reconciliation and save receipts | Tools & settings → Reconcile time |
| Dated labor budgets and agreed fees | Project → Plan & billing |
| Explicitly linked invoices and payment allocations | Project → Load accounting history |
| Resumable local draft and timer | Log |
| CSV and Print / save PDF exports | Project → Take this report with you |

See the [workspace guide](WORKSPACE_GUIDE.md) for exact steps and scope rules.

## Verification evidence

- **138 backend tests passed**, with zero failures, errors or skipped tests.
  This includes duplicate/day guards against stale and newer entity versions,
  lost responses, concurrent saves, company changes, corruption handling,
  payment attribution, recovery archives and legacy-launcher behavior.
- **75 browser tests passed**, with zero failures or skipped tests. They cover
  the existing app, new workspace features, retry behavior across tabs, mobile
  layouts, exports and local-data import validation. The save-safety tests now
  wait for actual responses instead of using fixed pauses for those assertions.
- Layout checks passed at **320, 390 and 1280 pixels**. The sample app was also
  reviewed interactively for Projects, project summaries, plans, accounting
  history and reconciliation.
- `pip check` found no dependency conflicts. `pip-audit -r requirements.lock`
  and `npm audit --audit-level=high` reported **no known vulnerabilities**.
- JavaScript and shell syntax checks and `git diff --check` passed.

All tests use synthetic fixtures. Raw local receipts are retained under the
gitignored `scratchpad/audit-2026-08-30/` directory. Reproduction commands are in
[release and recovery](RELEASE_AND_RECOVERY.md).

## Limits and publication checks

Passing tests is not proof that historical accounting data is error-free.
Reconciliation compares time entities and retained receipts; it is not an
independent QuickBooks accounting report. Invoice/payment history intentionally
excludes ambiguous relationships and invoices outside the selected date range.

Plans, categories and the current draft are local to one company and browser.
They are not cloud-synchronized. Exports can contain private client information.

Production must use one instance and one worker. Supabase concurrency was tested
with a mocked service, not a live database. Remote CI, deployment settings and
branch protection have not been verified by this local implementation pass.
Before publication, verify the intended company/storage, recovery readiness and
remote checks without changing credentials or restoring an old journal over a
live store. See the release guide for the precise boundaries.
