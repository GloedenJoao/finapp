// Basic offline shell: cache core pages & static assets
const CACHE = "finapp-cache-v1";
const CORE = [
  "/",                // home
  "/login",
  "/signup",
  "/transactions",
  "/static/manifest.webmanifest"
  // (Bootstrap CSS is from CDN; it’ll still work online. We can cache it later.)
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(CORE)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// Network-first for HTML; cache-first for everything else
self.addEventListener("fetch", (e) => {
  const req = e.request;
  const isHTML = req.headers.get("accept")?.includes("text/html");

  if (isHTML) {
    e.respondWith(
      fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
        return res;
      }).catch(() => caches.match(req).then((res) => res || caches.match("/")))
    );
  } else {
    e.respondWith(
      caches.match(req).then((res) => res || fetch(req).then((live) => {
        const copy = live.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
        return live;
      }))
    );
  }
});