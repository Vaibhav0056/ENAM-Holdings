/* FINN service worker: Web Push display + click-through. No offline caching (data must be fresh). */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let payload = { title: "FINN", body: "New alert", data: { url: "/#alerts" } };
  try { if (event.data) payload = Object.assign(payload, event.data.json()); } catch (_) {
    try { payload.body = event.data.text(); } catch (_) {}
  }
  event.waitUntil(self.registration.showNotification(payload.title, {
    body: payload.body,
    icon: "/static/icons/icon-192.png",
    badge: "/static/icons/icon-192.png",
    data: payload.data || { url: "/#alerts" },
    tag: (payload.data && payload.data.url) || "finn",
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL((event.notification.data && event.notification.data.url) || "/#alerts", self.location.origin).href;
  event.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const c of all) {
      if (new URL(c.url).origin === self.location.origin) {
        await c.focus();
        c.postMessage({ type: "navigate", url });
        return;
      }
    }
    await self.clients.openWindow(url);
  })());
});
