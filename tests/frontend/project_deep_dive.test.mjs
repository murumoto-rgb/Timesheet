import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { launch, openApp } from "./harness.mjs";

const person = { employee: "Murat Baykal", employeeId: "55", nameOf: "Employee" };
const localYmd = (date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
const todayYmd = () => localYmd(new Date());
const entry = (o) => ({ ...person, hours: 1, minutes: 0, itemId: "5", service: "PR",
  customer: "Acme", customerId: "10", projectId: "p1", billable: true,
  billableStatus: "Billable", hourlyRate: 250, description: "work", date: todayYmd(), ...o });
const projects = { projects: [
  { id: "p1", name: "Shared Name", parentId: "10" },
  { id: "p2", name: "Shared Name", parentId: "20" },
], clients: [{ id: "10", name: "Acme" }, { id: "20", name: "Beta" }] };
const base = (entries) => ({ employees: [{ id: "55", name: "Murat Baykal" }],
  items: [{ id: "5", name: "PR" }, { id: "6", name: "Mileage" }], projects, entries });
const selectProject = async (page, id) => {
  await page.locator(`.project-open[data-project-id="${id}"]`).click();
  await page.waitForSelector("#repDrill", { state: "visible" });
  await page.waitForSelector("#projectSummary");
};

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser.close(); });

