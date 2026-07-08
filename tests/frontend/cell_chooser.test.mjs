import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { launch, openApp } from "./harness.mjs";

const pad = (n) => String(n).padStart(2, "0");
const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const today = () => ymd(new Date());

const MB = { employee: "Murat Baykal", employeeId: "55", nameOf: "Employee" };
const e = (o) => ({ ...MB, date: today(), hours: 1, minutes: 0, itemId: "5", service: "PR",
  billable: true, billableStatus: "Billable", hourlyRate: 250, ...o });
const data = (entries) => ({ entries, employees: [{ id: "55", name: "Murat Baykal" }],
  items: [{ id: "5", name: "PR" }], projects: { projects: [], clients: [{ id: "10", name: "Acme" }] } });

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser.close(); });

test("a week cell with several entries opens a chooser; picking one edits it", async () => {
  // two entries on the same project + day → one cell, two entries
  const entries = [
    e({ id: "A", customer: "Acme", customerId: "10", service: "PR", description: "site visit" }),
    e({ id: "B", customer: "Acme", customerId: "10", service: "PR", description: "report drafting" }),
  ];
  const { ctx, page, errors } = await openApp(browser, data(entries), "week");
  await page.waitForSelector("#wgGrid .wg-cell.has");
  await page.click("#wgGrid .wg-cell.has");           // the cell with 2 entries (2:00)
  await page.waitForSelector("#cellChooser", { state: "visible" });
  const rows = await page.$$eval("#cellChooser .chooser-row .cr-desc", (els) => els.map((x) => x.textContent));
  assert.deepEqual(rows.sort(), ["report drafting", "site visit"]);
  // pick one → chooser closes, form is populated, we're on the Log tab
  await page.click("#cellChooser .chooser-row");
  await page.waitForTimeout(200);
  assert.equal(await page.isVisible("#cellChooser"), false);
  assert.equal(await page.isVisible("#logView"), true);
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("a single-entry cell edits directly (no chooser)", async () => {
  const entries = [e({ id: "A", customer: "Acme", customerId: "10", description: "solo" })];
  const { ctx, page } = await openApp(browser, data(entries), "week");
  await page.waitForSelector("#wgGrid .wg-cell.has");
  await page.click("#wgGrid .wg-cell.has");
  await page.waitForTimeout(200);
  assert.equal(await page.isVisible("#cellChooser"), false);
  assert.equal(await page.isVisible("#logView"), true);
  await ctx.close();
});
