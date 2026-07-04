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

## Built so far (beyond the original scaffold)

- Recent-entries list (`GET /api/timeactivities?days=N` or `?start=&end=`)
  with delete (`DELETE /api/timeactivity/{id}` — reads the entity for its
  `SyncToken`, then posts `?operation=delete`).
- Report tab (bottom tab bar): Day / Week / Month periods with prev/next
  navigation, hero total + billable split, hours-per-day column chart
  (tap a column to drill into that day), per-client/project proportional
  bars, and the period's entry list. All computed client-side from one
  range fetch.
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
- **Report person filter**: Everyone / per employee-or-vendor select that
  scopes the hero, chart, breakdowns, and entry list.
- **Repeat entry**: ⟳ on every entry row copies it into the form as a new
  entry dated today.
- **Duration rounding**: durations round UP to 15 min at save
  (`ROUND_MINUTES` in `index.html`; the success message notes the rounding).
- **Billable by default** for all new entries (boot, post-submit reset,
  cancel-edit).
- **Week tab**: projects × Mon–Sun grid (rows = this week's entries plus
  frequent projects, sorted by hours; day/row/grand totals). Tapping a cell
  types decimal hours inline and posts a new entry for that project+day
  (falls back to the Log form when who/service can't be auto-resolved).
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

## Backlog (not yet built)

- Timer mode (start/stop instead of typing a duration).
- CSV export of a period's entries.
