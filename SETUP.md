# SETUP — from zero to your first sandbox time entry

Follow these in order. Parts A and B happen in your web browser, Part C gets the
code onto your computer, Parts D–F run and test it. Nothing here touches your
real QuickBooks books — everything is against Intuit's fake "sandbox" company.

---

## Part A — Create your Intuit developer account (browser)

1. Open **https://developer.intuit.com** in your browser.
2. Click **Sign In** (top-right corner).
3. Sign in with the **same Intuit account you use for QuickBooks Online**
   (same email + password). Don't create a separate account.
4. The first time, Intuit asks you to complete a **developer profile** — your
   name, country, and a company address. This is required; fill it in and continue.
5. You now land on the developer **Dashboard**. Intuit automatically created a
   **sandbox company** for you (a fake QuickBooks company pre-filled with sample
   customers, employees, and items). You'll see it later.

## Part B — Create the app and get its keys (browser)

1. On the Dashboard, click **Create an app** (big button, sometimes shown as **+**).
2. If asked which platform: choose **QuickBooks Online and Payments**.
3. Give the app a name, e.g. `Baykal Timesheet`. (Nobody but you sees this.)
4. When asked which **scope** the app needs, tick
   **Accounting** (`com.intuit.quickbooks.accounting`). Leave Payments unticked.
5. Click **Create app** / **Finish**. You're now inside your app's page.
6. In the app's left-hand menu, under **Development Settings** (may be labeled
   just **Development**), click **Keys & credentials** (older label: **Keys & OAuth**).
   Make sure you are on the **Development** keys tab, not Production.
7. You'll see:
   - **Client ID** — a long string of letters/numbers.
   - **Client Secret** — click the eye icon / **Show** to reveal it.

   Keep this browser tab open — you'll copy both values in Part D.
8. On the same page, scroll down to **Redirect URIs**.
   Click **Add URI** and type **exactly**:

   ```
   http://localhost:8000/callback
   ```

   No trailing slash, no `https`, nothing extra. Click **Save**.

## Part C — Get the code onto your computer

You need **Python 3.10 or newer**. To check:

- **Mac**: press `Cmd+Space`, type `Terminal`, press Enter. In the window type
  `python3 --version` and press Enter.
- **Windows**: press the Windows key, type `PowerShell`, press Enter. Type
  `python --version` and press Enter.

If you get a version like `Python 3.12.x`, you're fine. If you get an error,
install Python from https://www.python.org/downloads/ — on **Windows, tick
"Add python.exe to PATH"** on the first installer screen.

Then get the code. Two options:

**Option 1 — Download ZIP (no git needed)**

