# Screen and behavior parity check

Checked 2026-10-08 during the authorized production cutover. Render's last
production commit was `f233cd20e0dd22735d79c7399462ee87e658c4b4`. The owner requires
the narrow phone layout on desktop too. The inherited desktop media block was
present in both the retained Render page and initial Vercel deployment, but
conflicted with that requirement; its widening/two-column rules were removed.

The main page's base styles and markup, phone-specific CSS, colors, controls,
icons, legal pages, service worker and workspace JavaScript remain unchanged
from that Render source. Exceptions are the build label, an isolated disconnected
preview message, the explicit desktop-width correction, and changing the hosting
name in two-factor instructions to Vercel. Production accounting records were
not changed during these checks.

| Screen or flow | Verification |
| --- | --- |
| Log form, recent projects and project/client picker | Live browser opening/closing, unchanged client source; synthetic selection, exact-minute and recent-entry tests |
| Timer, draft restore, multi-day entry, confirmations, edit/delete/undo and bulk selection | Synthetic browser journeys, including uncertainty, stale versions, billed locks and cross-tab recovery; isolated sandbox write acceptance before cutover |
| Week grid, people/day chooser and gap indicators | Live week screen; synthetic cell chooser, totals/gaps and full people-grid journeys |
| Report, periods, filters, comparison, project drill and money/hour display | Live report loading and full 5,612-entry report; synthetic range/filter/compare/capacity tests; exact production response parity |
| Projects directory/search/pins, deep dive and overview | Live directory and project opening; synthetic identity/scoping/search/pin and period behavior |
| Project work/notes, people grid, plan/billing and linked accounting | Live notes and plan/billing panels; synthetic budgets, notes, linked financial scope, CSV/print and failure handling |
| Overview practice/mix/concentration/vendor costs/receivables | Live completed data load without error; synthetic dashboard/mix/capacity tests; exact date-normalized production receivables parity |
| Tools/settings, service categories, local backup/restore | Live controls/category panel without changing preferences; synthetic valid/invalid import and local-state preservation tests |
| Reconcile time and download check | Live read-only period check; synthetic flags, scoped totals and unresolved-operation checks |
| Password/TOTP login, setup and disconnected QuickBooks | Real login passed; no-session/invalid-password/missing-TOTP requests rejected; synthetic phone/desktop setup, connection, MFA fields and invalid-login display |
| Activity log | Live authenticated page; imported audit JSON matched Render; journal/audit tests |
| Two-factor helper | Live page opened without saving a new secret; hosting instructions adapted, layout and existing configuration preserved |
| Rate diagnostics | Live authenticated read-only page; rate/quantity tests |
| Privacy/EULA | Live pages opened; unchanged static source |
| Reminders and installed phone app | Source/keys and cron safeguards preserved; old-origin subscription backed up and removed from new storage. Device delivery/enrollment testing was waived; re-enrollment on the new origin is required |

Responsive checks cover 320px, 390px and 1280px, and a true desktop browser
context. Main views, project details and reconciliation stay within the 520px
phone canvas; the navigation stays centered and within that canvas. Report,
project-directory and overview containers remain single-column. Live desktop
readback at a 1728px viewport measured 520px content and navigation widths.
All 79 existing frontend tests and the backend suite passed; two additional
hosting-screen tests passed separately and are included in final CI.

Private live responses, DOM records, screenshots and static-asset comparison
are outside Git. Checks on live books were read-only. This is browser/test
verification, not a claim of physical-phone notification delivery or inspection
of every device's browser-local pending saves. Retain old-origin local state;
see MIGRATION_CUTOVER_STATE.md for recovery and operational boundaries.
