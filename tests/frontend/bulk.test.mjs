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
  employees: [{ id: "55", name: "Murat Baykal" }], items: [{ id: "5", name: "PR" }, { id: "6", name: "MTG" }],
  projects: { projects: [], clients: [{ id: "10", name: "Acme" }] },
  entries: [e({ id: "1" }), e({ id: "2" }), e({ id: "3", billableStatus: "HasBeenBilled" })],
};

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser.close(); });

test("bulk select → set non-billable PUTs each selected entry; billed rows can't be selected", async () => {
  const { ctx, page, errors } = await openApp(browser, data, "report");
  const puts = [];
  await page.route("**/api/timeactivity/*", (route) => {
    const r = route.request();
    if (r.method() === "PUT") puts.push(r.postDataJSON());
    return route.fulfill({ json: { Id: "x" } });
  });
  await page.waitForSelector("#repEntries .entry");
  await page.click("#repSelectToggle");
  await page.waitForSelector("#repEntries .sel-box");
  // billed entry (#3) gets no checkbox → only 2 selectable
  assert.equal((await page.$$("#repEntries .sel-box")).length, 2);
  const boxes = await page.$$("#repEntries .sel-box");
  await boxes[0].check();
  await boxes[1].check();
  assert.equal((await page.textContent("#bulkCount")).trim(), "2 selected");
  await page.click('#bulkBar button[data-bulk="nonbillable"]');
  await page.waitForTimeout(300);
  assert.equal(puts.length, 2, "one PUT per selected entry");
  assert.ok(puts.every((b) => b.billable === false), "each PUT flips billable off");
  assert.deepEqual(errors, []);
  await ctx.close();
});
