const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = readFileSync(
  process.env.LS_SW_TEST_SOURCE ?? path.resolve(__dirname, "../../core/static/js/sw.js"), "utf8",
);

function worker() {
  const handlers = new Map();
  const network = [];
  const lookups = [];
  const lifecycle = [];
  const context = {
    URL, Headers, Response, Date, console,
    setInterval: () => 1,
    self: {
      location: new URL("https://studio.example.test"),
      addEventListener: (name, handler) => handlers.set(name, handler),
      skipWaiting: async () => lifecycle.push("skip-waiting"),
      clients: { claim: async () => lifecycle.push("claim") },
    },
    caches: {
      keys: async () => ["ls-cache-v1", "unrelated-cache"],
      delete: async (name) => lifecycle.push(`delete:${name}`),
      match: async (request) => {
        lookups.push(request);
        return { headers: new Headers({
          Location: "https://storage.example.test/private-image",
          "x-cache-expiration": String(Date.now() + 50_000),
        }) };
      },
      open: async () => ({ keys: async () => [], put: async () => {}, delete: async () => {} }),
    },
    fetch: async (request, options) => {
      network.push({ request, options });
      if (typeof request === "string") return { status: 200, body: "previous-session-data" };
      return { status: request.headers.get("X-Test-Actor") === "alice" ? 200 : 403 };
    },
  };
  vm.runInNewContext(source, context, { filename: "sw.js" });
  return { handlers, network, lookups, lifecycle };
}

function request(actor, pathname = "/tasks/42/resolve/?fileuri=source", range) {
  const headers = new Headers({ "X-Test-Actor": actor });
  if (range) headers.set("Range", range);
  return {
    url: new URL(pathname, "https://studio.example.test").href,
    referrer: "https://studio.example.test/projects/1/data",
    headers,
  };
}

async function dispatchFetch(instance, incoming) {
  let result;
  await instance.handlers.get("fetch")({ request: incoming, respondWith(value) { result = value; } });
  return result === undefined ? undefined : await result;
}

test("a cached media grant is not reused after the caller changes", async () => {
  const instance = worker();
  const alice = request("alice");
  const bob = request("bob");
  assert.equal((await dispatchFetch(instance, alice)).status, 200);
  assert.equal((await dispatchFetch(instance, bob)).status, 403);
  assert.equal(instance.lookups.length, 0);
  assert.equal(instance.network.length, 2);
  assert.equal(instance.network[0].request, alice);
  assert.equal(instance.network[1].request, bob);
  assert.equal(instance.network[1].options.cache, "no-store");
});

test("range requests keep their headers and also recheck authorization", async () => {
  const instance = worker();
  const incoming = request("alice", "/tasks/42/presign/?file=source", "bytes=0-1023");
  assert.equal((await dispatchFetch(instance, incoming)).status, 200);
  assert.equal(instance.network[0].request.headers.get("Range"), "bytes=0-1023");
  assert.equal(instance.network[0].options.cache, "no-store");
});

test("ordinary and foreign-origin requests use the normal browser network path", async () => {
  const instance = worker();
  assert.equal(await dispatchFetch(instance, request("alice", "/api/projects/1/")), undefined);
  assert.equal(await dispatchFetch(instance, request("alice", "https://other.example.test/resolve/")), undefined);
  assert.equal(instance.network.length, 0);
});

test("the replacement worker activates in open tabs and clears only its legacy cache", async () => {
  const instance = worker();
  const pending = [];
  instance.handlers.get("install")({ waitUntil(value) { pending.push(value); } });
  await Promise.all(pending);
  instance.handlers.get("activate")({ waitUntil(value) { pending.push(value); } });
  await Promise.all(pending);
  assert.deepEqual(instance.lifecycle, ["skip-waiting", "delete:ls-cache-v1", "claim"]);
});
