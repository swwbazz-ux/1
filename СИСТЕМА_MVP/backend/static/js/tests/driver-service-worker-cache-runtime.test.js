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
        .replaceAll("{DRIVER_SHELL_VERSION}", "driver-mobile-shell-v315")
        .replaceAll("{{", "{")
        .replaceAll("}}", "}")
        .replaceAll("\\\\b", "\\b")
        .replaceAll("\\\\s", "\\s");
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
    async delete(input) { return this.entries.delete(this.key(input)); }
    async addAll(urls) {
        for (const url of urls) await this.put(url, await this.fetcher(new FakeRequest(url)));
    }
}

async function createExpiredSessionRuntime({missingDependency = "", freshShell = false} = {}) {
    const listeners = new Map();
    const stores = new Map();
    const deleted = [];
    let offline = false;
    const exactDependencies = [
        "/static/css/app.css?v=release-231",
        "/static/js/native-background-connection-v1.js?v=release-231",
        "/static/js/driver-offline-outbox-v2.js?v=driver-mobile-shell-v315",
    ];
    let shellText = "old authenticated shell";
    const shellMarkup = () => [
        '<link rel="stylesheet" href="' + exactDependencies[0] + '">',
        '<script src="' + exactDependencies[1] + '" defer></script>',
        '<script src="' + exactDependencies[2] + '"></script>',
        '<main data-driver-shell data-driver-access-id="77">' + shellText + '</main>',
    ].join("");
    const renderedDriverShell = shellMarkup();
    const fetcher = async (request) => {
        if (offline) throw new TypeError("offline");
        const url = new URL(request.url);
        const pathname = url.pathname;
        if (pathname === "/driver/" || pathname === "/driver/shift/") {
            if (freshShell) {
                return new FakeResponse(shellMarkup(), {
                    url: request.url,
                    contentType: "text/html; charset=utf-8",
                });
            }
            return new FakeResponse("<html>login</html>", {
                url: "https://driverform.ru/",
                contentType: "text/html; charset=utf-8",
            });
        }
        return new FakeResponse("new:" + pathname + url.search, {url: request.url});
    };
    const oldName = "driver-mobile-shell-v213";
    const oldCache = new MemoryCache(fetcher);
    stores.set(oldName, oldCache);
    await oldCache.put("/driver/", new FakeResponse(
        renderedDriverShell,
        {url: "https://driverform.ru/driver/", contentType: "text/html; charset=utf-8"}
    ));
    for (const dependency of exactDependencies) {
        if (dependency === missingDependency) continue;
        await oldCache.put(dependency, new FakeResponse("old:" + dependency, {
            url: "https://driverform.ru" + dependency,
        }));
    }

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
        Set,
    });

    return {
        listeners,
        stores,
        deleted,
        oldName,
        exactDependencies,
        setOffline(value) { offline = value; },
        setShellText(value) { shellText = value; },
    };
}

test("fresh authenticated install caches the exact rendered Driver dependency closure", async () => {
    const runtime = await createExpiredSessionRuntime({freshShell: true});
    let installPromise;
    runtime.listeners.get("install")({waitUntil(value) { installPromise = value; }});
    await installPromise;

    const current = runtime.stores.get("driver-mobile-shell-v315");
    assert.match((await current.match("/driver/")).body, /data-driver-shell/);
    for (const dependency of runtime.exactDependencies) {
        assert.equal((await current.match(dependency)).body, "new:" + dependency);
    }
    assert.deepEqual(runtime.deleted, []);

    let activatePromise;
    runtime.listeners.get("activate")({waitUntil(value) { activatePromise = value; }});
    await activatePromise;
    assert.deepEqual(runtime.deleted, [runtime.oldName]);
});

test("expired-session update migrates real exact Driver shell dependencies and serves each offline", async () => {
    const runtime = await createExpiredSessionRuntime();

    let installPromise;
    runtime.listeners.get("install")({waitUntil(value) { installPromise = value; }});
    await installPromise;

    assert.equal(runtime.stores.has(runtime.oldName), true);
    assert.deepEqual(runtime.deleted, []);
    const current = runtime.stores.get("driver-mobile-shell-v315");
    assert.match((await current.match("/driver/")).body, /old authenticated shell/);
    for (const dependency of runtime.exactDependencies) {
        assert.equal((await current.match(dependency)).body, "old:" + dependency);
    }

    let activatePromise;
    runtime.listeners.get("activate")({waitUntil(value) { activatePromise = value; }});
    await activatePromise;
    assert.deepEqual(runtime.deleted, [runtime.oldName]);
    assert.equal(runtime.stores.has(runtime.oldName), false);

    runtime.setOffline(true);
    for (const dependency of runtime.exactDependencies) {
        let responsePromise;
        runtime.listeners.get("fetch")({
            request: new FakeRequest(dependency),
            respondWith(value) { responsePromise = value; },
        });
        const response = await responsePromise;
        assert.equal(response.body, "old:" + dependency);
    }
});

