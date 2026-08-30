# Using the project workspace

## Find and review a project

Choose **Projects** in the bottom bar. Search by name and use the star to keep
frequent projects in **Pinned only**. Projects with identical names are kept
separate by their QuickBooks IDs; parent clients are shown for context.

Open a project and use **Overview**, **Work & notes**, or **Plan & billing** to
jump to the section you need. Date controls apply to the whole deep dive.
**Copy link** preserves the project and dates and still requires normal sign-in.
Use the back button above the project name to return to where you opened it.

## Five additions

| Addition | Where to find it | What to expect |
| --- | --- | --- |
| Reconciliation | Tools & settings → Reconcile time | A fresh QuickBooks time read compared with an app snapshot, plus duplicate/consistency flags and save receipts. Checks never change records. Retrying an uncertain save requires an explicit review. |
| Project budgets and fees | Project → Plan & billing | Enter planned labor hours, an agreed USD fee, or both. The hours comparison uses only the budget's exact dates and excludes expense quantities. A fee is a planning reference, not revenue or profit. |
| Invoice and payment history | Project → Load accounting history | Exact project invoice relationships and unambiguous payment allocations, with dates and currencies. Parent-client invoices and ambiguous allocations are excluded, not guessed. Both the invoice and payment must fall within the selected dates. |
| Local drafts and timer | Log | Unfinished fields stay on this browser. After reloading, choose Restore draft. Stop the timer, review the duration and fields, then choose Log time. No background or automatic posting. |
| Project exports | Project → Take this report with you | Download CSV, or choose Print / save PDF and select Save as PDF in your browser's print window. Exports include every project entry in the date/expense scope, even when a notes search is active. |

## Safer editing and recovery

Durations retain their exact minutes; editing notes on a 15-minute entry does
not turn it into a 30-minute entry. Billed time remains read-only. Updates,
deletes and Undo use the entry version that was reviewed. If QuickBooks has
changed it, reload and review the new record before making another change.

Multi-day and bulk changes show a review before posting and keep an outcome for
each record. If only some succeed, use **Retry unconfirmed entries only**. Do not
re-enter successful days. Original save IDs survive reloads and are reused.

If a save result is uncertain, keep the browser's site data. Open **Reconcile
time** to review the original request, or retry the unchanged form. Do not create
a replacement entry, clear site data, or change the original request merely to
bypass the warning. A stale company tab is blocked from saving until reloaded.

## What stays local

Drafts, timers, planning budgets and service-category overrides are specific to
this company on this browser. They are not shared across devices or written into
QuickBooks. **Tools & settings → Back up plans & drafts** downloads a private JSON
backup; use **Restore plans & drafts** only for the same company. Save IDs are deliberately not
transferred by this export. Use a trusted device and protect downloaded reports.

There is one current Log draft per company in each browser. Use one Log tab when
editing an unfinished draft; drafts are not synchronized between devices.

Expense categories can be reviewed under **Tools & settings → Service categories**.
This corrects how this browser classifies service quantities without renaming or
editing QuickBooks items. Unknown rates remain unknown; an explicit zero rate is
shown as zero. Time values, invoices and cash are labeled separately.

The reconciliation report is a second read of time entities, not an independent
accounting report or proof that historical data is error-free. Invoice history
does not establish profitability, and mixed currencies are never added together.
