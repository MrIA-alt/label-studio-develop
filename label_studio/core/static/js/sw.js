// A resolver response is an authorization decision for the current session.
// Never reuse a previous session's presigned destination from Cache Storage.
const LEGACY_MEDIA_CACHE_PREFIX = "ls-cache-";

self.addEventListener("install", (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then(async (names) => {
      await Promise.all(
        names.filter((name) => name.startsWith(LEGACY_MEDIA_CACHE_PREFIX)).map((name) => caches.delete(name)),
      );
      await self.clients.claim();
    }),
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.origin === self.location.origin && /\/(?:presign|resolve)\//.test(url.pathname)) {
    // Keep the original credentials, range and request mode, while asking the
    // server to recheck access even when an earlier request succeeded.
    event.respondWith(fetch(event.request, { cache: "no-store" }));
  }
});
