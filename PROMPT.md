# Starting a coding task

Use this prompt with your concrete requested change. `CLAUDE.md` imports the
shared `AGENTS.md`; other coding agents should read `AGENTS.md` directly.

## Prompt to paste

> Implement the following change: [describe the desired behavior and what a
> successful result looks like]. Read AGENTS.md and the task-relevant sections
> of README.md and docs/AGENT_REFERENCE.md. Check the existing implementation
> before treating a historical backlog item as unfinished. Verify current Intuit
> requirements before changing QBO request fields or authentication behavior.
>
> Preserve billed-entry immutability, exact duration, company/version checks,
> and durable retry handling. Keep private data and credentials out of Git and
> logs. Development permission does not authorize changes to real QuickBooks books.
>
> Resolve routine implementation choices, run focused checks, fix failures caused
> by the change, and complete the PR workflow. Report the changed behavior,
> validation evidence, limits, and any precise manual action I must take.

## Setup and live verification

Use README.md when setup or OAuth connection is actually part of the request.
Use synthetic fixtures for local tests. A live sandbox write needs an explicitly
identified sandbox and requested test; a successful sandbox test does not authorize
switching to production keys or writing to real books. Never repeat onboarding or
rebuild an existing feature merely because an old kickoff prompt listed it.