test("Projects navigation is searchable, ID-stable, and pins persist", async () => {
  const { ctx, page, errors } = await openApp(browser, base([entry({ id: "1" })]), "projects");
  await page.waitForSelector("#projectsView");
  assert.equal(await page.locator(".project-open").count(), 4);
  await page.fill("#projectSearch", "Beta");
  assert.equal(await page.locator('.project-open[data-project-id="p1"]').count(), 0);
  const beta = page.locator('.project-open[data-project-id="p2"]');
  assert.match(await beta.textContent(), /Shared Name.*Beta|Beta.*Shared Name/s);
  await page.fill("#projectSearch", "");
  await page.locator('.project-pin[data-project-id="p1"]').click();
  assert.equal(await page.locator('.project-pin[data-project-id="p1"]').getAttribute("aria-pressed"), "true");
  await page.reload();
  await page.click('#tabbar button[data-view="projects"]');
  assert.equal(await page.locator('.project-pin[data-project-id="p1"]').getAttribute("aria-pressed"), "true");
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("project drill summary keeps duplicate IDs and exact recorded-rate cents", async () => {
  const entries = [
    entry({ id: "a", projectId: "p1", customerId: "10", hours: 1, minutes: 30, hourlyRate: 200 }),
    entry({ id: "b", projectId: "p1", customerId: "10", hours: 2, hourlyRate: 333.33, billableStatus: "HasBeenBilled" }),
    entry({ id: "c", projectId: "p1", customerId: "10", hours: 1, billable: false, billableStatus: "NotBillable", hourlyRate: 0 }),
    entry({ id: "zero", projectId: "p1", customerId: "10", hours: 1, hourlyRate: 0 }),
    entry({ id: "d", projectId: "p2", customerId: "20", hours: 9, hourlyRate: 999 }),
    entry({ id: "unknown", projectId: "p1", customerId: "10", employee: "Former person", employeeId: "gone", hours: 1, hourlyRate: null }),
  ];
  const now = new Date();
  const expectedStart = localYmd(new Date(now.getFullYear(), now.getMonth() - 11, 1));
  const expectedEnd = localYmd(new Date(now.getFullYear(), now.getMonth() + 1, 0));
  const { ctx, page, errors } = await openApp(browser, base(entries), "projects");
  await selectProject(page, "p1");
  const summary = page.locator("#projectSummary");
  const text = await summary.textContent();
  const kpis = await page.$$eval("#projectSummary .project-kpi", (els) => Object.fromEntries(els.map((el) => [el.querySelector(".label")?.textContent.trim(), el.querySelector(".value")?.textContent.trim()])));
  assert.equal(kpis["Total time"], "6:30");
  assert.equal(kpis["Billable time"], "5:30");
  assert.equal(kpis[Object.keys(kpis).find((label) => /to.?invoice/i.test(label))], "3:30");
  assert.equal(kpis[Object.keys(kpis).find((label) => /billed/i.test(label))], "2:00");
  assert.match(text, /recorded.rate/i);
  assert.match(text, /\$966\.66/); // 1.5h*$200 + 2h*$333.33, exact cents
  assert.match(text, /not invoice totals, cash received or profit/i);
  await page.selectOption("#projectEntryFilter", "missingrate");
  assert.deepEqual(await page.$$eval("#projectEntries .project-entry", (els) => els.map((el) => el.dataset.entryId)), ["unknown"]);
  assert.doesNotMatch(text, /\$8,991/); // p2 must remain separate despite its duplicate name
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("expense toggle changes project rows and summary while keeping labor values readable", async () => {
  const entries = [entry({ id: "labor", hours: 2 }), entry({ id: "expense", service: "Mileage", itemId: "6", hours: 1, hourlyRate: 40 })];
  const { ctx, page } = await openApp(browser, base(entries), "projects");
  await selectProject(page, "p1");
  const before = await page.locator("#projectSummary").textContent();
  const toggle = page.locator("#projectExpenseToggle, #projectSummary .hideExp, .hideExp").first();
  await toggle.click();
  await page.waitForSelector("#projectSummary");
  const afterText = await page.locator("#projectSummary").textContent();
  assert.notEqual(afterText, before);
  assert.ok((await page.locator("#projectEntries .project-entry").count()) < 2);
  assert.match(afterText, /500\.00|2:00|2h/);
  await ctx.close();
});

test("entry search and status filters are read-only and do not alter the summary", async () => {
  const entries = [
    entry({ id: "u", description: "unbilled note" }),
    entry({ id: "b", description: "billed note", billableStatus: "HasBeenBilled" }),
    entry({ id: "n", description: "internal", billable: false, billableStatus: "NotBillable", hourlyRate: 0 }),
    entry({ id: "r", description: "missing rate", hourlyRate: null }),
    entry({ id: "m", description: "", hourlyRate: 250 }),
  ];
  const { ctx, page } = await openApp(browser, base(entries), "projects");
  await selectProject(page, "p1");
  const original = await page.locator("#projectSummary").textContent();
  const methods = [];
  page.on("request", (request) => { if (request.url().includes("/api/")) methods.push(request.method()); });
  await page.fill("#projectEntrySearch", "billed note");
  await page.selectOption("#projectEntryFilter", "billed");
  assert.equal(await page.locator("#projectEntries .project-entry").count(), 1);
  await page.fill("#projectEntrySearch", "");
  await page.selectOption("#projectEntryFilter", "missingnotes");
  assert.equal(await page.locator("#projectEntries .project-entry").count(), 1);
  assert.equal(await page.locator("#projectSummary").textContent(), original);
  assert.ok(methods.every((m) => m === "GET"), `deep dive filters must not write: ${methods}`);
  await ctx.close();
});

test("deep link restores project and dates; five-year range says Last 5 years", async () => {
  const { ctx, page } = await openApp(browser, base([entry({ id: "1" })]), "projects");
  await page.goto("https://app.test/#project=p1&start=2026-02-01&end=2026-08-20");
  await page.waitForSelector("#projectSummary");
  assert.match(await page.textContent("#projectTitle"), /Shared Name/);
  assert.equal(await page.inputValue("#repCStart"), "2026-02-01");
  assert.equal(await page.inputValue("#repCEnd"), "2026-08-20");
  await page.click("#projectRangeFiveYears");
  assert.match(await page.textContent("#projectRangeFiveYears"), /Last 5 years/i);
  await page.reload();
  await page.waitForSelector("#projectSummary");
  await page.goBack();
  await page.waitForSelector("#projectsView", { state: "visible" });
  assert.equal(await page.locator("#projectSummary:visible").count(), 0);
  await page.goto("https://app.test/#project=p1&start=2026-08-20&end=2026-02-01");
  await page.waitForSelector("#projectsView", { state: "visible" });
  assert.equal(await page.locator("#projectSummary:visible").count(), 0);
  await ctx.close();
});

test("a newer project range wins when an older response arrives late", async () => {
  const oldEntry = entry({ id: "old", projectId: "p1", hours: 1, date: "2026-02-10" });
  const newEntry = entry({ id: "new", projectId: "p1", hours: 3, date: "2026-08-10" });
  const { ctx, page } = await openApp(browser, base([oldEntry, newEntry]), "projects");
  await selectProject(page, "p1");
  await page.route("**/api/timeactivities*", async (route) => {
    const u = new URL(route.request().url());
    if (u.searchParams.get("start") === "2026-02-01") {
      await new Promise((resolve) => setTimeout(resolve, 300));
      return route.fulfill({ json: [oldEntry] });
    }
    if (u.searchParams.get("start") === "2026-08-01") return route.fulfill({ json: [newEntry] });
    return route.fallback();
  });
  await page.fill("#repCStart", "2026-02-01");
  await page.dispatchEvent("#repCStart", "change");
  await page.fill("#repCEnd", "2026-02-20");
  await page.dispatchEvent("#repCEnd", "change");
  await page.fill("#repCStart", "2026-08-01");
  await page.dispatchEvent("#repCStart", "change");
  await page.fill("#repCEnd", "2026-08-20");
  await page.dispatchEvent("#repCEnd", "change");
  await page.waitForTimeout(450);
  const kpis = await page.$$eval("#projectSummary .project-kpi", (els) => Object.fromEntries(els.map((el) => [el.querySelector(".label")?.textContent.trim(), el.querySelector(".value")?.textContent.trim()])));
  assert.equal(kpis["Total time"], "3:00");
  await ctx.close();
});

test("Report by-project links into the same enriched drill and preserves its range", async () => {
  const { ctx, page } = await openApp(browser, base([entry({ id: "1", projectId: "p1", date: "2026-08-15" })]), "report");
  await page.click('#seg button[data-unit="custom"]');
  await page.fill("#repCStart", "2026-08-01");
  await page.dispatchEvent("#repCStart", "change");
  await page.fill("#repCEnd", "2026-08-20");
  await page.dispatchEvent("#repCEnd", "change");
  await page.waitForSelector("#projRows .proj-row.tappable");
  await page.locator("#projRows .proj-row.tappable").first().click();
  await page.waitForSelector("#projectSummary");
  assert.ok(await page.locator("#projectTitle").count(), "Report opens the enriched project drill");
  assert.equal(await page.inputValue("#repCStart"), "2026-08-01");
  assert.equal(await page.inputValue("#repCEnd"), "2026-08-20");
  await ctx.close();
});

test("retry clears stale project totals and back returns to Projects", async () => {
  const entries = [entry({ id: "one", projectId: "p1", hours: 1 }), entry({ id: "two", projectId: "p2", customerId: "20", hours: 4 })];
  const { ctx, page } = await openApp(browser, base(entries), "projects");
  await selectProject(page, "p1");
  assert.match(await page.locator("#projectSummary").textContent(), /250\.00|1:00|1h/);
  let failed = true;
  await page.route("**/api/timeactivities*", (route) => {
    if (failed) { failed = false; return route.fulfill({ status: 500, body: "temporary" }); }
    return route.fulfill({ json: [entries[1]] });
  });
  await page.click("#repDrillBack");
  await page.waitForSelector("#projectsView", { state: "visible" });
  await page.locator('.project-open[data-project-id="p2"]').click();
  await page.waitForSelector("#projectRetry");
  assert.equal(await page.locator("#projectSummary").count(), 0);
  await page.click("#projectRetry");
  await page.waitForSelector("#projectSummary");
  assert.match(await page.locator("#projectSummary").textContent(), /\$1,000\.00|4:00|4h/);
  await page.click("#repDrillBack");
  assert.equal(await page.isVisible("#projectsView"), true);
  await ctx.close();
});

test("Dashboard Project concentration opens an exact project-scoped drill and returns", async () => {
  const now = new Date();
  const tomorrow = localYmd(new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1));
  const entries = [
    entry({ id: "dash-p1", projectId: "p1", customerId: "10", hours: 2, hourlyRate: 100, billableStatus: "HasBeenBilled", date: todayYmd() }),
    entry({ id: "dash-p2", projectId: "p2", customerId: "20", hours: 5, hourlyRate: 100, billableStatus: "HasBeenBilled", date: todayYmd() }),
    entry({ id: "dash-future", projectId: "p1", customerId: "10", hours: 7, date: tomorrow }),
  ];
  const expectedStart = localYmd(new Date(now.getFullYear(), now.getMonth() - 11, 1));
  const expectedEnd = todayYmd();
  const { ctx, page } = await openApp(browser, base(entries), "dash");
  await page.waitForSelector("#concSeg [data-concby=client]");
  assert.equal(await page.locator(".project-concentration").count(), 0, "client rollups must not pretend to be one project");
  await page.click("#concSeg [data-concby=project]");
  const p1 = page.locator('.project-concentration[data-project-id="p1"]');
  await page.waitForSelector('.project-concentration[data-project-id="p1"]');
  assert.equal(await p1.count(), 1);
  await p1.click();
  await page.waitForSelector("#projectSummary");
  const kpis = await page.$$eval("#projectSummary .project-kpi", (els) => Object.fromEntries(els.map((el) => [el.querySelector(".label")?.textContent.trim(), el.querySelector(".value")?.textContent.trim()])));
  assert.equal(kpis["Total time"], "2:00");
  assert.equal(await page.inputValue("#repCStart"), expectedStart);
  assert.equal(await page.inputValue("#repCEnd"), expectedEnd);
  assert.equal(await page.locator("#projectEntries .project-entry").count(), 1);
  assert.match(await page.textContent("#projectTitle"), /Shared Name/);
  await page.click("#repDrillBack");
  await page.waitForSelector("#dashView", { state: "visible" });
  await ctx.close();
});
