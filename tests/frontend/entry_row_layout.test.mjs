import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { launch, openApp } from "./harness.mjs";

const pad = (n) => String(n).padStart(2, "0");
const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const today = () => ymd(new Date());

const row = (o) => ({ employee: "Murat Baykal", employeeId: "55", nameOf: "Employee",
  date: today(), hours: 1, minutes: 0, itemId: "5", service: "PR", customerId: "10",
  billable: true, billableStatus: "Billable", hourlyRate: 250,
  description: "Communications, document review and preparation.", ...o });
const base = (entries) => ({ entries, employees: [{ id: "55", name: "Murat Baykal" }],
  items: [{ id: "5", name: "PR" }], projects: { projects: [], clients: [{ id: "10", name: "X" }] } });

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser.close(); });

// A long client name must truncate and keep its billable/billed badge on the
// SAME line as the name (it used to wrap and collide with the duration).
for (const status of ["Billable", "HasBeenBilled"]) {
  test(`long client name keeps its ${status} badge inline`, async () => {
    const entries = [row({ id: "1", billableStatus: status,
      customer: "Hutchinson Residence Structural Assessment and Report #99881" })];
    const { ctx, page, errors } = await openApp(browser, base(entries));
    await page.waitForSelector("#entries .entry .bill");
    const m = await page.$eval("#entries .entry", (r) => {
      const who = r.querySelector(".who"), bill = r.querySelector(".bill");
      return { whoTop: who.offsetTop, billTop: bill.offsetTop,
               truncated: who.scrollWidth > who.clientWidth + 1 };
    });
    assert.ok(Math.abs(m.whoTop - m.billTop) <= 4, "badge sits on the same line as the name");
    assert.ok(m.truncated, "the long name is truncated with an ellipsis");
    assert.deepEqual(errors, []);
    await ctx.close();
  });
}
