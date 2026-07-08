import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { launch, openApp } from "./harness.mjs";

const pad = (n) => String(n).padStart(2, "0");
const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const daysAgo = (n) => { const d = new Date(); d.setDate(d.getDate() - n); return ymd(d); };

const MB = { employee: "Murat Baykal", employeeId: "55", nameOf: "Employee" };
const e = (o) => ({ ...MB, hours: 1, minutes: 0, itemId: "5", service: "PR", customer: "Acme",
  customerId: "10", billable: true, billableStatus: "Billable", hourlyRate: 250, ...o });
const data = {
  employees: [{ id: "55", name: "Murat Baykal" }], items: [{ id: "5", name: "PR" }],
  projects: { projects: [], clients: [{ id: "10", name: "Acme" }] },
  entries: [
    e({ id: "1", date: daysAgo(10), description: "site visit" }),
    e({ id: "2", date: daysAgo(11), description: "site visit" }),
    e({ id: "3", date: daysAgo(12), description: "site visit" }),
    e({ id: "4", date: daysAgo(13), description: "report drafting" }),
  ],
};

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser.close(); });

test("#2 selecting a project leads the notes dropdown with its frequent descriptions", async () => {
  const { ctx, page, errors } = await openApp(browser, data);   // Log default view
  await page.waitForTimeout(600);                               // let loadFrequents populate notes
  await page.click("#projectBtn");                              // open the picker
  await page.locator('#pickerList .pick-item', { hasText: "Acme" }).first().click();
  await page.waitForTimeout(150);
  const groups = await page.$$eval("#template optgroup", (gs) =>
    gs.map((g) => ({ label: g.label, opts: [...g.querySelectorAll("option")].map((o) => o.textContent) })));
  assert.equal(groups[0].label, "Frequent on this project");
  assert.equal(groups[0].opts[0], "site visit");               // most-used first
  assert.ok(groups[0].opts.includes("report drafting"));
  // standard templates still available in a second group
  assert.ok(groups.length >= 2 && groups[1].label === "Standard");
  assert.deepEqual(errors, []);
  await ctx.close();
});
