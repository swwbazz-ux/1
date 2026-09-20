"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SOURCE = fs.readFileSync(path.resolve(__dirname, "../driver-native-push-v1.js"), "utf8");

function boot(options = {}) {
    const events = new Map();
    const nativeEvents = new Map();
    const requests = [];
    const shell = {dataset: {
        driverAccessId: "77",
        driverActorId: "88",
        driverAuthGeneration: "generation-1",
    }};
    const token = {
        provider: "fcm",
        token: "device-token",
        platform: "android",
        appId: "ru.copperresources.driver",
    };
    const nativePush = options.unsupported ? null : {
        getToken: () => Promise.resolve(token),
        addListener: (name, callback) => { nativeEvents.set(name, callback); return Promise.resolve({remove() {}}); },
    };
    const document = {
        cookie: "csrftoken=csrf-value",
        querySelector(selector) {
            if (selector === "[data-driver-shell]") return shell;
            if (selector === 'meta[name="csrf-token"]') return null;
            return null;
        },
    };
    const window = {
        Capacitor: nativePush ? {Plugins: {NativePush: nativePush}} : undefined,
        addEventListener: (name, callback) => events.set(name, callback),
        fetch: (url, request) => {
            requests.push({url, request});
            return Promise.resolve({ok: options.responseOk !== false});
        },
    };
    vm.runInNewContext(SOURCE, {
        window,
        document,
        Promise,
        JSON,
        String,
        decodeURIComponent,
    });
    return {window, shell, token, events, nativeEvents, requests};
}

async function settle() {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
    await new Promise((resolve) => setImmediate(resolve));
}

test("native token is registered with the authenticated Driver session", async () => {
    const runtime = boot();
    await settle();
    assert.equal(runtime.requests.length, 1);
    assert.equal(runtime.requests[0].url, "/driver/push/native/");
    assert.equal(runtime.requests[0].request.credentials, "same-origin");
    assert.equal(runtime.requests[0].request.headers["X-CSRFToken"], "csrf-value");
    assert.deepEqual(JSON.parse(runtime.requests[0].request.body), {
        provider: "fcm",
        token: "device-token",
        platform: "android",
        app_id: "ru.copperresources.driver",
    });
});

test("same token is idempotent but a new auth generation is re-registered", async () => {
    const runtime = boot();
    await settle();
    await runtime.window.__driverNativePushBinding.refresh();
    assert.equal(runtime.requests.length, 1);
    runtime.shell.dataset.driverAuthGeneration = "generation-2";
    await runtime.events.get("operational-state-refresh-applied")();
    assert.equal(runtime.requests.length, 2);
});

test("FCM token rotation is registered through one native listener", async () => {
    const runtime = boot();
    await settle();
    await runtime.nativeEvents.get("pushToken")({...runtime.token, token: "rotated-token"});
    await settle();
    assert.equal(runtime.requests.length, 2);
    assert.equal(JSON.parse(runtime.requests[1].request.body).token, "rotated-token");
    assert.equal(runtime.nativeEvents.size, 1);
});

test("ordinary browser does not call the endpoint", async () => {
    const runtime = boot({unsupported: true});
    await settle();
    assert.equal(runtime.requests.length, 0);
    assert.equal(await runtime.window.__driverNativePushBinding.refresh(), false);
});

test("source never logs or persists the token", () => {
    assert.doesNotMatch(SOURCE, /console\.|localStorage|sessionStorage|indexedDB/i);
});
