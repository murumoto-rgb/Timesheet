import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { launch, openApp } from "./harness.mjs";

const pad = (n) => String(n).padStart(2, "0");
const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const today = ymd(new Date());

const MB = { employee: "Murat Baykal", employeeId: "55", nameOf: "Employee" };
const entry = (o) => ({ ...MB, hours: 0, minutes: 0, itemId: "5", service: "PR",
  billable: true, billableStatus: "Billable", hourlyRate: 250, date: today, ...o });
const data = (entries) => ({ entries, employees: [{ id: "55", name: "Murat Baykal" }],
  items: [{ id: "5", name: "PR" }],
  projects: { projects: [], clients: [{ id: "10", name: "Acme" }, { id: "20", name: "Beta" }] } });

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser.close(); });

// The per-project drill-down used to be its own "Totals" tab; it now lives inside
// Report — tapping a "By project / client" row opens it in place of the report body.
test("Report drill: tap a project → per-person leaderboard → back", async () => {
  const entries = [
    entry({ id: "1", customer: "Acme", customerId: "10", hours: 6 }),
    entry({ id: "2", customer: "Beta", customerId: "20", hours: 3 }),
  ];
  const { ctx, page, errors } = await openApp(browser, data(entries), "report");
  // the "By project / client" breakdown lists both projects (value desc: Acme first)
  await page.waitForSelector("#projRows .proj-row.tappable");
  const names = await page.$$eval("#projRows .proj-row .n", (els) => els.map((e) => e.textContent.trim()));
  assert.deepEqual(names, ["Acme", "Beta"]);
  // tap Acme → the drill replaces the report body (main hidden, drill shown)
  await page.click("#projRows .proj-row");
  await page.waitForSelector("#repDrillBack");
  assert.equal(await page.isVisible("#repMain"), false, "normal report body is hidden while drilled");
  assert.equal(await page.isVisible("#repDrill"), true);
  const drillText = await page.textContent("#repDrill");
  assert.match(drillText, /Acme/);
  assert.match(drillText, /Murat Baykal/);
  // back → the report overview is shown again with the breakdown intact
  await page.click("#repDrillBack");
  await page.waitForSelector("#projRows .proj-row");
  assert.equal(await page.isVisible("#repMain"), true);
  assert.equal(await page.isVisible("#repDrill"), false);
  assert.equal((await page.$$("#projRows .proj-row")).length, 2);
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("Report drill: full-grid toggle + period seg stays usable while drilled", async () => {
  const entries = [entry({ id: "1", customer: "Acme", customerId: "10", hours: 6 })];
  const { ctx, page, errors } = await openApp(browser, data(entries), "report");
  await page.waitForSelector("#projRows .proj-row");
  await page.click("#projRows .proj-row");
  await page.waitForSelector("#totToggle");
  // grid is collapsed until toggled
  assert.equal((await page.$$("#repDrill .ptbl")).length, 0);
  await page.click("#totToggle");
  await page.waitForSelector("#repDrill .ptbl");
  // the period segment is still visible/usable; switching unit keeps the drill open
  assert.equal(await page.isVisible("#seg"), true);
  await page.click("#seg button[data-unit=month]");
  await page.waitForTimeout(250);
  assert.equal(await page.isVisible("#repDrill"), true, "changing the period keeps the drill open");
  assert.equal(await page.isVisible("#repDrillBack"), true);
  assert.deepEqual(errors, []);
  await ctx.close();
});
