/* FINN service worker.
   - App shell (HTML, CSS, JS, icons, Alpine) is served from cache and refreshed in the background
     (stale-while-revalidate), so the shell paints in well under 300 ms on repeat visits.
   - API data is never cached: stale financial data is worse than none. The page shows its own "stale" notice.
   - Web Push display + click-through to the alert's card. */
const SHELL = "finn-shell-v9";
const SHELL_URLS = ["/", "/static/app.css", "/static/app.js", "/static/charts.js", "/manifest.json", "/static/icons/icon-192.png",
                    "/static/vendor/alpine-3.14.8.min.js"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_URLS)).catch(() => {}).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) if (k !== SHELL) await caches.delete(k);
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  const isShell = SHELL_URLS.includes(url.origin === self.location.origin ? url.pathname : req.url) ||
                  (url.origin === self.location.origin && url.pathname.startsWith("/static/icons/"));
  if (!isShell) return;   // API and everything else: straight to the network
  event.respondWith((async () => {
    const cache = await caches.open(SHELL);
    const cached = await cache.match(req, { ignoreSearch: true });
    const fresh = fetch(req).then((res) => { if (res.ok) cache.put(req, res.clone()); return res; }).catch(() => null);
    return cached || (await fresh) || Response.error();
  })());
});

self.addEventListener("push", (event) => {
  let payload = { title: "FINN", body: "New alert", data: { url: "/#alerts" } };
  try { if (event.data) payload = Object.assign(payload, event.data.json()); } catch (_) {
    try { payload.body = event.data.text(); } catch (_) {}
  }
  event.waitUntil(self.registration.showNotification(payload.title, {
    body: payload.body, icon: "/static/icons/icon-192.png", badge: "/static/icons/icon-192.png",
    data: payload.data || { url: "/#alerts" }, tag: (payload.data && payload.data.url) || "finn",
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
