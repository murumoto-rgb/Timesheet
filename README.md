# QBO Timesheet

A tiny single-user mobile web app: pick a project/client, enter hours + minutes,
and the entry is written straight into QuickBooks Online as a Time Activity with
the right employee and service item. It also shows your recent entries (with
delete) and a this-week total per client.

On a phone, open the app in the browser and use **Add to Home Screen** — it
installs as a standalone app with its own icon.

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
pip install -r requirements.txt
uvicorn main:app --reload
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
- The billable checkbox only applies when a project/client is selected. When
  billable, QBO uses the service item's rate unless you extend the form to send
  `hourly_rate`.
- To move token storage to Supabase/Postgres later, swap the two functions
  `_load_tokens` / `_save_tokens` in `main.py`.
