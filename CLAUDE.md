# CLAUDE.md — QBO Timesheet

Persistent project context for Claude Code. Read this before changing anything.

## What this is

A **single-user** web app for logging time straight into **QuickBooks Online**.
The user picks a project (or client), an employee, and a service item, enters a
duration, and submits — the app creates a QBO `TimeActivity`. Built to replace
manual timesheet entry for a solo consulting practice.

Stack: **FastAPI** (Python) backend + a **single static `index.html`** frontend
(no build step). Requests via `requests`. Tokens persisted to a local JSON file.

## Files

- `main.py` — FastAPI app: OAuth, read endpoints, and the create endpoint.
- `index.html` — the entire frontend (form + vanilla JS), served by FastAPI at `/`.
- `.env` — secrets (gitignored). Template in `.env.example`.
- `qbo_tokens.json` — created at runtime after connecting (gitignored).

## Run

```bash
pip install -r requirements.txt
uvicorn main:app --reload      # http://localhost:8000
```

Open `/`, click Connect QuickBooks, authorize. Start in the **sandbox**
(`QBO_ENVIRONMENT=sandbox`) before touching real books.

---

## QuickBooks Online API — verified facts (do NOT guess these)

These were confirmed against Intuit's current docs. Field names differ from the
old QB Desktop API — use exactly what's below.

### OAuth 2.0
- Authorize: `https://appcenter.intuit.com/connect/oauth2`
  params: `client_id`, `response_type=code`, `scope=com.intuit.quickbooks.accounting`,
  `redirect_uri`, `state`.
- Token exchange + refresh: `POST https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer`
  - Header `Authorization: Basic base64(client_id:client_secret)`
  - Body `application/x-www-form-urlencoded`
  - Exchange: `grant_type=authorization_code&code=…&redirect_uri=…`
  - Refresh: `grant_type=refresh_token&refresh_token=…`
- The callback receives `code`, `state`, **and `realmId`** (the company ID — store it).
- Access token lives ~3600s. The refresh token **rotates** (its value can change
  every ~24h) — always persist the `refresh_token` returned on refresh, not just
  the first one. Since Nov 2025 refresh tokens have a max validity of 5 years
  (previously 100 days of inactivity).
- Redirect URI must match a registered URI **exactly**. `http://localhost` is
  allowed for development; production requires `https://`.

### API base + versioning
- Sandbox: `https://sandbox-quickbooks.api.intuit.com`
- Production: `https://quickbooks.api.intuit.com`
- Since **2025-08-01** Intuit ignores `minorversion` values below 75 — the base
  version is 75. We send `?minorversion=75`. Send `Accept: application/json`
  or you'll get XML back.

### Reading data (query endpoint)
`GET {base}/v3/company/{realmId}/query?query={SQL}&minorversion=70`

- Projects/clients: `SELECT * FROM Customer WHERE Active = true`
  - A **Project** is a special customer with `IsProject: true` and a `ParentRef`.
  - A sub-customer/job has `Job: true`. A plain client has neither.
  - `IsProject` is **read-only** — projects CANNOT be created via API. Reading is fine.
- Employees: `SELECT * FROM Employee WHERE Active = true`
- Service items: `SELECT * FROM Item WHERE Type = 'Service' AND Active = true`
- **Paginate past 1000.** A single query returns ≤1000 rows; a real practice
  can have >1000 customers/matters. `qbo_query_all` loops `STARTPOSITION`
  until a short page — used for Customer/Employee/Vendor/Item lists.

### Creating a time entry
`POST {base}/v3/company/{realmId}/timeactivity?minorversion=70`

Minimum viable payload (non-payroll):

```json
{
  "NameOf": "Employee",
  "EmployeeRef": { "value": "55" },
  "ItemRef":     { "value": "5" },
  "CustomerRef": { "value": "416296152" },
  "Hours": 2,
  "Minutes": 30,
  "TxnDate": "2026-07-03",
  "Description": "…",
  "BillableStatus": "NotBillable"
}
```

Field rules that trip people up:
- **`ItemRef` is REQUIRED on create.** Every entry needs a Service item.
- **A Project is a sub-customer** (`IsProject: true`). Attach project time via
  `CustomerRef` = the **project's own `Customer.Id`** — QBO derives the parent
  client from the project's `ParentRef`. Same for a plain client or a job: only
  `CustomerRef`, set to that entity's id.
