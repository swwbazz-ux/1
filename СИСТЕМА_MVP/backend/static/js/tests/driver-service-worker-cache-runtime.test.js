"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const views = fs.readFileSync(path.resolve(__dirname, "../../../users/views.py"), "utf8");

function renderedWorkerSource() {
    const match = views.match(/DRIVER_SERVICE_WORKER_JS = f"""([\s\S]*?)"""\.strip\(\)/);
    assert.ok(match);
    return match[1]
        .replaceAll("{json.dumps(APP_CONTRACT_VERSION)}", JSON.stringify("test-contract"))
        .replaceAll("{DRIVER_SHELL_VERSION}", "driver-mobile-shell-v213")
        .replaceAll("{{", "{")
        .replaceAll("}}", "}");
}

class Headers {
    constructor(values = {}) { this.values = new Map(Object.entries(values).map(([key, value]) => [key.toLowerCase(), value])); }
    get(name) { return this.values.get(String(name).toLowerCase()) || null; }
}

class FakeRequest {
    constructor(input, options = {}) {
        this.url = new URL(typeof input === "string" ? input : input.url, "https://driverform.ru").href;
        this.method = options.method || input.method || "GET";
        this.mode = options.mode || input.mode || "same-origin";
        this.headers = options.headers || input.headers || new Headers();
    }
}

class FakeResponse {
    constructor(body, {status = 200, url = "https://driverform.ru/static/test", contentType = "text/plain"} = {}) {
        this.body = body;
        this.status = status;
        this.ok = status >= 200 && status < 300;
        this.url = url;
        this.headers = new Headers({"Content-Type": contentType});
    }
    clone() { return new FakeResponse(this.body, {status: this.status, url: this.url, contentType: this.headers.get("Content-Type")}); }
    text() { return Promise.resolve(this.body); }
    static error() { return new FakeResponse("", {status: 500}); }
}

class MemoryCache {
    constructor(fetcher) { this.entries = new Map(); this.fetcher = fetcher; }
    key(input) { return new URL(typeof input === "string" ? input : input.url, "https://driverform.ru").href; }
    async keys() { return [...this.entries.keys()].map((url) => new FakeRequest(url)); }
    async match(input) { const value = this.entries.get(this.key(input)); return value ? value.clone() : undefined; }
    async put(input, response) { this.entries.set(this.key(input), response.clone()); }
    async addAll(urls) {
        for (const url of urls) await this.put(url, await this.fetcher(new FakeRequest(url)));
    }
}

test("expired-session update migrates the complete validated driver cache before deleting the old one", async () => {
    const listeners = new Map();
    const stores = new Map();
    const deleted = [];
    const fetcher = async (request) => {
        const pathname = new URL(request.url).pathname;
        if (pathname === "/driver/" || pathname === "/driver/shift/") {
            return new FakeResponse("<html>login</html>", {
                url: "https://driverform.ru/",
                contentType: "text/html; charset=utf-8",
            });
        }
        return new FakeResponse("new:" + pathname, {url: request.url});
    };
    const oldName = "driver-mobile-shell-v212";
    const oldCache = new MemoryCache(fetcher);
    stores.set(oldName, oldCache);
    await oldCache.put("/driver/", new FakeResponse(
        '<main data-driver-shell data-driver-access-id="77">old authenticated shell</main>',
        {url: "https://driverform.ru/driver/", contentType: "text/html; charset=utf-8"}
    ));
    await oldCache.put("/static/js/exact-old-driver.js?v=212", new FakeResponse(
        "exact old asset",
        {url: "https://driverform.ru/static/js/exact-old-driver.js?v=212"}
    ));

    const caches = {
        async open(name) {
            if (!stores.has(name)) stores.set(name, new MemoryCache(fetcher));
            return stores.get(name);
        },
        async keys() { return [...stores.keys()]; },
        async delete(name) { deleted.push(name); return stores.delete(name); },
    };
    const self = {
        location: {origin: "https://driverform.ru"},
        clients: {claim: async () => {}},
        skipWaiting: async () => {},
        addEventListener(name, callback) { listeners.set(name, callback); },
    };
    vm.runInNewContext(renderedWorkerSource(), {
        self,
        caches,
        fetch: fetcher,
        Request: FakeRequest,
        Response: FakeResponse,
        URL,
        Promise,
        Error,
        String,
    });

    let installPromise;
    listeners.get("install")({waitUntil(value) { installPromise = value; }});
    await installPromise;

    assert.equal(stores.has(oldName), true);
    assert.deepEqual(deleted, []);
    const current = stores.get("driver-mobile-shell-v213");
    assert.match((await current.match("/driver/")).body, /old authenticated shell/);
    assert.equal((await current.match("/static/js/exact-old-driver.js?v=212")).body, "exact old asset");

    let activatePromise;
    listeners.get("activate")({waitUntil(value) { activatePromise = value; }});
    await activatePromise;
    assert.deepEqual(deleted, [oldName]);
    assert.equal(stores.has(oldName), false);
    assert.equal((await current.match("/static/js/exact-old-driver.js?v=212")).body, "exact old asset");
});
