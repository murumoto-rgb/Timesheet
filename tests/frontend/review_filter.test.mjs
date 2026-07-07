import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { launch, openApp } from "./harness.mjs";

const pad = (n) => String(n).padStart(2, "0");
const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const today = () => ymd(new Date());

const MB = { employee: "Murat Baykal", employeeId: "55", nameOf: "Employee" };
const e = (o) => ({ ...MB, date: today(), hours: 1, minutes: 0, itemId: "5", service: "PR",
  customer: "Acme", customerId: "10", billable: true, billableStatus: "Billable", hourlyRate: 250, ...o });
const data = {
  employees: [{ id: "55", name: "Murat Baykal" }], items: [{ id: "5", name: "PR" }],
  projects: { projects: [], clients: [{ id: "10", name: "Acme" }] },
  entries: [
    e({ id: "1", service: "PR", customer: "Acme", hours: 2, description: "foundation review" }),
    e({ id: "2", service: "MTG", customer: "Beta", description: "client call" }),
    e({ id: "3", service: "PR", customer: "", description: "" }),               // needs attention
    e({ id: "4", service: "PR", customer: "Gamma", description: "admin", billable: false, billableStatus: "NotBillable" }),
  ],
};

const rows = (page) => page.$$eval("#repEntries .entry", (els) => els.length);

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser.close(); });

test("report search + cleanup + status/service filters scope the list and hero", async () => {
  const { ctx, page, errors } = await openApp(browser, data, "report");
  await page.waitForSelector("#repEntries .entry");
  assert.equal(await rows(page), 4);
  await page.click("#filterToggle");

  // text search
  await page.fill("#repQ", "foundation");
  await page.waitForTimeout(150);
  assert.equal(await rows(page), 1);
  assert.equal((await page.textContent("#heroTotal")).trim(), "2:00");
  await page.click("#repFilterClear");
  await page.waitForTimeout(100);
  assert.equal(await rows(page), 4);

  // #8 cleanup preset — only the entry missing client + notes
  await page.check("#repNeeds");
  await page.waitForTimeout(150);
  assert.equal(await rows(page), 1);
  await page.uncheck("#repNeeds");

  // status = non-billable
  await page.selectOption("#repStatus", "nonbillable");
  await page.waitForTimeout(150);
  assert.equal(await rows(page), 1);
  await page.selectOption("#repStatus", "");

  // service = MTG
  await page.selectOption("#repSvc", "MTG");
  await page.waitForTimeout(150);
  assert.equal(await rows(page), 1);

  assert.deepEqual(errors, []);
  await ctx.close();
});
