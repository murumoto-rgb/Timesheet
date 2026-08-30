// Frontend regression harness: loads index.html in headless Chromium with all
// /api/* endpoints mocked from a plain-JS dataset, so tests assert the values
// the dashboard/report/week views actually render. Behaviour-level (not
// internal) so it survives refactors — the whole point of a regression net.
//
// Setup: npm ci && npx playwright install chromium
// Run: tests/frontend/run.sh
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { chromium, devices } from "playwright";

const HERE = dirname(fileURLToPath(import.meta.url));
const HTML = readFileSync(join(HERE, "../../index.html"), "utf8");
const EXEC = process.env.PLAYWRIGHT_CHROMIUM;

export function launch() {
  return chromium.launch(EXEC ? { executablePath: EXEC } : {});
}

export function emptyReceivables(asOf = "2026-07-05") {
  return { asOf, outstanding: 0, pastDue: 0, aging: { "0-30": 0, "31-60": 0, "61-90": 0, "90+": 0 },
           dso: null, billed365: 0, byClient: [], invoices: [] };
}

const DEFAULT_STATUS = { connected: true, environment: "production", configured: true,
                         auth_required: false, mfa_required: false, authed: true, companyKey: "fixture-company" };

// Open the app with mocked APIs. `data` fields: status, projects ({projects,clients}),
// employees, vendors, items, entries, payments, bills, receivables. Returns
// { ctx, page, errors } — caller closes ctx. `view` optionally clicks a bottom tab.
export async function openApp(browser, data = {}, view) {
  // Service-worker script requests bypass page routes. Disable registration in
  // this fully mocked harness so no background request reaches the real network.
  const ctx = await browser.newContext({ ...devices["iPhone 13"], serviceWorkers: "block" });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => { if (m.type() === "error") errors.push("console: " + m.text()); });

  const json = (d) => (r) => r.fulfill({ json: d });
  const inRange = (arr, url) => {
    const u = new URL(url), s = u.searchParams.get("start"), e = u.searchParams.get("end");
    return arr.filter((x) => (!s || x.date >= s) && (!e || x.date <= e));
  };

  // catch-all FIRST (lowest priority) so any unmocked /api/* returns {} instead
  // of hitting the network and hanging; specifics registered after win.
  await page.route("**/api/**", json({}));
  await page.route("https://app.test/", (r) => r.fulfill({ contentType: "text/html", body: HTML }));
  await page.route("**/static/**", (r) => r.fulfill({ status: 204, body: "" }));
  await page.route("**/static/workspace.*", (r) => {
    const file = new URL(r.request().url()).pathname.split("/").pop();
    if (!["workspace.js", "workspace.css"].includes(file)) return r.abort();
    return r.fulfill({ contentType: file.endsWith(".js") ? "text/javascript" : "text/css", body: readFileSync(join(HERE, "../../static", file), "utf8") });
  });
  await page.route("**/sw.js", (r) => r.fulfill({ contentType: "text/javascript", body: "" }));
  await page.route("**/api/status", json(data.status || DEFAULT_STATUS));
  await page.route("**/api/projects", json(data.projects || { projects: [], clients: [] }));
  await page.route("**/api/employees", json(data.employees || []));
  await page.route("**/api/vendors", json(data.vendors || []));
  await page.route("**/api/items", json(data.items || []));
  await page.route("**/api/receivables", json(data.receivables || emptyReceivables()));
  await page.route("**/api/timeactivities*", (r) => r.fulfill({ json: inRange(data.entries || [], r.request().url()) }));
  await page.route("**/api/payments*", (r) => r.fulfill({ json: inRange(data.payments || [], r.request().url()) }));
  await page.route("**/api/bills*", (r) => r.fulfill({ json: inRange(data.bills || [], r.request().url()) }));

  await page.goto("https://app.test/");
  await page.waitForSelector("#app", { state: "visible" });
  await page.waitForTimeout(200);
  if (view) { await page.click(`#tabbar button[data-view=${view}]`); await page.waitForTimeout(300); }
  return { ctx, page, errors };
}

// Read the .mstat blocks of a named .money card ("Practice", "concentration",
// "Subcontractor", "Accounts receivable") as { label: value }.
export async function moneyStats(page, titleIncludes) {
  return page.$$eval(".money", (cards, t) => {
    const c = cards.find((x) => x.querySelector("h2") && x.querySelector("h2").textContent.includes(t));
    if (!c) return null;
    const out = {};
    c.querySelectorAll(".mstat").forEach((m) => { out[m.querySelector(".mlbl").textContent.trim()] = m.querySelector(".mnum").textContent.trim(); });
    return out;
  }, titleIncludes);
}
