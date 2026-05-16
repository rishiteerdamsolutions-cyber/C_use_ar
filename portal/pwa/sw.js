const CACHE = "cusear-pwa-v1";
const SHELL = ["/app/", "/app/index.html", "/app/app.css", "/app/app.js", "/app/config.js", "/app/manifest.webmanifest"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || !url.pathname.startsWith("/app/")) {
    return;
  }
  if (event.request.method !== "GET") {
    return;
  }
  const isDoc = event.request.mode === "navigate" || event.request.destination === "document";
  const isShell =
    url.pathname === "/app/" ||
    url.pathname === "/app/index.html" ||
    url.pathname.endsWith(".css") ||
    url.pathname.endsWith(".js") ||
    url.pathname.endsWith(".webmanifest");

  if (!isDoc && !isShell) {
    return;
  }

  event.respondWith(
    fetch(event.request)
      .then((res) => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(event.request, copy));
        }
        return res;
      })
      .catch(() => caches.match(event.request).then((r) => r || caches.match("/app/index.html")))
  );
});
