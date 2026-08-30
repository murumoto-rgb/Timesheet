# Tests

Two suites. Together they are the regression net: run both before and after any
non-trivial change (especially a refactor) — they should stay green.

## Backend — `pytest` (mocked QBO, no network)

Pure Python logic with the QBO HTTP layer monkeypatched. Covers the parts that
touch **production financial data**, so a wrong number can't ship silently:

- `_timeactivity_payload` field rules (CustomerRef = project id, never
  ProjectRef, billable gate, hourly rate)
- `update_time` / `delete_time` **lock billed entries** (409) and preserve the
  HourlyRate on an ordinary edit
- `qbo_query_all` pagination past the 1000-row page cap
- `_receivables_summary` aging buckets / past-due / DSO / group-by-id
- `list_payments` + `list_bills` entity merges (credits excluded)
- `list_projects` exposes `parentId` for the client roll-up
- `_ta_summary`, `_audit`, `_ratecheck`, date-range validation
- Durable request IDs, lost responses, version conflicts, company changes and
  simultaneous creates; the daily guard includes confirmed writes still absent
  from a query. Storage concurrency tests use isolated files and mocked Supabase.
- Scoped reconciliation, explicit invoice/payment attribution, corrupt storage,
  push endpoint restrictions and offline backup/restore integrity.
- First-run launcher behavior when one or several older installations exist.

```bash
python -m pip install -r requirements.lock -r requirements-dev.txt
PYTHONPATH=. python -m pytest -q
```

## Frontend — Playwright + `node:test` (mocked `/api/*`, no build step)

Loads `index.html` in headless Chromium with every `/api/*` endpoint mocked from
a plain-JS dataset, then asserts the values each view actually **renders**. These
are behaviour-level (they assert output, not internals), so they survive a
refactor of the JS — which is exactly why they exist. Coverage:

- **Practice KPIs** — utilization / realization / effective-rate, and that
  mileage/expense lines are excluded from them
- **Client concentration** — main-client roll-up vs. per-project split over a
  real Client → job → project hierarchy
- **Subcontractor margin** — revenue − sub cost, incl. negative-margin
  formatting/colour
- **Unbilled WIP** — stable across the Day/Week/Month toggle
- **Receivables** — aging + who-owes render from `/api/receivables`
- **Report year-over-year** — shown for Month, hidden for Day
- **Week grid** — billed time amber, unbilled blue
- **Billed lock** — badge + no delete on billed rows; tapping one opens a
  locked, read-only form; Close unlocks it
- Page and console errors, alongside the rendered behavior.

Project deep-dive regressions in `project_deep_dive.test.mjs` additionally cover
directory search and pins, separate same-name IDs, exact time/value totals,
explicit zero versus unknown rates, mileage exclusion, read-only entry filters,
bookmarked dates and browser history, failed-load retry, and late-response races.
Workspace regressions cover exact-minute writes, retry recovery across tabs,
partial batches, newer-draft preservation, budgets, invoice scope, CSV and print,
invalid backup imports and layouts at 320, 390 and 1280 pixels.

```bash
npm ci
npx playwright install chromium
tests/frontend/run.sh
# or, equivalently:
node --test --test-concurrency=2 tests/frontend/*.test.mjs
```

Playwright comes from the local lockfile, with its matching installed Chromium.
Override the browser with `PLAYWRIGHT_CHROMIUM` only when needed. The harness
blocks service workers so all accounting requests stay in the mocked API layer.
`tests/frontend/harness.mjs` holds the shared
`openApp(browser, data, view)` fixture and `moneyStats()` reader — add new tests
by importing those.

Use the Python version in `.python-version`. See
[release and recovery](../docs/RELEASE_AND_RECOVERY.md) for the complete clean
environment, vulnerability-scan and backup commands. These tests do not prove
that production settings or a live Supabase installation match the fixtures.
