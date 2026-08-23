"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const VIEWS_SOURCE = fs.readFileSync(
    path.resolve(__dirname, "..", "..", "..", "trips", "views.py"),
    "utf8"
);
const WORKER_MATCH = VIEWS_SOURCE.match(
    /DISPATCHER_SERVICE_WORKER_JS = r"""([\s\S]*?)"""/
);
assert.ok(WORKER_MATCH, "Dispatcher service worker source was not found");
const WORKER_SOURCE = WORKER_MATCH[1];
assert.match(
    WORKER_SOURCE,
    /\/static\/js\/realtime-client\.js\?v=__STATIC_ASSET_RELEASE__/,
    "Versioned realtime client must remain in the fail-closed Dispatcher core shell"
);
const FINAL_WORKER_SOURCE = WORKER_SOURCE.replace(
    /__STATIC_ASSET_RELEASE__/g,
    "ready-core-traffic-v7"
);
const REQUIRED_REALTIME_URL = "/static/js/realtime-client.js?v=ready-core-traffic-v7";

function createWorkerRuntime({failRequiredAsset = false} = {}) {
    const listeners = new Map();
    const deletedCaches = [];
    let skipWaitingCalls = 0;

    const context = {
        URL,
        Request: class RequestStub {
            constructor(url, options) {
                this.url = url;
                this.options = options;
            }
        },
        Response: class ResponseStub {},
        fetch() {
            throw new Error("fetch is outside this install test");
        },
        caches: {
            async open() {
                return {
                    async addAll(requests) {
                        const requestUrls = requests.map(request => request.url);
                        assert.ok(
                            requestUrls.includes(REQUIRED_REALTIME_URL),
                            "Final Dispatcher core shell must install the versioned realtime client"
                        );
                        if (failRequiredAsset && requestUrls.includes(REQUIRED_REALTIME_URL)) {
                            throw new Error("required asset unavailable");
                        }
                    },
                    async match() {
                        return null;
                    },
                    async put() {},
                };
            },
            async keys() {
                return ["dispatcher-desktop-shell-v41"];
            },
            async delete(key) {
                deletedCaches.push(key);
                return true;
            },
        },
        self: {
            addEventListener(type, listener) {
                listeners.set(type, listener);
            },
            async skipWaiting() {
                skipWaitingCalls += 1;
            },
            clients: {
                async claim() {},
            },
        },
    };
    vm.runInNewContext(FINAL_WORKER_SOURCE, context);

    return {
        deletedCaches,
        listeners,
        skipWaitingCalls() {
            return skipWaitingCalls;
        },
    };
}

function dispatchWaitUntil(listener) {
    let promise;
    listener({
        waitUntil(value) {
            promise = Promise.resolve(value);
        },
    });
    assert.ok(promise, "worker listener did not publish waitUntil promise");
    return promise;
}

test("Dispatcher worker fails closed when one required asset is unavailable", async () => {
    const runtime = createWorkerRuntime({failRequiredAsset: true});

    await assert.rejects(
        dispatchWaitUntil(runtime.listeners.get("install")),
        /required asset unavailable/
    );

    assert.equal(runtime.skipWaitingCalls(), 0);
    assert.deepEqual(runtime.deletedCaches, []);
});

test("Dispatcher worker activates only after the complete shell is cached", async () => {
    const runtime = createWorkerRuntime();

    await dispatchWaitUntil(runtime.listeners.get("install"));

    assert.equal(runtime.skipWaitingCalls(), 1);
    assert.deepEqual(runtime.deletedCaches, []);
});
