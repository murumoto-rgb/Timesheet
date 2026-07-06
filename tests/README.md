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

```bash
pip install -r requirements-dev.txt
pytest -q
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
- Every test also asserts **no page/console errors**.

```bash
tests/frontend/run.sh
# or, equivalently:
NODE_PATH="$(npm root -g)" node --test tests/frontend/*.test.mjs
```

Playwright is used from the global install via `NODE_PATH` (no local
`npm install` needed here). Chromium is at `/opt/pw-browsers/chromium`; override
with `PLAYWRIGHT_CHROMIUM`. `tests/frontend/harness.mjs` holds the shared
`openApp(browser, data, view)` fixture and `moneyStats()` reader — add new tests
by importing those.
