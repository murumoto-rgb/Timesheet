import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { launch, openApp } from "./harness.mjs";

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser.close(); });

for (const desktop of [false, true]) {
  test(`setup, connection and MFA login screens retain phone layout (${desktop ? "desktop" : "phone"})`, async () => {
    for (const [readySelector, status] of [
      ["#setup", { configured: false, connected: false }],
      ["#connect", { configured: true, connected: false, authed: true }],
      ["#loginCard", { configured: true, connected: true, auth_required: true, mfa_required: true, authed: false }],
    ]) {
      const { ctx, page, errors } = await openApp(browser, { status: { environment: "production", ...status } }, undefined, { desktop, readySelector });
      try {
        assert.ok((await page.locator("body").boundingBox()).width <= 520);
        assert.equal(await page.locator(readySelector).isVisible(), true);
        if (readySelector === "#connect") assert.equal(await page.locator("#connect a[href='/connect']").count(), 1);
        if (readySelector === "#loginCard") {
          assert.equal(await page.locator("#codeRow").isVisible(), true);
          await page.route("**/login", route => route.fulfill({ status: 401, json: { detail: "Invalid password or authentication code." } }));
          await page.fill("#pw", "synthetic-invalid-password");
          await page.fill("#code", "123456");
          await page.click("#pwGo");
          await page.waitForSelector("#pwMsg.bad");
          assert.match(await page.locator("#pwMsg").textContent(), /Invalid password/);
          // A deliberately rejected mock login is not a browser/runtime error.
          assert.equal(await page.locator("#app").isVisible(), false);
        } else assert.deepEqual(errors, []);
      } finally { await ctx.close(); }
    }
  });
}
