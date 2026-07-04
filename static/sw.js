// Service worker: shows the daily reminder and focuses the app on tap.
// The reminder message is fixed here (server sends a payloadless push).
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
