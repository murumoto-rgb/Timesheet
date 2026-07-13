import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { launch, openApp } from "./harness.mjs";

const base = () => ({ entries: [], employees: [{ id: "55", name: "Murat Baykal" }],
  items: [{ id: "5", name: "PR" }], projects: { projects: [], clients: [{ id: "10", name: "Acme" }] } });

// The QBO fault the backend surfaces for a stale/unknown reference (code 2500).
const QBO_FAULT = JSON.stringify({ detail: '{"Fault":{"Error":[{"Message":"Invalid Reference Id",' +
  '"Detail":"Invalid Reference Id : Names element id 540123582 not found","code":"2500"}],' +
  '"type":"ValidationFault"}}' });

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser.close(); });

test("a QBO 'Invalid Reference Id' fault shows an actionable message + reload, not raw JSON", async () => {
  const { ctx, page, errors } = await openApp(browser, base());
  await page.route("**/api/timeactivity", (route) =>
    route.request().method() === "POST"
      ? route.fulfill({ status: 400, body: QBO_FAULT })
      : route.fulfill({ json: {} }));
  await page.fill("#durh", "1");
  await page.click("#submit");
  await page.waitForSelector("#msg.bad");
  const txt = await page.textContent("#msg");
  assert.match(txt, /didn't recognize/);       // friendly, not "Failed: {...}"
  assert.match(txt, /540123582/);               // the offending id is surfaced
  assert.doesNotMatch(txt, /ValidationFault/);  // raw fault JSON is not dumped
  assert.equal(await page.isVisible("#reloadApp"), true, "offers a Reload button");
  // the deliberate 400 logs a browser "Failed to load resource" line; ignore it,
  // still guard against any real page/JS error
  assert.deepEqual(errors.filter((e) => !/Failed to load resource/.test(e)), []);
  await ctx.close();
});

test("an exact-duplicate warning requires explicit confirmation before retrying", async () => {
  const { ctx, page } = await openApp(browser, base());
  const bodies = [];
  await page.route("**/api/timeactivity", (route) => {
    const body = route.request().postDataJSON();
    bodies.push(body);
    if (!body.allow_duplicate) {
      return route.fulfill({ status: 409, json: { detail: {
        message: "An identical entry already exists for this person and date.", code: "DUPLICATE_ENTRY"
      } } });
    }
    return route.fulfill({ json: { Id: "new1", SyncToken: "0" } });
  });
  await page.fill("#durh", "1");
  await page.click("#submit");
  await page.waitForSelector("#confirmDialog:not([hidden])");
  assert.match(await page.textContent("#confirmText"), /identical entry/);
  await page.click("#confirmGo");
  await page.waitForSelector("#msg.good");
  assert.equal(bodies.length, 2);
  assert.equal(bodies[1].allow_duplicate, true);
  await ctx.close();
});
