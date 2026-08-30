import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { launch } from "./harness.mjs";

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser.close(); });
const root = new URL("../../", import.meta.url);
const pendingKey = "timesheet:fixture-company:pending-writes";
const batchKey = "timesheet:fixture-company:batch";
const sample = (description = "Original", date = "2026-08-30") => ({ item_id: "5", employee_id: "55", customer_id: "10", hours: 1, minutes: 0, billable: false, description, txn_date: date });

async function twoTabs(handler) {
  const ctx = await browser.newContext();
  await ctx.route("**/*", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/") return route.fulfill({ contentType: "text/html", body: readFileSync(new URL("index.html", root), "utf8") });
    if (path.startsWith("/static/workspace.")) return route.fulfill({ contentType: path.endsWith(".js") ? "text/javascript" : "text/css", body: readFileSync(new URL(path.slice(1), root), "utf8") });
    if (path === "/sw.js") return route.fulfill({ contentType: "text/javascript", body: "" });
    if (path.startsWith("/api/timeactivity") && route.request().method() !== "GET") return handler(route);
    const fixtures = {
      "/api/status": { configured: true, connected: true, auth_required: false, authed: true, environment: "sandbox", companyKey: "fixture-company" },
      "/api/projects": { projects: [], clients: [{ id: "10", name: "Acme" }] },
      "/api/employees": [{ id: "55", name: "Test person" }], "/api/vendors": [],
      "/api/items": [{ id: "5", name: "PR" }], "/api/timeactivities": [],
      "/api/company": { name: "Synthetic fixture" },
    };
    return route.fulfill({ json: fixtures[path] ?? {} });
  });
  const first = await ctx.newPage(), second = await ctx.newPage();
  for (const page of [first, second]) {
    await page.goto("https://app.test/");
    await page.waitForSelector("#workspaceMenuButton:not([hidden])");
    assert.equal(await page.evaluate(() => window.isSecureContext && !!navigator.locks), true);
  }
  return { ctx, first, second };
}
const tickets = (page) => page.evaluate((key) => JSON.parse(localStorage.getItem(key) || "{}"), pendingKey);
const batch = (page) => page.evaluate((key) => JSON.parse(localStorage.getItem(key) || "null"), batchKey);
const start = (page, body) => page.evaluate((body) => {
  window.savePromise = ProjectWorkspace.write("POST", "/api/timeactivity", body).then((response) => response.status);
}, body);
const settled = (page) => page.evaluate(() => window.savePromise);
const uncertain = (route) => route.fulfill({ status: 502, json: { detail: { code: "OPERATION_UNCERTAIN", message: "Unknown outcome" } } });
const confirmed = (route, id = "q1") => route.fulfill({ json: { Id: id, SyncToken: "1", operationId: route.request().postDataJSON().operation_id } });
async function waitForCalls(calls, length) { await assert.doesNotReject(async () => { for (let n = 0; n < 100 && calls.length < length; n++) await new Promise((resolve) => setTimeout(resolve, 10)); assert.equal(calls.length, length); }); }

test("two tabs persist separate uncertain tickets under one browser lock", async () => {
  const calls = [];
  const { ctx, first, second } = await twoTabs((route) => { calls.push(route.request().postDataJSON()); return uncertain(route); });
  try {
    await second.evaluate(() => { window.holding = navigator.locks.request("timesheet:fixture-company:pending-writes:lock", () => new Promise((resolve) => { window.releaseLock = resolve; })); });
    await second.waitForFunction(() => !!window.releaseLock);
    await Promise.all([start(first, sample("first")), start(second, sample("second", "2026-08-31"))]);
    await first.waitForTimeout(50);
    assert.equal(calls.length, 0, "no outbound write before acquiring the persistence lock");
    await second.evaluate(() => window.releaseLock());
    assert.deepEqual(await Promise.all([settled(first), settled(second)]), [502, 502]);
    const saved = Object.values(await tickets(first));
    assert.equal(saved.length, 2);
    assert.deepEqual(new Set(saved.map((ticket) => ticket.id)), new Set(calls.map((body) => body.operation_id)));
    assert.ok(calls.every((body) => body.company_key === "fixture-company"));
  } finally { await ctx.close(); }
});

test("a delayed confirmation cannot erase another tab's newer same-field ticket", async () => {
  const routes = [];
  const { ctx, first, second } = await twoTabs((route) => { routes.push(route); });
  try {
    await start(first, sample()); await waitForCalls(routes, 1);
    await start(second, sample()); await waitForCalls(routes, 2);
    const oldId = routes[0].request().postDataJSON().operation_id;
    assert.equal(routes[1].request().postDataJSON().operation_id, oldId);
    await confirmed(routes[0]); await settled(first);
    await start(first, sample()); await waitForCalls(routes, 3);
    const newId = routes[2].request().postDataJSON().operation_id;
    assert.notEqual(newId, oldId);
    await confirmed(routes[1]); await settled(second);
    assert.equal(Object.values(await tickets(first))[0].id, newId);
    await uncertain(routes[2]); await settled(first);
    assert.equal(Object.values(await tickets(first))[0].id, newId);
  } finally { await ctx.close(); }
});

