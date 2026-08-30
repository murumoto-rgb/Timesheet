import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { launch, openApp } from "./harness.mjs";

const now = new Date();
const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
const company = "fixture-company";
const projectName = "Synthetic waterfront rehabilitation and accessibility coordination project";
const longNote = "Coordination notes: " + "Detailed multidisciplinary review of entrances and service access. ".repeat(18)
  + "\nReference_" + "x".repeat(240) + "\nLiteral <draft> & final review.";
const employees = Array.from({ length: 8 }, (_, i) => ({ id: `employee-${i}`, name: `Synthetic Contributor ${i} with a long display name` }));
const entries = employees.map((person, i) => ({
  id: `layout-entry-${i}`, date: today, hours: 1, minutes: 15,
  employee: person.name, employeeId: person.id, nameOf: "Employee",
  itemId: "design", service: "Design and multidisciplinary coordination",
  customer: projectName, customerId: "client-one", projectId: "p1",
  billable: true, billableStatus: "Billable", hourlyRate: 125, syncToken: "3",
  description: i === 0 ? longNote : `Other project note ${i}: full narrative retained for printing.`,
}));
const outside = { ...entries[0], id: "outside-project-entry", projectId: "p2", customerId: "client-two", description: "EXCLUDED_OTHER_PROJECT_NOTE" };
const fixture = {
  projects: { projects: [{ id: "p1", name: projectName, parentId: "client-one" }, { id: "p2", name: "Other synthetic project", parentId: "client-two" }], clients: [{ id: "client-one", name: "Synthetic waterfront owners and development partnership" }, { id: "client-two", name: "Other client" }] },
  employees, items: [{ id: "design", name: "Design and multidisciplinary coordination" }], entries: [...entries, outside],
};
let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser.close(); });

async function drill(page) {
  await page.click('#tabbar button[data-view="projects"]');
  await page.locator('.project-open[data-project-id="p1"]').click();
  await page.waitForSelector("#projectSummary");
}

function trackWrites(page) {
  const writes = [];
  page.on("request", request => {
    if (new URL(request.url()).pathname.startsWith("/api/") && request.method() !== "GET") writes.push(`${request.method()} ${new URL(request.url()).pathname}`);
  });
  return writes;
}

async function assertFits(page, width, label) {
  const size = await page.evaluate(() => {
    const viewport = innerWidth;
    return {
      viewport, document: document.documentElement.scrollWidth, body: document.body.scrollWidth,
      // Internal scrolling tables are allowed. These details identify the source
      // only if a containing element actually makes the document overflow.
      edges: [...document.querySelectorAll("body *")].filter(node => {
        const r = node.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && (r.right > viewport + 1 || r.left < -1);
      }).slice(0, 10).map(node => ({ node: node.tagName.toLowerCase(), id: node.id, class: String(node.className), right: Math.round(node.getBoundingClientRect().right) })),
    };
  });
  assert.equal(size.viewport, width, `${label}: expected viewport width`);
  assert.ok(size.document <= width + 1 && size.body <= width + 1, `${label}: horizontal document overflow ${JSON.stringify(size)}`);
  const bar = await page.locator("#tabbar").boundingBox();
  assert.ok(bar && bar.x >= -1 && bar.x + bar.width <= width + 1, `${label}: tabbar fits viewport`);
  assert.equal(await page.locator("#tabbar button:visible").count(), 5, `${label}: all five navigation tabs remain visible`);
  if (width === 1280) assert.ok(Math.abs(bar.x + bar.width / 2 - width / 2) <= 1, `${label}: desktop tabbar must remain centered`);
}

for (const width of [320, 390, 1280]) {
  test(`workspace layouts fit ${width}px with full people grid and long notes`, async () => {
    const { ctx, page, errors } = await openApp(browser, fixture);
    try {
      const writes = trackWrites(page);
      await page.setViewportSize({ width, height: 900 });
      await page.click('#tabbar button[data-view="projects"]');
      await page.waitForSelector('.project-open[data-project-id="p1"]');
      await assertFits(page, width, "Projects directory");
      await page.locator('.project-open[data-project-id="p1"]').click();
      await page.waitForSelector("#projectSummary");
      await page.click("#totToggle");
      await page.waitForSelector("#repDrill .ptbl");
      assert.equal(await page.locator("#repDrill .ptbl tbody tr").count(), employees.length);
      assert.equal(await page.locator("#projectEntries .project-entry").count(), entries.length);
      assert.ok((await page.locator("#projectEntries").textContent()).includes(longNote));
      await assertFits(page, width, "Project drill with expanded grid");

      await page.click("#workspaceMenuButton");
      await page.waitForSelector("#workspaceMenu");
      await assertFits(page, width, "Tools menu");
      await page.route("**/api/reconciliation*", route => route.fulfill({ json: {
        freshQbo: { entries: fixture.entries.length, minutes: fixture.entries.length * 75 },
        journal: { operations: 1, completed: 0, unresolved: [{ operationId: "synthetic-operation-" + "9".repeat(80), state: "uncertain" }] },
        auditEvents: 0, flags: [{ code: "UNKNOWN_OUTCOME", message: "Review a synthetic unresolved save: " + "reference".repeat(25), entryId: "layout-entry-0" }],
        readOnly: true, caveats: ["Read-only synthetic consistency check; no accounting records are modified."],
      } }));
      await page.click("#openReconciliation");
      await page.click("#reconcileRun");
      await page.waitForSelector("#downloadReconciliation");
      assert.match(await page.locator("#reconcileResults").textContent(), /Review flags \(1\)/);
      await assertFits(page, width, "Reconciliation results");

      await page.click('#tabbar button[data-view="week"]');
      await page.waitForSelector("#wgGrid .wg-row");
      assert.equal(await page.locator("#wgPeople .chip").count(), employees.length + 1);
      await assertFits(page, width, "Week grid");
      assert.deepEqual(writes, []);
      assert.deepEqual(errors, []);
    } finally { await ctx.close(); }
  });
}

