import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { launch, openApp } from "./harness.mjs";

const pad = (n) => String(n).padStart(2, "0");
const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const today = () => ymd(new Date());

const MB = { employee: "Murat Baykal", employeeId: "55", nameOf: "Employee" };
const entry = (o) => ({ ...MB, hours: 0, minutes: 0, itemId: "5", service: "PR",
  customer: "A", customerId: "10", projectId: null, billable: true,
  billableStatus: "Billable", hourlyRate: 250, date: today(), ...o });
const data = (entries) => ({ entries, employees: [{ id: "55", name: "Murat Baykal" }],
  items: [{ id: "5", name: "PR" }],
  projects: { projects: [], clients: [{ id: "10", name: "Acme" }, { id: "20", name: "Beta" }] } });

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser.close(); });

test("billed rows: badge shown, delete button omitted; unbilled rows keep delete", async () => {
  const entries = [
    entry({ id: "B1", hours: 3, customer: "Acme", customerId: "10", billableStatus: "HasBeenBilled" }),
    entry({ id: "N1", hours: 1, customer: "Beta", customerId: "20", billableStatus: "Billable" }),
  ];
  const { ctx, page, errors } = await openApp(browser, data(entries));
  await page.waitForSelector("#entries .entry");
  const rows = await page.$$eval("#entries .entry", (els) => els.map((r) => ({
    billed: !!r.querySelector(".bill.billed"),
    hasDelete: !!r.querySelector(".del:not(.rep)"),
  })));
  const billedRow = rows.find((r) => r.billed);
  const normalRow = rows.find((r) => !r.billed);
  assert.ok(billedRow && !billedRow.hasDelete, "billed row: badge present, no delete button");
  assert.ok(normalRow && normalRow.hasDelete, "unbilled row keeps its delete button");
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("tapping a billed entry opens a locked, read-only form; Close unlocks it", async () => {
  const entries = [entry({ id: "B1", hours: 3, customer: "Acme", customerId: "10", billableStatus: "HasBeenBilled" })];
  const { ctx, page } = await openApp(browser, data(entries));
  await page.waitForSelector("#entries .entry .meta");
  await page.click("#entries .entry .meta");
  await page.waitForTimeout(200);
  const locked = await page.evaluate(() => ({
    banner: getComputedStyle(document.getElementById("billedBanner")).display !== "none",
    formBilled: document.getElementById("logForm").classList.contains("billed"),
    submitHidden: getComputedStyle(document.getElementById("submit")).display === "none",
    closeShown: getComputedStyle(document.getElementById("closeBilled")).display !== "none",
    durDisabled: document.getElementById("durh").disabled,
    billableDisabled: document.getElementById("billable").disabled,
  }));
  assert.deepEqual(locked, { banner: true, formBilled: true, submitHidden: true,
    closeShown: true, durDisabled: true, billableDisabled: true });
  await page.click("#closeBilled");
  await page.waitForTimeout(150);
  const after = await page.evaluate(() => ({
    formBilled: document.getElementById("logForm").classList.contains("billed"),
    durDisabled: document.getElementById("durh").disabled,
    submitShown: getComputedStyle(document.getElementById("submit")).display !== "none",
  }));
  assert.deepEqual(after, { formBilled: false, durDisabled: false, submitShown: true });
  await ctx.close();
});