test("missing exact shell dependency fails closure and preserves the previous cache", async () => {
    const missing = "/static/js/native-background-connection-v1.js?v=release-231";
    const runtime = await createExpiredSessionRuntime({missingDependency: missing});
    let installPromise;
    runtime.listeners.get("install")({waitUntil(value) { installPromise = value; }});

    await assert.rejects(installPromise, /Authenticated driver shell is unavailable/);
    assert.equal(runtime.stores.has(runtime.oldName), true);
    assert.deepEqual(runtime.deleted, []);

    let activatePromise;
    runtime.listeners.get("activate")({waitUntil(value) { activatePromise = value; }});
    await activatePromise;
    assert.equal(runtime.stores.has(runtime.oldName), true);
    assert.deepEqual(runtime.deleted, []);
});

test("confirmed logout clears authenticated Driver shells but keeps exact assets", async () => {
    const runtime = await createExpiredSessionRuntime({freshShell: true});
    let installPromise;
    runtime.listeners.get("install")({waitUntil(value) { installPromise = value; }});
    await installPromise;

    let messagePromise;
    let acknowledged = false;
    runtime.listeners.get("message")({
        data: {type: "CLEAR_AUTHENTICATED_SHELL"},
        ports: [{postMessage(payload) { acknowledged = Boolean(payload && payload.ok); }}],
        waitUntil(value) { messagePromise = value; },
    });
    await messagePromise;
    await Promise.resolve();

    const current = runtime.stores.get("driver-mobile-shell-v315");
    assert.equal(await current.match("/driver/"), undefined);
    assert.equal(await runtime.stores.get(runtime.oldName).match("/driver/"), undefined);
    assert.ok(await current.match(runtime.exactDependencies[0]));
    assert.equal(acknowledged, true);
});

/* Матрица без сети A2/B4 (30.09.2026): без сети после перезапуска экран берётся
   из кэша — со страницы последней загрузки (не свежее 15 мин), и подтверждённые
   с тех пор действия (отмена ковша, разгрузка) на ней не видны. Страница просит
   положить свежую копию: она ложится под /driver/, а устаревшие записи того же
   адреса с параметрами убираются, чтобы не перекрыть её при холодном старте. */
test("REFRESH_AUTHENTICATED_SHELL stores the current page so an offline cold start shows it", async () => {
    const runtime = await createExpiredSessionRuntime({freshShell: true});
    let installPromise;
    runtime.listeners.get("install")({waitUntil(value) { installPromise = value; }});
    await installPromise;
    const current = runtime.stores.get("driver-mobile-shell-v315");
    await current.put("/driver/?tab=work", (await current.match("/driver/")).clone());

    runtime.setShellText("bucket cancelled on the phone");
    let messagePromise;
    runtime.listeners.get("message")({
        data: {type: "REFRESH_AUTHENTICATED_SHELL"},
        waitUntil(value) { messagePromise = value; },
    });
    assert.equal(await messagePromise, true);

    runtime.setOffline(true);
    let responsePromise;
    runtime.listeners.get("fetch")({
        request: new FakeRequest("/driver/?tab=work", {mode: "navigate"}),
        respondWith(value) { responsePromise = value; },
        waitUntil() {},
    });
    assert.match((await responsePromise).body, /bucket cancelled on the phone/);
});

test("a shell refresh that gets the login page keeps the cached authenticated shell", async () => {
    const runtime = await createExpiredSessionRuntime();
    let installPromise;
    runtime.listeners.get("install")({waitUntil(value) { installPromise = value; }});
    await installPromise;
    let messagePromise;
    runtime.listeners.get("message")({
        data: {type: "REFRESH_AUTHENTICATED_SHELL"},
        waitUntil(value) { messagePromise = value; },
    });
    assert.equal(await messagePromise, false);
    const current = runtime.stores.get("driver-mobile-shell-v315");
    assert.match((await current.match("/driver/")).body, /old authenticated shell/);
});
