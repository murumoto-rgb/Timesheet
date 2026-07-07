import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { launch, openApp } from "./harness.mjs";

const pad = (n) => String(n).padStart(2, "0");
const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
// a date inside the current month, and the same day one month prior
const thisMonth = () => { const d = new Date(); d.setDate(10); return ymd(d); };
const prevMonth = () => { const d = new Date(); d.setDate(10); d.setMonth(d.getMonth() - 1); return ymd(d); };

const MB = { employee: "Murat Baykal", employeeId: "55", nameOf: "Employee" };
const e = (o) => ({ ...MB, itemId: "5", service: "PR", customer: "Acme", customerId: "10",
  billable: true, billableStatus: "Billable", hourlyRate: 250, minutes: 0, ...o });
const data = { employees: [{ id: "55", name: "Murat Baykal" }], items: [{ id: "5", name: "PR" }],
  projects: { projects: [], clients: [{ id: "10", name: "Acme" }] },
  entries: [e({ id: "1", date: thisMonth(), hours: 10 }), e({ id: "2", date: prevMonth(), hours: 8 })] };

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser.close(); });

test('"vs prev" compares this month to last month', async () => {
  const { ctx, page, errors } = await openApp(browser, data, "report");
  await page.click('#seg button[data-unit="month"]');
  await page.waitForTimeout(300);
  await page.click('#repCompare button[data-cmp="prev"]');
  await page.waitForTimeout(300);
  const txt = (await page.textContent("#repYoY")).trim();
  // this month 10h vs last month 8h → +25%, "vs previous month"
  assert.match(txt, /vs previous month/);
  assert.match(txt, /25%/);
  assert.match(txt, /8:00 → 10:00/);
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("comparison is hidden for the Day view", async () => {
  const { ctx, page } = await openApp(browser, data, "report");
  await page.click('#seg button[data-unit="day"]');
  await page.waitForTimeout(200);
  assert.equal(await page.isVisible("#repYoY"), false);
  assert.equal(await page.isVisible("#repCompare"), false);   // toggle hidden too
  await ctx.close();
});
