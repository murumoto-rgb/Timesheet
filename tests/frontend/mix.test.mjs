import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { launch, openApp, moneyStats } from "./harness.mjs";

const pad = (n) => String(n).padStart(2, "0");
const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const daysAgo = (n) => { const d = new Date(); d.setDate(d.getDate() - n); return ymd(d); };

const MB = { employee: "Murat Baykal", employeeId: "55", nameOf: "Employee" };
const e = (o) => ({ ...MB, itemId: "5", service: "PR", customer: "Acme", customerId: "10",
  hourlyRate: 250, minutes: 0, date: daysAgo(5), ...o });
const base = (entries) => ({ entries, employees: [{ id: "55", name: "Murat Baykal" }],
  items: [{ id: "5", name: "PR" }], projects: { projects: [], clients: [{ id: "10", name: "Acme" }] } });

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser.close(); });

test("billable-mix card shows the billable % and excludes mileage", async () => {
  const entries = [
    e({ id: "1", hours: 15, billable: true, billableStatus: "Billable" }),         // billable
    e({ id: "2", hours: 5, billable: false, billableStatus: "NotBillable" }),       // non-billable
    e({ id: "3", hours: 40, service: "Mileage", itemId: "7", billable: true, billableStatus: "Billable", hourlyRate: 1 }), // excluded
  ];
  const { ctx, page, errors } = await openApp(browser, base(entries), "dash");
  await page.waitForSelector(".money .mix-bars");
  const s = await moneyStats(page, "Billable mix");
  // 15 billable / 20 total = 75% (mileage's 40h excluded)
  assert.equal(s["Billable"], "75%");
  assert.equal(s["Billable hrs"], "15:00");
  assert.equal(s["Non-billable hrs"], "5:00");
  const bars = await page.$$eval(".mix-bars .mix-bar", (b) => b.length);
  assert.equal(bars, 12, "one stacked bar per month bucket");
  assert.deepEqual(errors, []);
  await ctx.close();
});
