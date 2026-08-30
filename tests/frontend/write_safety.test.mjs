import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { launch, openApp } from "./harness.mjs";

const d = new Date(), today = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
const entries = [{ id: "e1", date: today, hours: 0, minutes: 15, employee: "Murat", employeeId: "55", nameOf: "Employee", itemId: "5", service: "PR", customer: "Acme", customerId: "10", billable: true, billableStatus: "Billable", hourlyRate: 200, description: "old", syncToken: "9" }];
const data = { entries, employees: [{ id: "55", name: "Murat" }], items: [{ id: "5", name: "PR" }], projects: { projects: [], clients: [{ id: "10", name: "Acme" }] } };
let browser;
before(async () => { browser = await launch(); }); after(async () => { await browser.close(); });

test("notes-only full edit preserves 0:15 and the displayed token", async () => {
  const { ctx, page } = await openApp(browser, data); const puts = [];
  await page.route("**/api/timeactivity/*", r => { if (r.request().method() === "PUT") { const body = r.request().postDataJSON(); puts.push(body); assert.equal(body.company_key, "fixture-company"); return r.fulfill({ json: { Id: "e1", SyncToken: "10", operationId: body.operation_id } }); } return r.fulfill({ json: {} }); });
  await page.waitForSelector("#entries .entry .meta"); await page.click("#entries .entry .meta"); await page.waitForSelector("#cancelEdit", { state: "visible" }); await page.fill("#desc", "full notes");
  const saved = page.waitForResponse(r => r.request().method() === "PUT" && r.url().endsWith("/api/timeactivity/e1"));
  await page.click("#submit"); await saved; await page.waitForSelector("#cancelEdit", { state: "hidden" });
  assert.equal(puts.length, 1);
  assert.equal(puts[0]?.hours, 0); assert.equal(puts[0]?.minutes, 15); assert.equal(puts[0]?.sync_token, "9");
  await ctx.close();
});

test("delete and undo use displayed or created sync tokens", async () => {
  const { ctx, page } = await openApp(browser, data); const calls = [];
  await page.route("**/api/timeactivity/*", r => { const body = r.request().postDataJSON(); calls.push({ method: r.request().method(), body }); return r.fulfill({ json: { deleted: "e1", Id: "new", SyncToken: "4", operationId: body?.operation_id } }); });
  await page.route("**/api/timeactivity", r => { const body = r.request().postDataJSON(); calls.push({ method: r.request().method(), body }); return r.fulfill({ json: { Id: "new", SyncToken: "4", operationId: body?.operation_id } }); });
  await page.waitForSelector('#entries .entry .del[aria-label="Delete entry"]'); await page.click('#entries .entry .del[aria-label="Delete entry"]'); await page.waitForSelector("#confirmDialog:not([hidden])");
  const deleted = page.waitForResponse(r => r.request().method() === "DELETE" && r.url().endsWith("/api/timeactivity/e1"));
  await page.click("#confirmGo"); await deleted;
  assert.equal(calls[0].method, "DELETE"); assert.equal(calls[0].body.sync_token, "9"); assert.ok(calls[0].body.operation_id);
  const restored = page.waitForResponse(r => r.request().method() === "POST" && r.url().endsWith("/api/timeactivity"));
  await page.click("#toastAction"); await restored; const post = calls.find(x => x.method === "POST"); assert.ok(post); assert.equal(post.body.sync_token, "9"); assert.ok(post.body.operation_id); await ctx.close();
});

test("uncertain save reloads and unchanged retry reuses UUID, changed notes get a new UUID", async () => {
  const { ctx, page } = await openApp(browser, { ...data, entries: [] }); const bodies = []; let first = true;
  await page.route("**/api/timeactivity", async r => { const body = r.request().postDataJSON(); bodies.push(body); if (first) { first = false; return r.abort(); } return r.fulfill({ json: { Id: "saved", SyncToken: "2", operationId: body.operation_id } }); });
  await page.click("#projectBtn"); await page.locator('#pickerList .pick-item', { hasText: "Acme" }).first().click(); await page.fill("#durh", "0.25"); await page.fill("#desc", "retry me"); await page.click("#submit"); await page.waitForFunction(() => document.querySelector("#writeWarning")?.textContent.includes("uncertain"));
  const pending = await page.evaluate(() => JSON.parse(localStorage.getItem("timesheet:fixture-company:pending-writes"))); assert.equal(Object.keys(pending).length, 1); const ticket = Object.values(pending)[0];
  await page.reload(); await page.waitForSelector("#draftNotice"); await page.click("#restoreDraft");
  const retried = page.waitForResponse(r => r.request().method() === "POST" && r.url().endsWith("/api/timeactivity"));
  await page.click("#submit"); await retried; await page.waitForSelector("#submit:not([disabled])");
  assert.equal(bodies.length, 2); assert.equal(bodies[0].operation_id, ticket.id); assert.deepEqual(bodies[1], bodies[0]); assert.equal(bodies[1].company_key, "fixture-company");
  await page.click("#projectBtn"); await page.locator('#pickerList .pick-item', { hasText: "Acme" }).first().click(); await page.fill("#durh", "0.25"); await page.fill("#desc", "changed notes");
  const changed = page.waitForResponse(r => r.request().method() === "POST" && r.url().endsWith("/api/timeactivity"));
  await page.click("#submit"); await changed; assert.equal(bodies.length, 3); assert.notEqual(bodies[2].operation_id, bodies[1].operation_id); assert.equal(bodies[2].description, "changed notes"); await ctx.close();
});