1. Open the repository on GitHub: `https://github.com/murumoto-rgb/Timesheet`
2. Click the **branch dropdown** (says `main`) and pick
   `claude/qbo-time-tracking-app-oyrx9x` (or `main` once it's merged).
3. Click the green **Code** button → **Download ZIP**.
4. Unzip it somewhere easy, e.g. your Desktop. You'll get a `Timesheet` folder.
5. Open a terminal **in that folder**:
   - Mac: in Terminal type `cd ` (with a space), drag the folder onto the
     Terminal window, press Enter.
   - Windows: open the folder in File Explorer, click the address bar, type
     `powershell`, press Enter.

**Option 2 — git clone**

```bash
git clone -b claude/qbo-time-tracking-app-oyrx9x https://github.com/murumoto-rgb/Timesheet.git
cd Timesheet
```

## Part D — Create your .env file with the keys

Still in the terminal, in the `Timesheet` folder:

- **Mac**:  `cp .env.example .env`  then  `open -e .env`
- **Windows**:  `Copy-Item .env.example .env`  then  `notepad .env`

A text editor opens. Replace the two placeholder values with the **Client ID**
and **Client Secret** from the browser tab you kept open (Part B step 7):

```
QBO_CLIENT_ID=ABxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
QBO_CLIENT_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
QBO_REDIRECT_URI=http://localhost:8000/callback
QBO_ENVIRONMENT=sandbox
```

Rules: no quotes, no spaces around `=`, one item per line. Leave the last two
lines exactly as shown. **Save and close** the editor.

## Part E — Install and run

In the same terminal:

- **Mac**:
  ```bash
  python3 -m pip install -r requirements.txt
  python3 -m uvicorn main:app
  ```
- **Windows**:
  ```powershell
  python -m pip install -r requirements.txt
  python -m uvicorn main:app
  ```

The install takes a minute. When the server starts you'll see
`Uvicorn running on http://127.0.0.1:8000`. **Leave this window open** —
closing it stops the app.

## Part F — Connect and log a test entry

1. Open **http://localhost:8000** in your browser. You should see the
   Timesheet app with a **Connect QuickBooks** button and a `SANDBOX` badge.
2. Click **Connect QuickBooks**. You're sent to Intuit:
   - sign in if asked,
   - it asks **which company** to connect — pick the **sandbox** company
     (named like *Sandbox Company_US_1*),
   - click **Connect** / **Authorize**.
3. You land back on the form, now with dropdowns filled from the sandbox:
   pick any client, any employee, any service, tap the **1:00** chip,
   type a note like `test entry`, and press **Log time**.
4. You should see green text: `Logged ✓  entry #123`, and the entry appears
   in **Recent entries** below with a delete (×) button.
5. Verify inside QuickBooks itself:
   - Back on developer.intuit.com, open the menu (your icon / hamburger) →
     **Sandboxes** → click **Go to company** next to your sandbox.
   - In the sandbox QuickBooks, go to **Reports** (left menu) and search for
     the report **"Time Activities by Employee Detail"**. Your `test entry`
     should be listed for today.
6. Optional: tap the **×** next to the entry in the app to delete it, and
   re-run the report to confirm it's gone.

That's the full round trip. When this works, the app is fully functional —
going to your real books later is only: switch to **Production** keys in `.env`,
set `QBO_ENVIRONMENT=production`, host the app behind `https`, and register that
`https://…/callback` redirect URI in the portal.

---

## If something goes wrong

| Symptom | Fix |
|---|---|
| Browser error at Intuit: *redirect_uri mismatch / invalid redirect* | The URI in the portal isn't **exactly** `http://localhost:8000/callback`. Re-check Part B step 8 (no trailing `/`), click Save, try again. |
| `invalid_client` after clicking Connect | Client ID/Secret in `.env` are wrong, have stray spaces/quotes, or are the **Production** keys. Copy the **Development** keys again, save `.env`, restart the server (Ctrl+C, then the uvicorn command again). |
| Page says "Server isn't configured yet" | The `.env` file wasn't found or is empty. It must be named exactly `.env` (not `.env.txt`) and sit in the same folder as `main.py`. Restart the server after fixing. |
| `python: command not found` / `not recognized` | Python isn't installed or not on PATH — reinstall from python.org (Windows: tick "Add to PATH"). On Mac use `python3`, on Windows use `python`. |
| `uvicorn: command not found` | Use `python3 -m uvicorn main:app` (Mac) / `python -m uvicorn main:app` (Windows) — the `-m` form always works. |
| Port already in use | Something else is on 8000. Run on 8001: add `--port 8001`, and ALSO add `http://localhost:8001/callback` as a second Redirect URI in the portal and set `QBO_REDIRECT_URI=http://localhost:8001/callback` in `.env`. |
| A dropdown is empty | The sandbox company is missing that record type. In the sandbox QBO UI add an Employee (Payroll → Employees), a Service item (Sales → Products & services → New → Service), or a Customer. |
| Entry fails with a QBO error message | Read the message — it's Intuit's real validation error (e.g. missing service item). The most common: no Service selected, or hours+minutes both 0. |
