import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { launch, openApp } from "./harness.mjs";

const pad = (n) => String(n).padStart(2, "0");
const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const daysAgo = (n) => { const d = new Date(); d.setDate(d.getDate() - n); return ymd(d); };

const MB = { employee: "Murat Baykal", employeeId: "55", nameOf: "Employee" };
const entry = (o) => ({ ...MB, hours: 0, minutes: 0, itemId: "5", service: "PR",
  billable: true, billableStatus: "Billable", hourlyRate: 250, date: daysAgo(3), ...o });
const data = (entries) => ({ entries, employees: [{ id: "55", name: "Murat Baykal" }],
  items: [{ id: "5", name: "PR" }],
  projects: { projects: [], clients: [{ id: "10", name: "Acme" }, { id: "20", name: "Beta" }] } });

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser.close(); });

test("Totals tab: project landing → drill-down → back", async () => {
  const entries = [
    entry({ id: "1", customer: "Acme", customerId: "10", hours: 6 }),
    entry({ id: "2", customer: "Beta", customerId: "20", hours: 3 }),
  ];
  const { ctx, page, errors } = await openApp(browser, data(entries), "people");
  // landing: one row per project, alphabetical
  await page.waitForSelector("#peoBody .tot-row");
  const names = await page.$$eval("#peoBody .tot-row .nm span:first-child", (els) => els.map((e) => e.textContent.trim()));
  assert.deepEqual(names, ["Acme", "Beta"]);
  // drill into the first project → leaderboard with a back button
  await page.click("#peoBody .tot-row");
  await page.waitForSelector("#peoBack");
  const drillText = await page.textContent("#peoBody");
  assert.match(drillText, /Murat Baykal/);
  // back → landing again
  await page.click("#peoBack");
  await page.waitForSelector("#peoBody .tot-row");
  assert.equal((await page.$$("#peoBody .tot-row")).length, 2);
  assert.deepEqual(errors, []);
  await ctx.close();
});
