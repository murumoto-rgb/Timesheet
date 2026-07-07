import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { launch, openApp } from "./harness.mjs";

const pad = (n) => String(n).padStart(2, "0");
const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const today = () => ymd(new Date());

const base = (entries) => ({ entries, employees: [{ id: "55", name: "Murat Baykal" }],
  items: [{ id: "5", name: "PR" }], projects: { projects: [], clients: [{ id: "10", name: "Acme" }] } });

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser.close(); });

test("inline quick-edit PUTs rounded hours + notes without opening the form", async () => {
  const entries = [{ id: "E1", date: today(), hours: 2, minutes: 0, employee: "Murat Baykal",
    employeeId: "55", nameOf: "Employee", itemId: "5", service: "PR", customer: "Acme",
    customerId: "10", billable: true, billableStatus: "Billable", hourlyRate: 250 }];
  const { ctx, page, errors } = await openApp(browser, base(entries));
  const puts = [];
  await page.route("**/api/timeactivity/*", (route) => {
    const r = route.request();
    if (r.method() === "PUT") puts.push(r.postDataJSON());
    return route.fulfill({ json: { Id: "E1" } });
  });
  await page.waitForSelector("#entries .entry .edit");
  await page.click("#entries .entry .edit");            // enter inline edit
  await page.waitForSelector("#entries .entry.ie .ie-dur");
  await page.fill("#entries .entry.ie .ie-dur", "3.2"); // → rounds to 3.0h (nearest ½h)
  await page.fill("#entries .entry.ie .ie-notes", "site review");
  await page.click("#entries .entry.ie .ie-btn.save");
  await page.waitForTimeout(250);
  assert.equal(puts.length, 1, "save should PUT once");
  assert.equal(puts[0].hours, 3);
  assert.equal(puts[0].minutes, 0);
  assert.equal(puts[0].description, "site review");
  assert.equal(puts[0].customer_id, "10");             // unchanged attachment
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("billed rows have no quick-edit affordance", async () => {
  const entries = [{ id: "B1", date: today(), hours: 3, minutes: 0, employee: "Murat Baykal",
    employeeId: "55", nameOf: "Employee", itemId: "5", service: "PR", customer: "Acme",
    customerId: "10", billable: true, billableStatus: "HasBeenBilled", hourlyRate: 250 }];
  const { ctx, page } = await openApp(browser, base(entries));
  await page.waitForSelector("#entries .entry");
  assert.equal((await page.$$("#entries .entry .edit")).length, 0, "no ✎ on a billed row");
  await ctx.close();
});
