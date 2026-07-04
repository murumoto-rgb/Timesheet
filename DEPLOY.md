# DEPLOY — put the app on the web (phone-ready) and connect your real company

Two parts, in order. Part 1 gets the app on the internet (still sandbox).
Part 2 switches it to your real QuickBooks company.

Cost: Render's Starter plan is ~$7/month plus ~$0.25/month for the 1GB disk
that keeps your QuickBooks connection alive across updates. In exchange the
app is always-on: no cold starts, opens instantly on your phone.

(A $0 route exists — Render free tier + Supabase for token storage, which the
app also supports — but it sleeps after 15 idle minutes and wakes in ~1 min.)

---

## Part 1 — Host on Render (browser only, ~15 min)

1. Go to **https://render.com** → **Get Started** → **Sign in with GitHub**.
   Authorize Render; when asked which repositories it may access, grant access
   to **murumoto-rgb/Timesheet** (all repos is fine too).
2. In the Render dashboard click **New +** (top right) → **Blueprint**.
3. Pick the **Timesheet** repository. Set the branch to **main**. Render reads
   `render.yaml` and shows a service called **qbo-timesheet**.
4. It will prompt for the secret environment variables:
   - `QBO_CLIENT_ID` / `QBO_CLIENT_SECRET` — your **Development** keys for now
     (same ones as in your Mac's `.env`).
   - `QBO_REDIRECT_URI` — leave a placeholder like `x` for now; you'll set the
     real value in step 6 once you know the app's URL.
   - `APP_PASSWORD` — invent a good password. This is what you'll type on your
     phone to use the app. Anyone without it gets nothing.
5. Click **Apply / Deploy** and wait for the first deploy to go green
   (a few minutes). Note your app's URL, e.g. `https://qbo-timesheet.onrender.com`
   (yours may have a suffix).
6. In the service's **Environment** tab, set
   `QBO_REDIRECT_URI` = `https://YOUR-APP-URL/callback` (exactly, no trailing
   slash). Save — Render redeploys automatically.
7. Register that same callback with Intuit: **developer.intuit.com → your app →
   Settings → Redirect URIs → Add** `https://YOUR-APP-URL/callback` (keep the
   localhost one too). Save.
8. Open the app URL in a browser → enter your `APP_PASSWORD` → **Connect
   QuickBooks** → pick the **sandbox** company. Log a test entry. If this
   works, hosting is done.
9. **On your phone**: open the URL, sign in, then use *Share → Add to Home
   Screen* (iPhone) or *⋮ → Add to home screen* (Android). You get an app
   icon that opens full-screen.

From now on every push to `main` auto-deploys in ~2 minutes. The Mac
launcher keeps working for local use, but the phone URL is the main app.

## Part 2 — Switch to your real QuickBooks company

Intuit requires a one-time compliance questionnaire before granting
production keys to any app — including private ones. For a private app it's
a ~20–30 minute self-service form, not the months-long review that App Store
apps go through.

1. **developer.intuit.com → your app → Keys and credentials → toggle to
   "Production"**. It will walk you through:
   - **App details** — name, and URLs for a EULA and privacy policy. For a
     private app, your company website (baykalconsulting.com) is acceptable
     for both.
   - **Compliance questionnaire** — answer honestly; when asked, say it's a
     **private app for your own business** with ~1 user. Data handling
     questions: tokens and data stay on your own server; no third parties.
2. When approved, the **Production Client ID and Client Secret** appear on the
   Production side of Keys and credentials.
3. Add the redirect URI on the **Production** side too:
   `https://YOUR-APP-URL/callback`. (Production URIs must be https.)
4. In Render → **Environment**, change:
   - `QBO_CLIENT_ID` / `QBO_CLIENT_SECRET` → the **production** values
   - `QBO_ENVIRONMENT` → `production`
   Save; it redeploys.
5. Open the app → the badge in the top-right now reads **PRODUCTION** (in
   green). Click **Connect QuickBooks**, sign in with the Intuit account that
   owns your real company, pick the company, authorize.
6. Your real clients/projects, employees, and service items now populate the
   form. Log one small test entry, confirm it in QuickBooks (Reports → Time
   Activities by Employee Detail), then delete it from the app.

### What your real company must have

- At least one **Employee** (Payroll → Employees — no payroll subscription
  needed, just the record) and one **Service item** (Sales → Products &
  services).
- **Projects** require QBO Plus, Advanced, or Enterprise Suite. On lower plans
  the app still works — you just pick plain clients instead of projects.

### Safety notes

- The sandbox and production connections are separate; switching env vars
  swaps which keys/company the app talks to (the connection tokens live on
  the Render disk at `/data/qbo_tokens.json`). After switching environments,
  reconnect once via the Connect QuickBooks button.
- Every write the app makes is a single TimeActivity you can see and delete.
  It never touches invoices, payments, or anything else.
- If you ever want to cut access: QuickBooks → ⚙ → Apps → your app →
  Disconnect (or rotate the client secret in the developer portal).
