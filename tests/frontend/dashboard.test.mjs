import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { launch, openApp, moneyStats, emptyReceivables } from "./harness.mjs";

const pad = (n) => String(n).padStart(2, "0");
const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const daysAgo = (n) => { const d = new Date(); d.setDate(d.getDate() - n); return ymd(d); };

const MB = { employee: "Murat Baykal", employeeId: "55", nameOf: "Employee" };
const entry = (o) => ({ ...MB, hours: 0, minutes: 0, itemId: "5", service: "PR",
  customer: "A", customerId: "10", projectId: null, billable: true,
  billableStatus: "Billable", hourlyRate: 250, date: daysAgo(5), ...o });
const baseData = (entries, extra = {}) => ({
  entries, employees: [{ id: "55", name: "Murat Baykal" }], items: [{ id: "5", name: "PR" }],
  projects: { projects: [], clients: [{ id: "10", name: "A" }] }, ...extra });

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser.close(); });

test("practice KPIs: utilization / realization / effective rate", async () => {
  const entries = [
    entry({ id: "1", hours: 10, billableStatus: "Billable" }),          // 10h billable, not invoiced
    entry({ id: "2", hours: 8, billableStatus: "HasBeenBilled" }),      // 8h billable, invoiced
    entry({ id: "3", hours: 2, service: "ADMIN", itemId: "6", billable: false, billableStatus: "NotBillable" }),
  ];
  const { ctx, page, errors } = await openApp(browser, baseData(entries), "dash");
  await page.waitForSelector(".money .mstat");
  const k = await moneyStats(page, "Practice");
  // billable 18h / total 20h = 90%; invoiced 2000 / billable-value 4500 = 44%; 4500 / 20h = $225
  assert.equal(k["Utilization"], "90%");
  assert.equal(k["Realization"], "44%");
  assert.equal(k["Effective / hr"], "$225");
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("mileage/expense lines are excluded from KPIs regardless of the hide toggle", async () => {
  const entries = [
    entry({ id: "1", hours: 10 }),                                        // $2500 labor
    entry({ id: "2", hours: 40, service: "Mileage", itemId: "7", hourlyRate: 1 }), // 40h, $40 pass-through
  ];
  const { ctx, page } = await openApp(browser, baseData(entries), "dash");
  await page.waitForSelector(".money .mstat");
  const k = await moneyStats(page, "Practice");
  // mileage excluded → effective rate = 2500 / 10h = $250 (would be ~$51 if the 40h counted)
  assert.equal(k["Effective / hr"], "$250");
  assert.equal(k["Utilization"], "100%");
  await ctx.close();
});

test("client concentration rolls sub-clients/projects up to the main client, and splits by project", async () => {
  // Acme(1) → East Wing job(2) → Roof project(3); Beta(4) top-level
  const projects = { projects: [{ id: "3", name: "Acme:East Wing:Roof", parentId: "2" }],
    clients: [{ id: "1", name: "Acme", parentId: null }, { id: "2", name: "Acme:East Wing", parentId: "1" },
              { id: "4", name: "Beta", parentId: null }] };
  const entries = [
    entry({ id: "a", customerId: "3", customer: "Acme:East Wing:Roof", hours: 24 }), // $6000
    entry({ id: "b", customerId: "2", customer: "Acme:East Wing", hours: 8 }),         // $2000
    entry({ id: "c", customerId: "4", customer: "Beta", hours: 8 }),                   // $2000
  ];
  const { ctx, page } = await openApp(browser, baseData(entries, { projects }), "dash");
  await page.waitForSelector("#concSeg");
  const client = await moneyStats(page, "concentration");
  assert.equal(client["Top client"], "80%");        // Acme = 6000 + 2000 of 10000
  assert.equal(client["Clients billed"], "2");
  await page.click("#concSeg button[data-concby=project]");
  await page.waitForTimeout(200);
  const proj = await moneyStats(page, "concentration");
  assert.equal(proj["Top project"], "60%");         // Roof alone
  assert.equal(proj["Projects billed"], "3");
  await ctx.close();
});

test("subcontractor margin, incl. negative margin formatting/colour", async () => {
  const data = baseData([
    entry({ id: "1", hours: 20, hourlyRate: 250 }),                                   // MB $5000
    { ...entry({ id: "2", hours: 40, hourlyRate: 150 }), employee: "Sub Co", employeeId: undefined, vendorId: "9", nameOf: "Vendor" }, // sub $6000 → revenue $11000
  ], { vendors: [{ id: "9", name: "Sub Co" }], bills: [{ id: "b1", date: daysAgo(4), amount: 14000, vendor: "Sub Co", kind: "bill" }] });
  const { ctx, page } = await openApp(browser, data, "dash");
  await page.waitForSelector("#dashMargin .mstat");
  const m = await moneyStats(page, "Subcontractor");
  assert.equal(m["Revenue (billable)"], "$11K");
  assert.equal(m["Gross margin"], "-$3.0K");        // 11000 - 14000, formatted with leading minus
  const negRed = await page.$$eval("#dashMargin .mstat .mnum", (els) =>
    els.some((e) => e.textContent.includes("-$3.0K") && e.classList.contains("bad")));
  assert.ok(negRed, "negative gross margin should be red");
  await ctx.close();
});

test("unbilled WIP is stable across the Day/Week/Month toggle", async () => {
  // one unbilled entry 500 days ago (outside Day/Week fetch, inside 2-yr WIP) + one recent
  const entries = [
    entry({ id: "old", hours: 40, date: daysAgo(500) }),   // $10000
    entry({ id: "new", hours: 12, date: daysAgo(3) }),     // $3000
  ];
  const { ctx, page } = await openApp(browser, baseData(entries), "dash");
  await page.waitForSelector(".money .mstat");
  const wipOf = () => moneyStats(page, "Accounts receivable").then((s) => s["Unbilled WIP"]);
  const monthWip = await wipOf();
  await page.click("#dashSeg button[data-unit=day]");
  await page.waitForTimeout(400);
  const dayWip = await wipOf();
  assert.equal(monthWip, dayWip, "WIP must not change with the unit toggle");
  assert.equal(monthWip, "$13K");                          // 10000 + 3000, both counted
  await ctx.close();
});

test("dashboard chart renders gridlines + one bar per bucket (shared axis engine)", async () => {
  const entries = [
    entry({ id: "1", hours: 8, billableStatus: "HasBeenBilled" }),   // invoiced → charted
    entry({ id: "2", hours: 4, date: daysAgo(40), billableStatus: "HasBeenBilled" }),
  ];
  const { ctx, page, errors } = await openApp(browser, baseData(entries), "dash");
  await page.waitForSelector("#dashChart .cols");
  const gridlines = await page.$$eval("#dashChart .gridline", (e) => e.length);
  const bars = await page.$$eval("#dashChart .cols .slot", (e) => e.length);
  const ticks = await page.$$eval("#dashChart .tick", (e) => e.map((t) => t.textContent));
  assert.ok(gridlines >= 2, "chart should draw gridlines");
  assert.equal(bars, 12, "month view = 12 bucket slots");
  assert.ok(ticks.every((t) => /h$/.test(t)), "hours-mode ticks end in h");
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("receivables card renders aging + who-owes from /api/receivables", async () => {
  const receivables = { asOf: "2026-07-05", outstanding: 3500, pastDue: 2500,
    aging: { "0-30": 1000, "31-60": 2000, "61-90": 0, "90+": 500 }, dso: 36, billed365: 35000,
    byClient: [{ customer: "Beta", customerId: "20", balance: 2000, count: 1 },
               { customer: "Alpha", customerId: "10", balance: 1500, count: 2 }],
    invoices: [{ id: "1", customer: "Alpha", customerId: "10", balance: 1000, overdue: false, bucket: "0-30" }] };
  const { ctx, page } = await openApp(browser, baseData([], { receivables }), "dash");
  await page.waitForSelector(".money .agebar");
  const s = await moneyStats(page, "Accounts receivable");
  assert.equal(s["Outstanding"], "$3.5K");
  assert.equal(s["Past due"], "$2.5K");
  assert.equal(s["Days to pay (DSO)"], "36");
  const owe = await page.$$eval(".money .owe-row .nm", (els) => els.map((e) => e.textContent.trim()));
  assert.deepEqual(owe, ["Beta$2,000", "Alpha$1,500"]);   // largest balance first
  await ctx.close();
});
