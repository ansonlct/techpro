const CACHE = "hk-risk-monitor-version-1";
const APP_SHELL = [
  "./",
  "./index.html",
  "./assets/styles.css",
  "./assets/app.js",
  "./manifest.json",
  "./icons/icon.svg",
  "./icons/icon-192.png",
  "./icons/icon-512.png",
];

async function cacheSuccessful(cache, request, response) {
  if (response && response.ok && response.type !== "opaque") {
    await cache.put(request, response.clone());
  }
  return response;
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(() => caches.match("./index.html"))
    );
    return;
  }

  if (url.pathname.includes("/data/")) {
    const stableKey = new Request(`${url.origin}${url.pathname}`);
    event.respondWith(
      fetch(request, { cache: "no-store" })
        .then((response) => caches.open(CACHE).then((cache) => cacheSuccessful(cache, stableKey, response)))
        .catch(async () => {
          const cached = await caches.match(stableKey);
          return cached || new Response(JSON.stringify({ error: "offline-data-unavailable" }), {
            status: 503,
            headers: { "Content-Type": "application/json; charset=utf-8" },
          });
        })
    );
    return;
  }

  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) return cached;
      return fetch(request).then((response) =>
        caches.open(CACHE).then((cache) => cacheSuccessful(cache, request, response))
      );
    })
  );
});
