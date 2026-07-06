import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { launch, openApp } from "./harness.mjs";

const pad = (n) => String(n).padStart(2, "0");
const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const today = () => ymd(new Date());
const lastYearToday = () => { const d = new Date(); d.setFullYear(d.getFullYear() - 1); return ymd(d); };

const MB = { employee: "Murat Baykal", employeeId: "55", nameOf: "Employee" };
const entry = (o) => ({ ...MB, hours: 0, minutes: 0, itemId: "5", service: "PR",
  customer: "A", customerId: "10", projectId: null, billable: true,
  billableStatus: "Billable", hourlyRate: 250, ...o });
const base = (entries) => ({ entries, employees: [{ id: "55", name: "Murat Baykal" }],
  items: [{ id: "5", name: "PR" }], projects: { projects: [], clients: [{ id: "10", name: "A" }] } });

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser.close(); });

test("report year-over-year: shown for Month, hidden for Day", async () => {
  const entries = [
    entry({ id: "cur", hours: 10, date: today() }),
    entry({ id: "prev", hours: 8, date: lastYearToday() }),
  ];
  const { ctx, page, errors } = await openApp(browser, base(entries), "report");
  await page.click("#reportView .seg button[data-unit=month]");
  await page.waitForTimeout(500);
  assert.equal(await page.isVisible("#repYoY"), true);
  const txt = (await page.textContent("#repYoY")).trim();
  assert.match(txt, /vs last year/);
  assert.match(txt, /25%/);                       // (10 − 8) / 8
  await page.click("#reportView .seg button[data-unit=day]");
  await page.waitForTimeout(300);
  assert.equal(await page.isVisible("#repYoY"), false);
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("week grid colours billed time amber, unbilled blue", async () => {
  const entries = [
    entry({ id: "B", hours: 3, date: today(), customer: "Acme", customerId: "10", billableStatus: "HasBeenBilled" }),
    entry({ id: "N", hours: 1, date: today(), customer: "Beta", customerId: "20", billableStatus: "Billable" }),
  ];
  const data = base(entries);
  data.projects.clients = [{ id: "10", name: "Acme" }, { id: "20", name: "Beta" }];
  const { ctx, page } = await openApp(browser, data, "week");
  await page.waitForSelector("#wgGrid .wg-cell.has");
  const cells = await page.$$eval("#wgGrid .wg-cell.has", (els) =>
    els.map((c) => ({ t: c.textContent, billed: c.classList.contains("billed") })));
  const billed = cells.find((c) => c.t === "3:00");
  const unbilled = cells.find((c) => c.t === "1:00");
  assert.ok(billed && billed.billed, "the 3:00 billed cell should be amber (.billed)");
  assert.ok(unbilled && !unbilled.billed, "the 1:00 unbilled cell should not be .billed");
  await ctx.close();
});
