# QBO Timesheet

A single-user timesheet and project workspace. Log exact minutes to QuickBooks
Online, review time by project, and keep local plans and drafts alongside
read-only accounting history.

On a phone, open the app in the browser and use **Add to Home Screen** — it
installs as a standalone app with its own icon.

## Project deep dives

Tap **Projects** in the bottom bar, search for a project or client, and tap its
name. Use the **star** to pin frequently used projects; **Pinned only** gives
you a short list of favorites.

Each deep dive opens year to date and includes:

- Total, billable, to-invoice, and billed time, plus recorded time value.
- People, services, activity trends, and an expandable people-by-period grid.
- Read-only entries and notes, with search and billing/missing-data filters.
- Day, week, month, quarter, custom dates, and quick ranges up to **Last 5 years**.

Use **Copy link** to bookmark the project and its selected dates. The link still
requires the app's normal sign-in. You can also open the same deep dive from a
Report project row or a Dashboard concentration row in **Project** mode.

Figures include only time tagged to the selected QuickBooks project/client ID;
child projects are separate. Projects with the same name stay separate, and the
directory shows the parent client where available. Historical projects appear
when their time is found in loaded reports.

Dollar values use each time entry's recorded rate, with cents preserved. Missing
rates are flagged as unknown; an explicitly recorded zero rate remains zero.
These figures are not invoice totals, cash receipts, or profit. The mileage and
expense filter applies to every deep-dive total and entry. Browsing a deep dive
does not create, edit, delete, or mark any QuickBooks entry as billed.

## Workspace tools

- **Projects → a project → Plan & billing:** keep a dated hours budget or fee,
  review explicitly linked invoices and allocated payments, and download CSV or
  use Print / save PDF. Budgets and expense-category overrides stay on this device.
- **Log:** unfinished work is saved locally. Restore it explicitly after a reload,
  or start a timer and review its duration before posting. Nothing posts in the background.
- **Tools & settings → Reconcile time:** compare an app time snapshot with a fresh
  QuickBooks read and review duplicate candidates, changed records and uncertain saves.
- **Tools & settings → Back up plans & drafts / Restore plans & drafts:** back up this browser's plans,
  categories and draft. The file is private and company-specific; it does not contain
  credentials or change QuickBooks records.

See [the workspace guide](docs/WORKSPACE_GUIDE.md) for navigation and limitations,
[the implementation verification](docs/IMPLEMENTATION_VERIFICATION.md) for the
audit changes and test results, and [release and recovery](docs/RELEASE_AND_RECOVERY.md)
for testing and safe backups.

All accounting changes carry the displayed company, an original save UUID and,
for edits/deletes, the entry version you reviewed. Stale versions and old-company
tabs are rejected. If a save result is uncertain, retry the original request
instead of creating a replacement. Multi-day and bulk actions show confirmed
and unconfirmed outcomes and retry only unconfirmed work.

## 1. Create the app in Intuit's portal (one-time, manual)

1. Go to **developer.intuit.com** → sign in → **Create an app** → choose
   **QuickBooks Online** and the **Accounting** scope.
2. Open **Keys & OAuth**. You'll see two key sets: **Development** (sandbox) and
   **Production**. Start with Development.
3. Under **Redirect URIs**, add exactly:
   `http://localhost:8000/callback`
4. Copy the **Client ID** and **Client Secret**.

## 2. Configure

```bash
cp .env.example .env
# paste your Client ID / Secret into .env, leave QBO_ENVIRONMENT=sandbox for now
```

## 3. Run

**Mac, no terminal:** put `Timesheet.command` on your Desktop and double-click
it. It pulls the latest code from GitHub, installs dependencies into a private
virtualenv, starts the server, and opens the app. Keep the window open while
you use the app. One-time install:

```bash
curl -fsSL https://raw.githubusercontent.com/murumoto-rgb/Timesheet/main/Timesheet.command -o ~/Desktop/Timesheet.command && chmod +x ~/Desktop/Timesheet.command
```

**Manual:**

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -c requirements.lock
.venv/bin/python -m uvicorn main:app --workers 1
```

Open <http://localhost:8000>, click **Connect QuickBooks**, and authorize. You'll
land back on the form with your projects, employees, and services loaded.

## 4. What needs to exist in the QBO company

For an entry to save, the connected company needs at least:

- **one Employee** (the "who") — the Employee dropdown pulls from these,
- **one Service item** (the "what") — QBO *requires* an item on every time entry,
- **Projects turned on** with at least one project (or just plain customers).

Intuit's sandbox company already has sample data for all three.

## 5. Going live on your real books

Switch `.env` to your **Production** Client ID/Secret and set
`QBO_ENVIRONMENT=production`, then re-connect. Production redirect URIs must be
`https://…`, so if you host this somewhere, register that HTTPS callback URL too.
Intuit may require you to complete an app assessment before granting production
access — check the current requirement in the portal.

## Notes

- Tokens are stored in `qbo_tokens.json`. **Keep it out of git** — the refresh
  token grants access to your books. (A `.gitignore` is included.)
- Billable time requires a project/client. The app stops and explains the issue
  instead of silently saving the entry as non-billable.
- When `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` are configured, server state uses
  the private `qbo_tokens` table instead of local files. Never expose its service
  key in browser code. See the recovery guide before changing storage.
- Run one application instance and one worker. The operation journal supports
  atomic concurrency, but token refresh, reminders and legacy audit/push storage
  still require the single-instance deployment described in the recovery guide.
