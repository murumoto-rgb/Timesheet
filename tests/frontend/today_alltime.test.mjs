import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { launch, openApp } from "./harness.mjs";

const pad = (n) => String(n).padStart(2, "0");
const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const today = () => ymd(new Date());
const daysAgo = (n) => { const d = new Date(); d.setDate(d.getDate() - n); return ymd(d); };

const MB = { employee: "Murat Baykal", employeeId: "55", nameOf: "Employee" };
const e = (o) => ({ ...MB, hours: 1, minutes: 0, itemId: "5", service: "PR", customer: "Acme",
  customerId: "10", billable: true, billableStatus: "Billable", hourlyRate: 250, ...o });
const base = (entries) => ({ entries, employees: [{ id: "55", name: "Murat Baykal" }],
  items: [{ id: "5", name: "PR" }], projects: { projects: [], clients: [{ id: "10", name: "Acme" }] } });

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser.close(); });

test("#1 today strip totals today's time; shows an empty state otherwise", async () => {
  // two entries today (2h + 1h), one older
  const entries = [e({ id: "1", hours: 2, date: today() }), e({ id: "2", hours: 1, date: today() }),
    e({ id: "3", hours: 5, date: daysAgo(9) })];
  const { ctx, page, errors } = await openApp(browser, base(entries));   // Log is the default view
  await page.waitForSelector("#todayStrip");
  const txt = await page.textContent("#todayStrip");
  assert.match(txt, /Today/);
  assert.match(txt, /3:00/);          // 2h + 1h today (the 5h from 9 days ago excluded)
  assert.match(txt, /2 entries/);
  assert.deepEqual(errors, []);
  await ctx.close();

  const empty = await openApp(browser, base([e({ id: "9", hours: 3, date: daysAgo(9) })]));
  await empty.page.waitForSelector("#todayStrip.empty-day");
  assert.match(await empty.page.textContent("#todayStrip"), /No time logged today/);
  await empty.ctx.close();
});

test("#7 all-history search widens the fetch beyond the period", async () => {
  const entries = [e({ id: "old", hours: 2, date: daysAgo(200), description: "retaining wall analysis" })];
  const { ctx, page, errors } = await openApp(browser, base(entries), "report");
  // default is the current week → the 200-day-old entry is not in range
  await page.waitForSelector("#repEntries");
  assert.equal((await page.$$("#repEntries .entry")).length, 0);
  // turn on all-history + search → the old entry surfaces
  await page.click("#filterToggle");
  await page.check("#repAllTime");
  await page.waitForTimeout(200);
  await page.fill("#repQ", "retaining");
  await page.waitForTimeout(200);
  assert.equal((await page.$$("#repEntries .entry")).length, 1);
  assert.match(await page.textContent("#periodLabel"), /All history/);
  assert.deepEqual(errors, []);
  await ctx.close();
});
