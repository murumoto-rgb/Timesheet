// Service worker: keeps the installed (home-screen) app on the latest build,
// shows the daily reminder, and focuses the app on tap.
// Bump SW_VERSION on each deploy so this worker re-activates and purges any
// stale app-shell cache left by an earlier build.
const SW_VERSION = "2026.07.13.1";

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    // Delete every cache from earlier builds — a previous version may have
    // cached index.html and left the home-screen app stuck on an old build.
    const keys = await caches.keys();
    await Promise.all(keys.map((k) => caches.delete(k)));
    await self.clients.claim();
  })());
});

// Always pull the page itself from the network so the installed app can never
// get stuck on a stale build. (Other requests pass straight through.)
self.addEventListener("fetch", (event) => {
  if (event.request.mode === "navigate") {
    event.respondWith(fetch(event.request).catch(() => new Response(
      '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">' +
      '<body style="margin:0;background:#12161c;color:#e6ebf1;font:16px system-ui;padding:32px">' +
      '<h1 style="font-size:24px">Timesheet is offline</h1>' +
      '<p style="color:#aab4c0;line-height:1.5">Reconnect to the internet, then reopen or reload the app. No entry was submitted.</p>' +
      '<a href="/" style="display:inline-block;padding:12px 18px;border-radius:10px;background:#4c9be8;color:#06121f;font-weight:650;text-decoration:none">Try again</a>',
      { status: 503, headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" } }
    )));
  }
});

self.addEventListener("push", (event) => {
  // Payloadless push: pick the message by weekday — Friday is the weekly review
  // nudge, other days the daily log reminder. (The live numbers live in-app.)
  const isReviewDay = new Date().getDay() === 5;  // Fri
  const body = isReviewDay
    ? "Weekly review — check your gaps and any entries that need attention."
    : "Don't forget to log today's hours.";
  event.waitUntil(
    self.registration.showNotification("Timesheet", {
      body,
      icon: "/static/icon-192.png",
      badge: "/static/icon-192.png",
      tag: isReviewDay ? "weekly-review" : "daily-reminder",
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      for (const c of list) {
        if (c.url.startsWith(self.location.origin) && "focus" in c) return c.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow("/");
    })
  );
});
