# QBO Timesheet agent guidance

This single-user FastAPI/static-HTML application can write QuickBooks time entries.
Development and tests do not authorize writes to real books; use synthetic fixtures
or an explicitly authorized sandbox. Keep unrelated local work and private data safe.

## Task-specific references

- Use `README.md` for setup and `docs/RELEASE_AND_RECOVERY.md` for validation,
  backup, recovery or release work.
- Read the relevant sections of `docs/AGENT_REFERENCE.md` when changing QBO OAuth,
  API requests, time-entry behavior, reports or dashboard features. Recheck current
  provider requirements before changing integration behavior; stored notes are evidence.
- Use `docs/WORKSPACE_GUIDE.md` when changing navigation/workspace behavior.
  Load skills only when their specific workflow matches the task.

## Data and write invariants

- Billed (`HasBeenBilled`) time is immutable. Never force through an edit/delete by
  replacing the caller's stale version with a newer one.
- Preserve the company key, operation UUID, payload and version across retries.
  Keep uncertain creates reserved in the durable journal; receipts bind to company,
  API environment and payload. Preserve local locking and checked revision CAS.
- Preserve exact whole minutes. A notes-only edit must not round existing duration.
- Keep project/person identities distinct. Preserve unknown rates as unknown and
  separate time value, billed time and received cash; never show hours as dollars.
- Deployment remains one instance/worker until the narrower token/audit/push state
  contract is deliberately changed and verified.

## Conventions

- Keep it single-user and dependency-light. No auth framework (just the
  APP_PASSWORD cookie gate, plus optional TOTP two-factor via `TOTP_SECRET` —
  pure-stdlib RFC 6238, enroll at `/mfa-setup`).
- Token persistence is isolated to `_load_tokens` / `_save_tokens` in `main.py`:
  local JSON file by default, Supabase table (`qbo_tokens`) when
  `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` are set (diskless Render free tier).
- Surface useful QBO validation messages and support IDs without exposing tokens.
- Never commit `.env` or `qbo_tokens.json`.
- **Build number**: `#buildInfo` in `index.html`'s footer shows `build
  YYYY.MM.DD.N` (single source of truth — one string in the HTML). BUMP it
  on every app-changing push (increment N same-day, or roll the date). State
  the build number in an app release handoff so the user can identify it.
  Documentation/instruction-only changes do not require a build bump.

## Completion

Use a focused branch from fresh `origin/main`, stage only task files, and finish
through a PR and merge after applicable checks pass. Work directly for clear tasks;
delegate bounded work only when it improves the result. Resolve routine choices
and failures caused by the change without repeated approval requests.

Run focused checks for changed behavior and required repository checks. For
instruction/documentation-only changes, check references, consistency and the diff;
do not connect to QuickBooks, change the app build, or perform a release exercise.
Repeat or broaden checks only for new changes, failures or unresolved risks. Report
what was verified, remaining limits, and the smallest owner action for any blocker.
