import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { launch, openApp } from "./harness.mjs";

const data = { employees: [{ id: "55", name: "Murat Baykal" }], items: [{ id: "5", name: "PR" }],
  projects: { projects: [], clients: [{ id: "10", name: "Acme" }] }, entries: [] };

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser.close(); });

test("#4 multi-day logs one entry per day in the range", async () => {
  const { ctx, page, errors } = await openApp(browser, data);   // Log default
  const posts = [];
  await page.route("**/api/timeactivity", (route) => {
    const r = route.request();
    if (r.method() === "POST") posts.push(r.postDataJSON());
    return route.fulfill({ json: { Id: "x" + posts.length } });
  });
  await page.fill("#durh", "8");
  await page.check("#multiDay");
  await page.waitForSelector("#multiDayRow", { state: "visible" });
  await page.fill("#date", "2026-03-02");        // Mon
  await page.fill("#dateEnd", "2026-03-05");      // Thu → 4 days
  await page.click("#submit");
  await page.waitForTimeout(300);
  assert.equal(posts.length, 4, "one POST per day Mon–Thu");
  assert.deepEqual(posts.map((p) => p.txn_date), ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05"]);
  assert.ok(posts.every((p) => p.hours === 8), "same duration each day");
  assert.match(await page.textContent("#msg"), /4 entries across 4 days/);
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("#4 multi-day toggle is hidden while editing", async () => {
  const today = new Date();
  const ymd = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
  const entries = [{ id: "E1", date: ymd, hours: 1, minutes: 0, employee: "Murat Baykal", employeeId: "55",
    nameOf: "Employee", itemId: "5", service: "PR", customer: "Acme", customerId: "10",
    billable: true, billableStatus: "Billable", hourlyRate: 250 }];
  const { ctx, page } = await openApp(browser, { ...data, entries });
  await page.waitForSelector("#entries .entry .meta");
  await page.click("#entries .entry .meta");           // enter edit mode
  await page.waitForTimeout(150);
  assert.equal(await page.isVisible("#multiDayToggle"), false);
  await ctx.close();
});
