import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { launch, openApp } from "./harness.mjs";

const pad = (n) => String(n).padStart(2, "0");
const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const today = () => ymd(new Date());

const base = (entries = []) => ({ entries, employees: [{ id: "55", name: "Murat Baykal" }],
  items: [{ id: "5", name: "PR" }], projects: { projects: [], clients: [{ id: "10", name: "Acme" }] } });

// record mutations and give POST a fresh id; registered after openApp so these win.
async function recordMutations(page) {
  const calls = [];
  await page.route("**/api/timeactivity", (route) => {
    const r = route.request();
    if (r.method() === "POST") { calls.push({ m: "POST", body: r.postDataJSON() }); return route.fulfill({ json: { Id: "new99" } }); }
    return route.fulfill({ json: {} });
  });
  await page.route("**/api/timeactivity/*", (route) => {
    const r = route.request();
    calls.push({ m: r.method(), url: r.url() });
    return route.fulfill({ json: { Id: "x" } });
  });
  return calls;
}

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser.close(); });

test("logging shows an Undo toast that deletes the new entry", async () => {
  const { ctx, page, errors } = await openApp(browser, base());
  const calls = await recordMutations(page);
  await page.fill("#durh", "2");
  await page.click("#submit");
  await page.waitForSelector("#toast", { state: "visible" });
  assert.ok(calls.some((c) => c.m === "POST"), "submit should POST a new entry");
  assert.equal(await page.textContent("#toastAction"), "Undo");
  await page.click("#toastAction");
  await page.waitForTimeout(200);
  assert.ok(calls.some((c) => c.m === "DELETE" && c.url.endsWith("/new99")), "Undo should DELETE the new id");
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("deleting shows an Undo toast that re-creates the entry", async () => {
  const entries = [{ id: "E1", date: today(), hours: 3, minutes: 0, employee: "Murat Baykal",
    employeeId: "55", nameOf: "Employee", itemId: "5", service: "PR", customer: "Acme",
    customerId: "10", billable: true, billableStatus: "Billable", hourlyRate: 250 }];
  const { ctx, page, errors } = await openApp(browser, base(entries));
  const calls = await recordMutations(page);
  await page.waitForSelector("#entries .entry");
  await page.click('#entries [aria-label="Delete entry"]');   // the × delete button
  await page.waitForSelector("#confirmDialog:not([hidden])");
  await page.click("#confirmGo");
  await page.waitForSelector("#toast", { state: "visible" });
  assert.ok(calls.some((c) => c.m === "DELETE" && c.url.endsWith("/E1")), "× should DELETE the entry");
  await page.click("#toastAction");
  await page.waitForTimeout(200);
  const recreate = calls.find((c) => c.m === "POST");
  assert.ok(recreate, "Undo should re-POST the entry");
  assert.equal(recreate.body.customer_id, "10");        // same client
  assert.equal(recreate.body.hours, 3);
  assert.deepEqual(errors, []);
  await ctx.close();
});
