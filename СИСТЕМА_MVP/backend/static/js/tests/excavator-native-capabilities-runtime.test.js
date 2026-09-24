"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const PUSH_SOURCE = fs.readFileSync(path.resolve(__dirname, "../excavator-native-push-v1.js"), "utf8");
const HAPTICS_SOURCE = fs.readFileSync(path.resolve(__dirname, "../excavator-haptics-v1.js"), "utf8");
const TEMPLATE = fs.readFileSync(path.resolve(__dirname, "../../../templates/trips/excavator_work.html"), "utf8");

function bootPush(options = {}) {
    const events = new Map();
    const nativeEvents = new Map();
    const requests = [];
    const shell = {dataset: {eoAccessId: "77", eoEmployeeId: "88"}};
    const token = {provider: "fcm", token: "excavator-token", platform: "android", appId: "ru.copperresources.excavator"};
    const nativePush = options.unsupported ? null : {
        getToken: () => Promise.resolve(token),
        addListener: (name, callback) => { nativeEvents.set(name, callback); return Promise.resolve({remove() {}}); },
    };
    const document = {
        cookie: "csrftoken=csrf-value",
        querySelector(selector) {
            if (selector === "[data-eo-shell]") return shell;
            return null;
        },
    };
    const window = {
        Capacitor: nativePush ? {Plugins: {NativePush: nativePush}} : undefined,
        addEventListener: (name, callback) => events.set(name, callback),
        fetch: (url, request) => {
            requests.push({url, request});
            return Promise.resolve({ok: true});
        },
    };
    vm.runInNewContext(PUSH_SOURCE, {window, document, Promise, JSON, String, decodeURIComponent});
    return {window, shell, token, events, nativeEvents, requests};
}

async function settle() {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
    await new Promise((resolve) => setImmediate(resolve));
}

test("Excavator FCM token is registered once for the authenticated shell", async () => {
    const runtime = bootPush();
    await settle();
    assert.equal(runtime.requests.length, 1);
    assert.equal(runtime.requests[0].url, "/excavator/push/native/");
    assert.equal(runtime.requests[0].request.headers["X-CSRFToken"], "csrf-value");
    assert.deepEqual(JSON.parse(runtime.requests[0].request.body), {
        provider: "fcm",
        token: "excavator-token",
        platform: "android",
        app_id: "ru.copperresources.excavator",
    });
    await runtime.window.__excavatorNativePushBinding.refresh();
    assert.equal(runtime.requests.length, 1);
});

test("Excavator token rotation is registered without duplicate listeners", async () => {
    const runtime = bootPush();
    await settle();
    await runtime.nativeEvents.get("pushToken")({...runtime.token, token: "rotated-token"});
    await settle();
    assert.equal(runtime.requests.length, 2);
    assert.equal(JSON.parse(runtime.requests[1].request.body).token, "rotated-token");
    assert.equal(runtime.nativeEvents.size, 1);
});

test("ordinary browser skips native registration and no token is persisted or logged", async () => {
    const runtime = bootPush({unsupported: true});
    await settle();
    assert.equal(runtime.requests.length, 0);
    assert.equal(await runtime.window.__excavatorNativePushBinding.refresh(), false);
    assert.doesNotMatch(PUSH_SOURCE, /console\.|localStorage|sessionStorage|indexedDB/i);
});

test("Excavator haptics prefer the native media bridge and preserve gesture timing", () => {
    const nativeCalls = [];
    const webCalls = [];
    const window = {
        navigator: {vibrate(pattern) { webCalls.push(pattern); return true; }},
        Capacitor: {Plugins: {NativeHaptics: {vibrate(payload) { nativeCalls.push(payload); return Promise.resolve({performed: true}); }}}},
    };
    vm.runInNewContext(HAPTICS_SOURCE, {window, Array, Number, Math, Promise});
    assert.equal(window.excavatorHaptic([180, 90, 180], 255), true);
    assert.deepEqual([...nativeCalls[0].pattern], [180, 90, 180]);
    assert.equal(nativeCalls[0].amplitude, 255);
    assert.deepEqual(webCalls, []);
});

test("Excavator shell wires native capabilities and routes existing feedback through them", () => {
    assert.match(TEMPLATE, /excavator-haptics-v1\.js[^\n]+excavator-mobile-shell-v261/);
    assert.match(TEMPLATE, /excavator-native-push-v1\.js[^\n]+excavator-mobile-shell-v261/);
    assert.match(TEMPLATE, /window\.excavatorHaptic\(\[180, 90, 180\], 255\)/);
    assert.match(TEMPLATE, /window\.excavatorHaptic\(70, 220\)/);
    assert.doesNotMatch(TEMPLATE, /navigator\.vibrate/);
});