test("lost response then auth/company rejection retains UUID until matching terminal receipt", async () => {
  const codes = [[502, "OPERATION_UNCERTAIN"], [401, ""], [403, ""], [409, "COMPANY_CHANGED"], [428, "COMPANY_REQUIRED"], [409, "OPERATION_MISMATCH"], [400, "INVALID_REFERENCE"]];
  const calls = [];
  const { ctx, first } = await twoTabs((route) => {
    const body = route.request().postDataJSON(), [status, code] = codes[calls.length]; calls.push(body);
    return route.fulfill({ status, json: { detail: { code, message: code, ...(calls.length === codes.length ? { operationId: body.operation_id, operationStatus: "failed" } : {}) } } });
  });
  try {
    for (let i = 0; i < codes.length; i++) {
      await start(first, sample()); assert.equal(await settled(first), codes[i][0]);
      assert.equal(Object.keys(await tickets(first)).length, i === codes.length - 1 ? 0 : 1);
    }
    assert.equal(new Set(calls.map((body) => body.operation_id)).size, 1);
  } finally { await ctx.close(); }
});

test("missing Web Locks prevents all accounting requests", async () => {
  let calls = 0;
  const { ctx, first } = await twoTabs((route) => { calls++; return confirmed(route); });
  try {
    const error = await first.evaluate(async (body) => {
      Object.defineProperty(navigator, "locks", { value: undefined, configurable: true });
      try { await ProjectWorkspace.write("POST", "/api/timeactivity", body); return ""; } catch (error) { return error.message; }
    }, sample());
    assert.match(error, /Web Locks.*HTTPS or localhost/);
    assert.equal(calls, 0);
  } finally { await ctx.close(); }
});

test("cross-tab batches cannot overwrite active recovery or replay a stale snapshot", async () => {
  const routes = [];
  const { ctx, first, second } = await twoTabs((route) => { routes.push(route); });
  const run = (page, body) => page.evaluate((body) => {
    window.batchPromise = ProjectWorkspace.runBatch("multi", [{ method: "POST", endpoint: "/api/timeactivity", body, label: body.description }]);
  }, body);
  try {
    await run(first, sample("first batch")); await waitForCalls(routes, 1);
    const original = await batch(first);
    await run(second, sample("second batch", "2026-08-31")); await second.evaluate(() => window.batchPromise);
    assert.equal(routes.length, 1);
    assert.equal((await batch(first)).batchId, original.batchId);
    assert.match(await second.locator("#writeWarning").textContent(), /Another tab/);
    await uncertain(routes[0]); await first.evaluate(() => window.batchPromise);
    await run(second, sample("second batch", "2026-08-31")); await second.evaluate(() => window.batchPromise);
    assert.equal(routes.length, 1);
    assert.equal((await batch(first)).batchId, original.batchId);
    await second.click("#retryFailedBatch"); await second.click("#confirmGo"); await waitForCalls(routes, 2);
    assert.equal(routes[1].request().postDataJSON().operation_id, routes[0].request().postDataJSON().operation_id);
    await confirmed(routes[1]); await second.waitForSelector("#retryFailedBatch", { state: "detached" });
    // First tab still displays the old unconfirmed snapshot; retry must reread done.
    await first.click("#retryFailedBatch"); await first.click("#confirmGo");
    await first.waitForSelector("#retryFailedBatch", { state: "detached" });
    assert.equal(routes.length, 2);
    assert.equal((await batch(first)).jobs[0].state, "done");
    await run(second, sample("replacement batch", "2026-09-01")); await waitForCalls(routes, 3);
    await uncertain(routes[2]); await second.evaluate(() => window.batchPromise);
    const replacement = await batch(second);
    await first.click("#dismissBatch");
    assert.equal((await batch(first)).batchId, replacement.batchId, "old Close must not delete the replacement batch");
  } finally { await ctx.close(); }
});

test("successful save keeps a newer form and persisted draft", async () => {
  const routes = [];
  const { ctx, first } = await twoTabs((route) => { routes.push(route); });
  try {
    await first.click("#projectBtn"); await first.locator("#pickerList .pick-item", { hasText: "Acme" }).first().click();
    await first.fill("#durh", "1"); await first.fill("#desc", "submitted draft"); await first.click("#submit"); await waitForCalls(routes, 1);
    await first.fill("#durh", "2"); await first.fill("#desc", "newer unsent draft"); await first.waitForTimeout(250);
    await confirmed(routes[0]); await first.waitForSelector("#submit:not([disabled])");
    assert.equal(await first.inputValue("#desc"), "newer unsent draft");
    assert.equal(await first.evaluate(() => JSON.parse(localStorage.getItem("timesheet:fixture-company:draft")).notes), "newer unsent draft");
  } finally { await ctx.close(); }
});

test("successful multi-day batch keeps a newer form and persisted draft", async () => {
  const routes = [];
  const { ctx, first } = await twoTabs((route) => { routes.push(route); });
  try {
    await first.fill("#durh", "1"); await first.fill("#desc", "submitted batch draft");
    await first.evaluate((body) => {
      window.batchPromise = ProjectWorkspace.runBatch("multi", [{ method: "POST", endpoint: "/api/timeactivity", body, label: "one day" }]);
    }, sample());
    await waitForCalls(routes, 1);
    await first.fill("#durh", "2"); await first.fill("#desc", "newer unsent draft during batch"); await first.waitForTimeout(250);
    await confirmed(routes[0]); await first.evaluate(() => window.batchPromise);
    assert.equal(await first.inputValue("#desc"), "newer unsent draft during batch");
    assert.equal(await first.evaluate(() => JSON.parse(localStorage.getItem("timesheet:fixture-company:draft")).notes), "newer unsent draft during batch");
    assert.match(await first.locator("#msg").textContent(), /newer draft has been kept/);
  } finally { await ctx.close(); }
});