test("cancel resets weekend state and keyboard modal focus is contained/restored", async () => {
  const { ctx, page } = await openApp(browser, data); await page.check("#multiDay"); await page.waitForSelector("#weekendToggle", { state: "visible" }); await page.check("#includeWeekends"); await page.click("#cancelNew"); await page.check("#multiDay"); await page.waitForSelector("#weekendToggle", { state: "visible" }); assert.equal(await page.isChecked("#includeWeekends"), false);
  await page.click("#projectBtn"); await page.waitForSelector("#picker:not([hidden])"); const first = await page.locator("#pickerSearch").evaluate(el => el === document.activeElement); assert.equal(first, true); await page.keyboard.press("Escape"); assert.equal(await page.locator("#picker").getAttribute("hidden"), ""); await ctx.close();
});

test("multi-day recovery retries only the unconfirmed job with its original UUID", async () => {
  const { ctx, page } = await openApp(browser, { ...data, entries: [] }); const calls = []; let attempts = 0;
  await page.route("**/api/timeactivity", r => { const body = r.request().postDataJSON(); assert.equal(body.company_key, "fixture-company"); calls.push(body); attempts++; if (attempts === 2) return r.fulfill({ status: 502, json: { detail: { code: "UNCERTAIN" } } }); return r.fulfill({ json: { Id: `q${attempts}`, SyncToken: "1", operationId: body.operation_id } }); });
  await page.click("#projectBtn"); await page.locator('#pickerList .pick-item', { hasText: "Acme" }).first().click(); await page.fill("#durh", "1"); await page.check("#multiDay"); await page.fill("#date", "2026-08-24"); await page.fill("#dateEnd", "2026-08-26"); await page.check("#includeWeekends"); assert.equal(await page.isChecked("#multiDay"), true); assert.equal(await page.inputValue("#dateEnd"), "2026-08-26"); await page.click("#submit"); await page.waitForSelector("#confirmDialog:not([hidden])"); await page.click("#confirmGo"); await page.waitForSelector("#retryFailedBatch:not([disabled])");
  assert.equal(calls.length, 3); const failedId = calls[1].operation_id; await page.click("#retryFailedBatch"); await page.waitForSelector("#confirmDialog:not([hidden])"); await page.click("#confirmGo"); await page.waitForSelector("#retryFailedBatch", { state: "hidden" }); assert.equal(calls.length, 4); assert.equal(calls[3].operation_id, failedId); assert.deepEqual(calls.slice(0, 3).map(x => x.txn_date), ["2026-08-24", "2026-08-25", "2026-08-26"]); await ctx.close();
});

test("a delayed older Log refresh cannot hide a newly saved entry", async () => {
  const { ctx, page } = await openApp(browser, { ...data, entries: [] }); let recentReads = 0; let releaseOld; const oldResponse = new Promise(resolve => { releaseOld = resolve; }); const methods = [];
  await page.route("**/api/timeactivities*", async r => { const url = r.request().url(); methods.push(r.request().method()); if (new URL(url).searchParams.get("days") !== "14") return r.fulfill({ json: [] }); recentReads++; if (recentReads === 1) { await oldResponse; return r.fulfill({ json: [] }); } return r.fulfill({ json: [{ ...entries[0], id: "new-save", description: "newly saved" }] }); });
  await page.route("**/api/timeactivity", r => { const body = r.request().postDataJSON(); assert.equal(body.company_key, "fixture-company"); methods.push(r.request().method()); return r.fulfill({ json: { Id: "new-save", SyncToken: "2", operationId: body.operation_id } }); });
  const oldRequest = page.waitForRequest(r => r.url().includes("/api/timeactivities") && new URL(r.url()).searchParams.get("days") === "14"); await page.click("#refreshRecent"); await oldRequest;
  await page.click("#projectBtn"); await page.locator('#pickerList .pick-item', { hasText: "Acme" }).first().click(); await page.fill("#durh", "1"); await page.fill("#desc", "newly saved"); await page.click("#submit"); await page.waitForFunction(() => document.querySelector("#entries")?.textContent.includes("newly saved")); assert.ok(methods.includes("POST")); releaseOld(); await page.waitForTimeout(100); assert.match(await page.locator("#entries").textContent(), /newly saved/); await ctx.close();
});
