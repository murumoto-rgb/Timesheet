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
- Access token lives ~3600s. Refresh token is ~100-day rolling and **rotates** —
  always persist the `refresh_token` returned on refresh, not just the first one.
- Redirect URI must match a registered URI **exactly**. `http://localhost` is
  allowed for development; production requires `https://`.

### API base + versioning
- Sandbox: `https://sandbox-quickbooks.api.intuit.com`
- Production: `https://quickbooks.api.intuit.com`
- Always append `?minorversion=70` (or higher). Send `Accept: application/json`
  or you'll get XML back.

### Reading data (query endpoint)
`GET {base}/v3/company/{realmId}/query?query={SQL}&minorversion=70`

- Projects/clients: `SELECT * FROM Customer WHERE Active = true`
  - A **Project** is a special customer with `IsProject: true` and a `ParentRef`.
  - A sub-customer/job has `Job: true`. A plain client has neither.
  - `IsProject` is **read-only** — projects CANNOT be created via API. Reading is fine.
- Employees: `SELECT * FROM Employee WHERE Active = true`
- Service items: `SELECT * FROM Item WHERE Type = 'Service' AND Active = true`

### Creating a time entry
`POST {base}/v3/company/{realmId}/timeactivity?minorversion=70`

Minimum viable payload (Projects enabled, non-payroll):

```json
{
  "NameOf": "Employee",
  "EmployeeRef": { "value": "55" },
  "ItemRef":     { "value": "5" },
  "ProjectRef":  { "value": "416296152" },
  "CustomerRef": { "value": "2" },
  "Hours": 2,
  "Minutes": 30,
  "TxnDate": "2026-07-03",
  "Description": "…",
  "BillableStatus": "NotBillable"
}
```

Field rules that trip people up:
- **`ItemRef` is REQUIRED on create.** Every entry needs a Service item.
- With Projects on, set **both** `ProjectRef` (the project's `Customer.Id`) and
  `CustomerRef` (the project's `ParentRef.value`). For a plain client, set only
  `CustomerRef`.
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

- Keep it single-user and dependency-light. No DB, no auth framework.
- Token persistence is isolated to `_load_tokens` / `_save_tokens` in `main.py` —
  that's the seam to swap for Supabase/Postgres later.
- Surface QBO fault responses verbatim (they contain the real validation error).
- Never commit `.env` or `qbo_tokens.json`.

## Backlog (not yet built)

- Recent-entries list with delete (`TimeActivity` supports read + delete).
- Weekly total / summary view grouped by project.
- Toggle to log under a **Vendor** instead of an Employee.
- Deploy to Render (register the `https://…/callback` redirect URI in Intuit).
- Move token storage to Supabase.
- Edit an existing entry (requires `SyncToken` — read it, then sparse update).
