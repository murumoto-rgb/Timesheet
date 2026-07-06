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
  const { ctx, page, errors } = await openApp(browser, data(entries), "totals");
  // landing: one row per project, alphabetical
  await page.waitForSelector("#totBody .tot-row");
  const names = await page.$$eval("#totBody .tot-row .nm span:first-child", (els) => els.map((e) => e.textContent.trim()));
  assert.deepEqual(names, ["Acme", "Beta"]);
  // drill into the first project → leaderboard with a back button
  await page.click("#totBody .tot-row");
  await page.waitForSelector("#totBack");
  const drillText = await page.textContent("#totBody");
  assert.match(drillText, /Murat Baykal/);
  // back → landing again
  await page.click("#totBack");
  await page.waitForSelector("#totBody .tot-row");
  assert.equal((await page.$$("#totBody .tot-row")).length, 2);
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("Totals tab: granularity segment + period nav are wired", async () => {
  const entries = [entry({ id: "1", customer: "Acme", customerId: "10", hours: 6 })];
  const { ctx, page, errors } = await openApp(browser, data(entries), "totals");
  await page.waitForSelector("#totBody .tot-row");
  // switch granularity to Week → that button becomes active and the view re-renders
  await page.click("#totSeg button[data-unit=week]");
  await page.waitForTimeout(250);
  const weekActive = await page.$eval("#totSeg button[data-unit=week]", (b) => b.classList.contains("on"));
  assert.ok(weekActive, "Week segment should activate");
  // page back a period — must not error, and the label updates
  const before = await page.textContent("#totLabel");
  await page.click("#totPrev");
  await page.waitForTimeout(250);
  const after = await page.textContent("#totLabel");
  assert.notEqual(before, after, "prev should move the period label");
  assert.deepEqual(errors, []);
  await ctx.close();
});
