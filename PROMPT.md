# Kickoff prompt for Claude Code

Open this folder in Claude Code and paste the prompt below as your first message.
`CLAUDE.md` will be loaded automatically as project context — it holds all the
verified QuickBooks Online API facts, so trust it over any assumptions.

---

## Prompt to paste

> This is a single-user app that logs time entries directly into QuickBooks
> Online. A working scaffold already exists: `main.py` (FastAPI backend),
> `index.html` (the whole frontend), plus `requirements.txt`, `.env.example`,
> and `README.md`. Read `CLAUDE.md` first — it has the verified QBO API details
> (OAuth endpoints, the exact `TimeActivity` payload, the ProjectRef + CustomerRef
> rule, the required `ItemRef`, token rotation, sandbox vs production). Treat those
> as ground truth and don't change field names without checking Intuit's docs.
>
> First, help me get it running end to end:
> 1. Walk me through creating the app in the Intuit Developer portal and filling
>    in `.env` (I'll do the portal clicks; you tell me exactly what to enter).
> 2. Get the OAuth connect flow working against the **sandbox** company and
>    confirm the projects, employees, and service items load in the form.
> 3. Post one test time entry and verify it appears in the QBO sandbox UI.
>
> Then work through the backlog in `CLAUDE.md`, starting with a **recent-entries
> list that I can delete from**. Keep the app single-user and dependency-light —
> no database, no auth framework — and keep all QBO token handling behind the
> existing `_load_tokens` / `_save_tokens` seam so I can move it to Supabase later.
>
> Before writing code for each task, tell me your plan in a sentence or two.

---

## Notes

- Start with `QBO_ENVIRONMENT=sandbox`. Only switch to production keys once the
  sandbox round-trip works.
- If a dropdown is empty, the fix is almost always in the QBO company itself:
  add an Employee, add a Service item, or enable Projects.
