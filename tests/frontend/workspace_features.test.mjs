import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { launch, openApp } from "./harness.mjs";

const local = new Date();
const today = `${local.getFullYear()}-${String(local.getMonth() + 1).padStart(2, "0")}-${String(local.getDate()).padStart(2, "0")}`;
const projects = { projects: [{ id: "p1", name: "Alpha Project", parentId: "c1" }], clients: [{ id: "c1", name: "Alpha" }] };
const entry = (o = {}) => ({ id: o.id || "e1", date: o.date || today, hours: 1, minutes: 0, employee: "Murat", employeeId: "55", nameOf: "Employee", itemId: "5", service: "Design", customer: "Alpha Project", customerId: "c1", projectId: "p1", billable: true, billableStatus: "Billable", hourlyRate: 200, description: "project note", syncToken: "7", ...o });
const base = (entries = [entry()]) => ({ projects, employees: [{ id: "55", name: "Murat" }], items: [{ id: "5", name: "Design" }], entries });
const drill = async (page) => { await page.click('#tabbar button[data-view="projects"]'); await page.locator('.project-open[data-project-id="p1"]').click(); await page.waitForSelector("#projectSummary"); };
let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser.close(); });

test("budget persists and a different range is never compared", async () => {
  const { ctx, page } = await openApp(browser, base()); await drill(page);
  await page.fill("#budgetHours", "12"); await page.fill("#budgetFee", "2400"); await page.click("#saveBudget");
  await page.reload(); await page.click('#tabbar button[data-view="projects"]'); await page.locator('.project-open[data-project-id="p1"]').click(); await page.waitForSelector("#budgetSummary");
  assert.match(await page.locator("#budgetSummary").textContent(), /12 planned hours/);
  await page.fill("#repCStart", "2025-01-01"); await page.dispatchEvent("#repCStart", "change"); await page.fill("#repCEnd", "2025-01-31"); await page.dispatchEvent("#repCEnd", "change"); await page.waitForTimeout(250);
  assert.match(await page.locator("#budgetSummary").textContent(), /different dates|No comparison/i); await ctx.close();
});

test("project financials renders exact arrays and mixed currencies without writes", async () => {
  const { ctx, page } = await openApp(browser, base()); await page.route("**/api/project-financials*", r => r.fulfill({ json: { start: today, end: today, scope: "Exact links", mixedCurrency: true, currencies: ["EUR", "USD"], invoices: [{ id: "i1", docNumber: "100", date: today, amount: 100, balance: 20, currency: "USD" }], payments: [{ id: "pay1", date: today, amount: 80, currency: "USD", invoiceIds: ["i1"] }] } })); await drill(page);
  const methods = []; page.on("request", r => { if (r.url().includes("/api/")) methods.push(r.method()); }); await page.click("#loadProjectBilling"); await page.waitForFunction(() => !document.querySelector("#projectBillingResults")?.textContent.includes("Loading accounting history"));
  const billing = await page.locator("#projectBillingResults").textContent();
  assert.match(billing, /Multiple currencies/); assert.match(billing, /Invoice 100/); assert.match(billing, /100\.00/); assert.match(billing, /20\.00/); assert.match(billing, /80\.00/); assert.match(billing, /pay1/); assert.match(billing, /i1/); assert.ok(methods.every(m => m === "GET")); await ctx.close();
});

test("reconciliation displays mismatch flags and performs no writes", async () => {
  const { ctx, page } = await openApp(browser, base(), "report"); await page.route("**/api/reconciliation*", r => r.fulfill({ json: { freshQbo: { entries: 2, minutes: 120 }, flags: [{ code: "DUPLICATE", message: "Duplicate candidate" }], journal: { completed: 1, unresolved: [] }, auditEvents: 1, readOnly: true, caveats: [] } }));
  await page.click("#workspaceMenuButton"); await page.click("#openReconciliation"); const methods = []; page.on("request", r => { if (r.url().includes("/api/")) methods.push(r.method()); }); await page.click("#reconcileRun"); await page.waitForSelector("#reconcileResults h3");
  const result = await page.locator("#reconcileResults").textContent(); assert.match(result, /differs/); assert.match(result, /Duplicate candidate/); assert.match(result, /1:00/); assert.match(result, /2:00/); assert.ok(methods.every(m => m === "GET")); await ctx.close();
});

test("draft restore and timer only change the form and never auto-post", async () => {
  const { ctx, page } = await openApp(browser, base()); const posts = []; page.on("request", r => { if (r.method() !== "GET") posts.push(r.method()); });
  await page.fill("#desc", "unfinished work"); await page.waitForTimeout(300); await page.reload(); await page.waitForSelector("#draftNotice"); assert.match(await page.locator("#draftNotice").textContent(), /never submitted automatically/i); await page.click("#restoreDraft"); await page.waitForTimeout(150); assert.equal(await page.inputValue("#desc"), "unfinished work"); await page.click("#timerStart"); assert.match(await page.locator("#timerReadout").textContent(), /not posted/); await page.click("#timerStop"); await page.waitForTimeout(100); assert.match(await page.inputValue("#durh"), /\d/); assert.deepEqual(posts, []); await ctx.close();
});

test("CSV export includes all notes and escapes formula text", async () => {
  const entries = [entry({ id: "a", description: "visible note" }), entry({ id: "b", description: "=HYPERLINK(\"https://bad\")" })]; const { ctx, page } = await openApp(browser, base(entries)); await drill(page); await page.fill("#projectEntrySearch", "visible note");
  const download = page.waitForEvent("download"); await page.click("#projectCSV"); const file = await download; const text = await file.createReadStream().then(async stream => { let out = ""; for await (const chunk of stream) out += chunk; return out; });
  assert.match(text, /,"a",/); assert.match(text, /,"b",/); assert.equal((text.match(/visible note/g) || []).length, 1); assert.equal((text.match(/'=HYPERLINK/g) || []).length, 1); await ctx.close();
});
