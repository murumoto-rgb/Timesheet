// Service worker: keeps the installed (home-screen) app on the latest build,
// shows the daily reminder, and focuses the app on tap.
// Bump SW_VERSION on each deploy so this worker re-activates and purges any
// stale app-shell cache left by an earlier build.
const SW_VERSION = "2026.07.05.7";

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
    event.respondWith(fetch(event.request));
  }
});

self.addEventListener("push", (event) => {
  event.waitUntil(
    self.registration.showNotification("Timesheet", {
      body: "Don't forget to log today's hours.",
      icon: "/static/icon-192.png",
      badge: "/static/icon-192.png",
      tag: "daily-reminder",
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