- **Do NOT send `ProjectRef` on TimeActivity.** It exists but is gated to US +
  QBO Advanced / Enterprise Suite; other companies reject it with
  `"Invalid ProjectRef"` (code 9341). `CustomerRef`=project id works on every
  Projects-enabled tier. (Confirmed against this company's live 9341 error.)
- `NameOf` is `"Employee"` or `"Vendor"`; pair it with `EmployeeRef` or `VendorRef`.
- `BillableStatus`: `"Billable"` | `"NotBillable"` | `"HasBeenBilled"`. Billable
  requires a `CustomerRef`. Add `HourlyRate` to override the item's rate.
- `PayrollItemRef` is only for QBO Payroll customers — omit otherwise.
- Duration is `Hours` + `Minutes` (0–59). `StartTime`/`EndTime` exist but are flaky.

### Requirements on the connected company
- **Projects** feature needs QBO **Plus / Advanced / Enterprise Suite**.
- At least one **Employee**, one **Service item**, and (for project tracking)
  Projects turned on with ≥1 project.

---

## Conventions

- Keep it single-user and dependency-light. No auth framework (just the
  APP_PASSWORD cookie gate, plus optional TOTP two-factor via `TOTP_SECRET` —
  pure-stdlib RFC 6238, enroll at `/mfa-setup`).
- Token persistence is isolated to `_load_tokens` / `_save_tokens` in `main.py`:
  local JSON file by default, Supabase table (`qbo_tokens`) when
  `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` are set (diskless Render free tier).
- Surface QBO fault responses verbatim (they contain the real validation error).
- Never commit `.env` or `qbo_tokens.json`.
- **Build number**: `#buildInfo` in `index.html`'s footer shows `build
  YYYY.MM.DD.N` (single source of truth — one string in the HTML). BUMP it
  on every push (increment N same-day, or roll the date), and state the new
  build number at the end of each response so the user can confirm the
  deployed version matches.

## Built so far (beyond the original scaffold)

- Recent-entries list (`GET /api/timeactivities?days=N` or `?start=&end=`)
  with delete (`DELETE /api/timeactivity/{id}` — reads the entity for its
  `SyncToken`, then posts `?operation=delete`).
- **Audit trail**: every create/update/delete appends an event to an
  append-only store (`_load_audit`/`_save_audit`, blob id=3 — local
  `qbo_audit.json` or Supabase, capped to `AUDIT_MAX`=2000). Each record has
  a UTC `ts`, `action`, `entryId`, a readable `summary` (date/hours/who/
  service/customer/billableStatus/description) built from the QBO response,
  the source `ip`, and — for updates — the prior `before` snapshot so the
  viewer can show what changed. `_audit()` never raises (auditing must not
  break a write). `GET /api/audit?limit=N` returns events newest-first;
  `/audit` is a password-gated server-rendered viewer (footer "Activity
  log" link) with color-coded action badges.
- **New-entry Cancel**: the Log form shows a "Cancel" button (`#cancelNew`)
  in new-entry mode that discards the in-progress entry and resets the form
  (`clearFormFields`); in edit mode it's swapped for "Cancel edit"
  (`setEditMode` toggles the two + the submit label + the editing recolor).
- Report tab (bottom tab bar): Day / Week / Month / **Quarter** periods with
  prev/next navigation, hero total + billable split, column chart (per-day
  for week/month → tap drills into that day; **3 month bars for a quarter →
  tap drills into that month**), a **"To invoice" card** (per-client
  not-yet-invoiced `Billable` hours in the period, sorted desc, hidden when
  none), per-client/project + per-service proportional bars, and the
  period's entry list. All computed client-side from one range fetch.
  `list_time` paginates (`qbo_query_all`) so long ranges never truncate.
- Mobile-first UI: duration preset chips, remembered last-used
  project/employee/service (localStorage), 16px inputs (no iOS zoom).
- PWA installability: `static/manifest.webmanifest`, icons, apple-touch meta —
  "Add to Home Screen" gives an app-like standalone window.
- Graceful unconfigured state (missing `.env` shows setup instructions instead
  of crashing on import).

## Built so far (continued)

- **Vendor mode**: `/api/vendors`; the "Employee / vendor" dropdown groups both,
  and create/update send `EmployeeRef` or `VendorRef` with the right `NameOf`.
- **Edit an entry**: tap a recent/report row to load it into the form; saves via
  `PUT /api/timeactivity/{id}` (reads `SyncToken`, then posts the full update).
- **Per-service breakdown** in the Report tab, alongside per-client.
- **Frequent picks**: picker sheet leads with the top-15 most-used
  projects/clients from the last 60 days of entries.
- **Notes templates**: dropdown of standard descriptions (edit the
  `TEMPLATES` array in `index.html`).
- **Smart defaults** (`RULES` in `index.html`, name-matched): Murat Baykal →
  service PR + billable; Garner Consulting → PE; project name containing
  GCG → GCG (project rule beats who rule). Rules set defaults on selection
  and never fire while editing an existing entry.
- **Report person filter**: chips (All + each employee/vendor with time in
  the period, shown only when >1 person — same as the Week tab) that scope
  the hero, chart, breakdowns, and entry list.
- **Billable = billable work**, counting both `Billable` (not yet invoiced)
  and `HasBeenBilled` (already invoiced). The report hero splits it as
  **hours** — "X billable · Y to invoice" (Y = the not-yet-invoiced
  `Billable` portion, shown only when 0 < Y < X). Never use a `$` prefix
  for billable *hours* (it misreads as dollars); billable time is shown as
  green text/markers and a green share of each per-project/service bar
  (`.bpart` inside `.fill`). API returns raw `billableStatus`.
- **Repeat entry**: ⟳ on every entry row copies it into the form as a new
  entry dated today.
- **Duration rounding**: durations round UP to 15 min at save
  (`ROUND_MINUTES` in `index.html`; the success message notes the rounding).
- **Billable by default** for all new entries (boot, post-submit reset,
  cancel-edit).
- **Blank project by default**: the form boots with no project selected
  (`Select…`) so every project choice is deliberate — no sticky
  last-project restore. Cancel-edit resets project → blank and date →
  today (plus clears duration/notes, billable back on).
- **Editing recolors the form**: entering edit mode adds `.editing` to
  `#logForm`, giving the whole card an accent border, glow ring, and tint
  (on top of the "Editing an existing entry" banner). Cleared on cancel,
  successful save, and repeat/copy.
- **Week tab**: projects × Mon–Sun grid (rows = this week's entries plus
  frequent projects, sorted by hours; day/row/grand totals) with a people
  filter (All + each employee/vendor with time that week; shown only when
  >1 person). Tapping a **blank** cell opens the Log form pre-filled (that
  day, that project, me/filtered person, rule-picked service) as a new
  entry; tapping a cell with an **existing** entry opens it for editing
  with a highlighted "Editing an existing entry" banner (PUT update). No
  inline entry from the grid.
- **Recent-project chips**: card atop the Log tab with the top 5 projects of
  the **last 30 days**, ranked by entry count (then hours, then most-recent
  day) — so active projects lead stably instead of noisy last-touched order.
  Tapping one selects it in the form (applying smart-default rules) and
  scrolls the form to the top.
- **Daily reminder push** (optional): payloadless Web Push, VAPID keys
  auto-generated + persisted with `_load_push`/`_save_push`. A background
  thread (`_reminder_loop`) nudges once/day past `REMINDER_HOUR` in
  `REMINDER_TZ` if no time is logged (weekdays only by default). Enrolled
  via the "Daily reminders" footer link; message lives in `static/sw.js`.
  Only `cryptography` was added (payloadless avoids the http-ece dep).

- **Totals tab** (4th bottom tab; internal ids still `people`/`peo*`):
  hours by project, then per person, over time. Granularity seg
  **Day/Week/Month/Qtr** (default **Month**) where the selected unit **is**
  the period shown and summed (`peoWindow`): Day→that day, Week→Mon–Sun,
  Month→calendar month, Qtr→the quarter; prev/next pages by that unit. The
  drill-down grid/sparkline split the period into one-finer `buckets`
  (Day→the day, Week→7 days, Month→its weeks, Qtr→3 months). **Landing** (`renderProjectTotals`,
  shown when `peo.projectId` is null; the tab always resets to it on open):
  every project with hours in the period, **alphabetical**, each a tappable
  row with total, billable split, and a two-tone bar. Tapping a row sets
  `peo.projectId`/`projectName` from the row's own data and opens the
  **drill-down** (`renderPersonDrill`): a "‹ All projects" back button plus
  the chosen **leaderboard + expandable pivot grid** (ranked people, avatar
  initials, % share, billable, per-bucket sparkline; "Show the full grid" →
  people×bucket pivot, sticky first column, row/col/grand totals). Both the
  list and the drill-down derive project identity from the entries
  themselves (`e.projectId || e.customerId` + `e.customer`), so the picked
  id always matches the tagged time — this replaced an earlier dropdown that
  could hand the drill-down a non-matching id (time showed in Report but not
  here). All client-side from one `fetchRange`. Person colors `PEO_COLORS`.

## Backlog (not yet built)

- Timer mode (start/stop instead of typing a duration).
- CSV export of a period's entries.
