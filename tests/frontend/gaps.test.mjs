import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { launch, openApp } from "./harness.mjs";

const pad = (n) => String(n).padStart(2, "0");
const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

// Monday of the current week + the past-or-today weekdays in it.
function weekInfo() {
  const t = new Date(); t.setHours(0, 0, 0, 0);
  const monday = new Date(t); monday.setDate(t.getDate() - ((t.getDay() + 6) % 7));
  const pastWeekdays = [];
  for (let i = 0; i < 7; i++) {
    const d = new Date(monday); d.setDate(monday.getDate() + i);
    if (d.getDay() >= 1 && d.getDay() <= 5 && d <= t) pastWeekdays.push(ymd(d));
  }
  return { pastWeekdays };
}

const MB = { employee: "Murat Baykal", employeeId: "55", nameOf: "Employee" };
const e = (date, hours) => ({ ...MB, id: date, date, hours, minutes: 0, itemId: "5", service: "PR",
  customer: "Acme", customerId: "10", billable: true, billableStatus: "Billable", hourlyRate: 250 });
const base = (entries) => ({ entries, employees: [{ id: "55", name: "Murat Baykal" }],
  items: [{ id: "5", name: "PR" }], projects: { projects: [], clients: [{ id: "10", name: "Acme" }] } });

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser.close(); });

test("under-logged weekdays are flagged in the header + summary", async () => {
  const { pastWeekdays } = weekInfo();
  // one small (2h < 6h target) entry so the grid renders; every past weekday is under target
  const { ctx, page, errors } = await openApp(browser, base([e(pastWeekdays[0], 2)]), "week");
  await page.waitForSelector("#wgGrid .wg-head");
  const gapCells = (await page.$$("#wgGrid .wg-head .wg-cell.gap")).length;
  assert.equal(gapCells, pastWeekdays.length, "each under-logged past weekday is flagged");
  assert.equal(await page.isVisible("#wgGaps"), true);
  assert.match(await page.textContent("#wgGaps"), /gap/);
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("logging the target on every past weekday clears all gaps", async () => {
  const { pastWeekdays } = weekInfo();
  const entries = pastWeekdays.map((d) => e(d, 8));   // 8h ≥ 6h target
  const { ctx, page } = await openApp(browser, base(entries), "week");
  await page.waitForSelector("#wgGrid .wg-head");
  assert.equal((await page.$$("#wgGrid .wg-head .wg-cell.gap")).length, 0);
  assert.equal(await page.isVisible("#wgGaps"), false);
  await ctx.close();
});