test("fee-only project budget persists without inventing planned hours", async () => {
  const { ctx, page } = await openApp(browser, fixture);
  try {
    const writes = trackWrites(page);
    await drill(page);
    await page.fill("#budgetHours", "");
    await page.fill("#budgetFee", "1234.56");
    await page.click("#saveBudget");
    const saved = await page.evaluate(companyKey => JSON.parse(localStorage.getItem(`timesheet:${companyKey}:budgets`)), company);
    assert.equal(saved.p1.hours, null);
    assert.equal(saved.p1.fee, 1234.56);
    assert.equal(saved.p1.currency, "USD");
    await page.reload();
    await page.waitForSelector("#app", { state: "visible" });
    await drill(page);
    assert.equal(await page.inputValue("#budgetHours"), "");
    assert.equal(await page.inputValue("#budgetFee"), "1234.56");
    const summary = await page.locator("#budgetSummary").textContent();
    assert.match(summary, /No hours budget/);
    assert.match(summary, /\$1,234\.56 agreed fee/);
    assert.doesNotMatch(summary, /remaining|over budget|0 planned hours/i);
    assert.deepEqual(writes, []);
  } finally { await ctx.close(); }
});

test("invalid local imports leave budgets, draft, categories and save IDs unchanged", async () => {
  const { ctx, page } = await openApp(browser, fixture);
  try {
    const writes = trackWrites(page);
    await page.fill("#desc", "Existing draft must survive rejected import");
    await page.waitForFunction(companyKey => !!localStorage.getItem(`timesheet:${companyKey}:draft`), company);
    await drill(page);
    await page.fill("#budgetFee", "900");
    await page.click("#saveBudget");
    await page.evaluate(companyKey => {
      localStorage.setItem(`timesheet:${companyKey}:service-categories`, JSON.stringify({ design: false }));
      localStorage.setItem(`timesheet:${companyKey}:pending-writes`, JSON.stringify({ retained: { id: "synthetic-retained-save" } }));
    }, company);
    const snapshot = () => page.evaluate(() => Object.fromEntries(Object.keys(localStorage).sort().map(key => [key, localStorage.getItem(key)])));
    const before = await snapshot();
    assert.ok(before[`timesheet:${company}:draft`]);
    const valid = { schema: 1, companyKey: company, budgets: { p1: { hours: null, fee: 123, currency: "USD", start: "2026-02-01", end: "2026-02-28" } }, draft: null, serviceCategories: {} };
    const invalid = [
      { ...valid, companyKey: "different-company" },
      { ...valid, budgets: { p1: { ...valid.budgets.p1, start: "2026-02-30", end: "2026-03-01" } } },
      { ...valid, serviceCategories: { design: "false" } },
      { ...valid, draft: { companyKey: company, date: today, hours: "1", notes: "replacement", editing: "entry-one", before: { id: "entry-two" } } },
    ];
    for (const [i, payload] of invalid.entries()) {
      await page.evaluate(() => { document.querySelector("#writeWarning").hidden = true; });
      await page.setInputFiles("#workspaceImportFile", { name: `invalid-${i}.json`, mimeType: "application/json", buffer: Buffer.from(JSON.stringify(payload)) });
      await page.waitForSelector("#writeWarning", { state: "visible" });
      assert.match(await page.locator("#writeWarning").textContent(), /invalid|different company/i);
      assert.deepEqual(await snapshot(), before, `invalid import ${i} must not alter any existing local values`);
    }
    assert.deepEqual(writes, []);
  } finally { await ctx.close(); }
});

test("Print/PDF includes every scoped note despite the notes search", async () => {
  const { ctx, page } = await openApp(browser, fixture);
  try {
    const writes = trackWrites(page);
    await drill(page);
    await page.fill("#projectEntrySearch", "Other project note 1:");
    assert.equal(await page.locator("#projectEntries .project-entry").count(), 1);
    await page.evaluate(() => { window.print = () => { window.__printCalls = (window.__printCalls || 0) + 1; }; });
    await page.click("#projectPrint");
    assert.equal(await page.evaluate(() => window.__printCalls), 1);
    const printed = page.locator("#projectPrintReport");
    assert.equal(await printed.locator("tbody tr").count(), entries.length);
    const notes = await printed.locator("tbody tr td:last-child").allTextContents();
    assert.deepEqual(notes.sort(), entries.map(entry => entry.description).sort());
    assert.doesNotMatch(await printed.textContent(), /EXCLUDED_OTHER_PROJECT_NOTE/);
    assert.equal(await printed.locator("draft").count(), 0, "literal angle brackets in notes must not become HTML");
    await page.emulateMedia({ media: "print" });
    assert.equal(await printed.isVisible(), true);
    assert.equal(await page.locator("#app").isVisible(), false);
    assert.deepEqual(writes, []);
  } finally { await ctx.close(); }
});
