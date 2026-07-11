import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { launch, openApp } from "./harness.mjs";

const pad = (n) => String(n).padStart(2, "0");
const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const today = () => ymd(new Date());

const FULL = "Northview:Northview Tower Roof Remediation";
const MB = { employee: "Murat Baykal", employeeId: "55", nameOf: "Employee" };
const entry = (o) => ({ ...MB, date: today(), hours: 2, minutes: 0, itemId: "5", service: "PR",
  billable: true, billableStatus: "Billable", hourlyRate: 250, ...o });
// picker knows the live project P1 (Customer.Id) under client C1
const projects = { projects: [{ id: "P1", name: FULL, parentId: "C1" }],
  clients: [{ id: "C1", name: "Northview", parentId: null }] };
const data = (entries) => ({ entries, employees: [{ id: "55", name: "Murat Baykal" }],
  items: [{ id: "5", name: "PR" }], projects });

async function capturePost(page) {
  const posts = [];
  await page.route("**/api/timeactivity", (route) =>
    route.request().method() === "POST"
      ? (posts.push(route.request().postDataJSON()), route.fulfill({ json: { Id: "new1" } }))
      : route.fulfill({ json: {} }));
  return posts;
}

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser.close(); });

test("a recent chip fills the project field with the full Client:Sub name and logs the live id", async () => {
  const { ctx, page, errors } = await openApp(browser, data([entry({ id: "1", customer: FULL, customerId: "P1" })]));
  const posts = await capturePost(page);
  await page.waitForSelector("#recentChips .chip");
  const chip = await page.$("#recentChips .chip");
  assert.equal(await chip.getAttribute("title"), FULL, "chip carries the full qualified name");
  await chip.click();
  assert.equal((await page.textContent("#projectName")).trim(), FULL, "field shows the full Client:Sub name");
  await page.fill("#durh", "1");
  await page.click("#submit");
  await page.waitForSelector("#toast", { state: "visible" });
  assert.equal(posts.length, 1);
  assert.equal(posts[0].project_id, "P1");   // the live project's Customer.Id
  assert.equal(posts[0].customer_id, "C1");  // its parent client
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("a recent entry with a STALE id resolves to the live project by its stored name", async () => {
  // the entry was logged under an old id that no longer exists in the picker;
  // the chip must recover the current project (P1) by matching FULL, not send OLD999
  const { ctx, page, errors } = await openApp(browser, data([entry({ id: "1", customer: FULL, customerId: "OLD999" })]));
  const posts = await capturePost(page);
  await page.waitForSelector("#recentChips .chip");
  await page.click("#recentChips .chip");
  assert.equal((await page.textContent("#projectName")).trim(), FULL);
  await page.fill("#durh", "1");
  await page.click("#submit");
  await page.waitForSelector("#toast", { state: "visible" });
  assert.equal(posts[0].project_id, "P1", "recovered the live id, not the stale one");
  assert.notEqual(posts[0].customer_id, "OLD999");
  assert.deepEqual(errors, []);
  await ctx.close();
});

test("a recent project that no longer exists (id + name gone) is dropped, not offered", async () => {
  const { ctx, page, errors } = await openApp(browser, data([entry({ id: "1", customer: "Ghost Project", customerId: "GONE" })]));
  await page.waitForTimeout(400);   // let loadFrequents run
  assert.equal((await page.$$("#recentChips .chip")).length, 0, "no chip for an unresolvable project");
  assert.equal(await page.isVisible("#recentCard"), false, "the recent card hides when nothing resolves");
  assert.deepEqual(errors, []);
  await ctx.close();
});
