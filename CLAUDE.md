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
  period's entry list. **Tapping a "By project / client" row jumps to that
  project's Totals drill-down for the same period** (`goToTotals` sets
  `peo.unit`/`anchor` from `rep`, then `showView("people", {keepPeo:true})`
  — Report & Totals share the same period units, so they line up). All
  computed client-side from one range fetch. `list_time` paginates
  (`qbo_query_all`) so long ranges never truncate.
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
- **Duration rounding**: durations round to the NEAREST 30 min (half hour)
  at save, floored so a logged entry never rounds to 0 (`ROUND_MINUTES` in
  `index.html`; the success message notes the rounding).
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

- **Per-project drill-down** (lives INSIDE the Report tab — the standalone
  "Totals" tab was removed once its landing screen proved to duplicate
  Report's "By project / client" breakdown). There are now **4 bottom tabs**:
  Log / Week / Report / Dash. **Entry point**: tap a row in Report's "By
  project / client" card (`renderBreakdown` → `openProjectDrill(id,name)`,
  which sets `rep.projectId`/`projectName`/`drillExpanded` and calls
  `renderReportView` — **no refetch**, it reuses the period's already-loaded
  `repFetched`). `renderReportView` hides the normal report body (`#repMain`)
  and shows `#repDrill` when `rep.projectId` is set; the period seg + Hours/$
  + hide-expenses controls stay above it, so the range is still switchable
  while drilled (changing it keeps the drill open). **The drill ignores the
  person filter and the search/cleanup filter** — it is itself a per-person
  view of the whole project. `renderDrill(entries)` renders a "‹ Back to
  report" button (`#repDrillBack` → `closeProjectDrill`) plus the
  **leaderboard + expandable pivot grid** (ranked people, avatar initials, %
  share, billable, per-bucket sparkline; "Show the full grid" (`#totToggle`)
  → people×bucket pivot, sticky first column, row/col/grand totals). Buckets
  come from `drillWindow()` (keyed on `rep.unit`: Day→the day, Week→7 days,
  Month→its weeks, Qtr→3 months) with `spanWindow()` picking a sensible
  bucket size for **custom / all-history** ranges (daily ≤16d, weekly ≤100d,
  monthly ≤24mo, else yearly); `win.noun` names the grid's column
  granularity. Project identity comes from the entries themselves
  (`e.projectId || e.customerId` + `e.customer`) so the picked id always
  matches the tagged time. Landing on the Report tab (or `showView("report")`)
  resets the drill back to the overview. Person colors `TOT_COLORS`.

- **Dollars + expense filter** (Report + Dashboard): a shared **Hours ⇄ $**
  toggle and a **Hide mileage & expenses** checkbox (`opts.dollars` /
  `opts.hideExpenses`, persisted in localStorage, synced across both tabs via
  `syncOptsUI`). `$` figures come **straight from each entry's own QBO
  `HourlyRate`** (`dollarsOf` = hours × that entry's rate = the QBO line
  amount) — never an average; `/ratecheck` confirmed ~98% of entries carry a
  rate. The value layer is `val(e)` (minutes in Hours mode; in $ mode only
  **billable** work has value, non-billable → $0) + `fmtVal`. In $ mode: hero
  shows "billable value · $X to invoice", a `#heroNote` flags billable
  entries with no rate (excluded from $), breakdowns/leaderboard drop the
  green billable split (all $ is billable) and hide zero-value rows, the
  chart axis switches to $ (`toDisp`/`tickTxt`), and the grid title reads
  "Value by …". Expense/mileage lines are name-matched (`EXPENSE_RE =
  /mileage|expense|reimburs/i`) and removed from both tabs when the box is
  checked — a per-entry `hours × rate` still yields the right $ for a mileage
  line, but hiding them keeps *hours* totals honest. `$` never appears on
  hours (keeps the "green hours ≠ dollars" rule); the Log/Week/entry-list
  surfaces stay in hours always.

- **Dashboard tab** (5th bottom tab, `#dashView`, `data-view="dash"`): a
  business overview, NOT by project. Granularity **Day/Week/Month** (default
  Month) = bucket size over a trailing window (last 30 days / 26 weeks / 12
  months); no period nav — always trailing to today. Reuses the shared
  Hours/$ + hide-expenses `opts`. **Chart** (`drawDashChart`): blue bars =
  **invoiced** value per bucket (`billableStatus === "HasBeenBilled"` only,
  in the active unit) with a green **12-month trailing-average line**
  overlaid (SVG `.trendline` polyline, `viewBox 0 0 100 100`
  preserveAspectRatio=none, `vector-effect:non-scaling-stroke`, aligned to
  the `.cols` inset). The trailing avg at each bucket = invoiced over the
  prior 365 days ÷ `per12` (12/52/365) = per-bucket rate. **Table**
  (`.dtbl`): Period · Total · Invoiced, newest first, with an "Avg /
  <unit>" footer. Headline = the latest trailing-avg value. Needs ~2 years
  of history per view, so it relies on the `list_time` pagination. Invoiced
  depends on time being marked billed in QBO (HasBeenBilled). **Billed ⇄
  Received** toggle (`dash.metric`, shown only in $ mode): "Received" swaps
  the invoiced series for **actual cash in** — `GET /api/payments`
  (`list_payments`, paginated) sums QBO **Payment + SalesReceipt** `TotalAmt`
  per bucket. Received mode drops the Total column (no "logged value" for
  cash) → Period · Received. The trailing SVG line stretches via an explicit
  `width: calc(100% - 34px)` — an SVG is a replaced element and won't fill
  left/right insets with `width:auto` (that bug squished the line into the
  left third). **Scrub**: drag across the chart (pointer events on `.plot`,
  `touch-action:none`) → a crosshair + highlighted bar, and the hero swaps
  to that bucket's exact value + "period · avg <trailing>"; reverts on
  release (`dashDefaultBig`/`dashDefaultCap`). Headline is rounded
  (`~159hrs` / `~$39K`); scrub readout is exact (`fmtVal`). **MB effective
  rate** (`.dash-rate`, green): total (billed or received) $ over the shown
  window ÷ hours worked by Murat Baykal (`MB_RE = /murat\s*baykal/i`) in that
  window — revenue includes vendor/leverage work, denominator is MB's hours
  only. Numerator follows the Billed/Received toggle. Dashboard graphs sum
  **all** employees + vendors (no person filter).

## Backlog (not yet built)

- Timer mode (start/stop instead of typing a duration).
- CSV export of a period's entries.
