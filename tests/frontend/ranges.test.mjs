import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { launch, openApp } from "./harness.mjs";

const data = { employees: [{ id: "55", name: "Murat Baykal" }], items: [{ id: "5", name: "PR" }],
  projects: { projects: [], clients: [{ id: "10", name: "Acme" }] }, entries: [] };

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser.close(); });

test("custom range drives the fetch and the period label", async () => {
  const { ctx, page, errors } = await openApp(browser, data, "report");
  const reqs = [];
  await page.route("**/api/timeactivities*", (route) => {
    const u = new URL(route.request().url());
    reqs.push({ start: u.searchParams.get("start"), end: u.searchParams.get("end") });
    return route.fulfill({ json: [] });
  });
  await page.click('#seg button[data-unit="custom"]');
  await page.waitForSelector("#customRange", { state: "visible" });
  await page.fill("#repCStart", "2026-03-01");
  await page.dispatchEvent("#repCStart", "change");
  await page.fill("#repCEnd", "2026-03-15");
  await page.dispatchEvent("#repCEnd", "change");
  await page.waitForTimeout(200);
  // the current range is fetched (a comparison range may also be fetched for #12)
  assert.ok(reqs.some((r) => r.start === "2026-03-01" && r.end === "2026-03-15"),
    "custom range should drive the period fetch");
  assert.match(await page.textContent("#periodLabel"), /Mar 1 – Mar 15/);
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("a saved view round-trips through localStorage", async () => {
  const { ctx, page } = await openApp(browser, data, "report");
  // set a custom range
  await page.click('#seg button[data-unit="custom"]');
  await page.fill("#repCStart", "2026-03-01");
  await page.dispatchEvent("#repCStart", "change");
  await page.fill("#repCEnd", "2026-03-15");
  await page.dispatchEvent("#repCEnd", "change");
  // save it (Save view lives in the filter panel)
  await page.click("#filterToggle");
  await page.fill("#savedViewName", "March sprint");
  await page.click("#saveView");
  await page.waitForTimeout(150);
  const opts = await page.$$eval("#savedViews option", (o) => o.map((x) => x.textContent));
  assert.ok(opts.includes("March sprint"), "saved view appears in the dropdown");
  // move away, then re-apply the saved view
  await page.click('#seg button[data-unit="week"]');
  await page.waitForTimeout(150);
  await page.selectOption("#savedViews", { label: "March sprint" });
  await page.waitForTimeout(200);
  assert.match(await page.textContent("#periodLabel"), /Mar 1 – Mar 15/);
  assert.equal(await page.$eval('#seg button[data-unit="custom"]', (b) => b.classList.contains("on")), true);
  await ctx.close();
});
