#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(
    path.resolve(__dirname, "../clerk-workplace-pwa.js"),
    "utf8"
);

async function flushPromises() {
    await Promise.resolve();
    await Promise.resolve();
    await new Promise((resolve) => setImmediate(resolve));
}

test("clerk shell retires only the exact legacy settlement worker and caches", async () => {
    let loadListener = null;
    const unregisterCalls = [];
    const deletedCaches = [];
    const registration = (scope, scriptURL, label) => ({
        scope,
        active: {scriptURL},
        waiting: null,
        installing: null,
        unregister() {
            unregisterCalls.push(label);
            return Promise.resolve(true);
        },
    });
    const registrations = [
        registration(
            "https://driverform.ru/settlement/",
            "https://driverform.ru/settlement/sw.js",
            "legacy-narrow"
        ),
        registration(
            "https://settlement.driverform.ru/",
            "https://settlement.driverform.ru/settlement/sw.js",
            "legacy-root"
        ),
        registration(
            "https://driverform.ru/clerk/",
            "https://driverform.ru/clerk/sw.js",
            "clerk"
        ),
        registration(
            "https://driverform.ru/",
            "https://driverform.ru/dispatcher-sw.js",
            "dispatcher"
        ),
    ];
    const window = {
        location: {origin: "https://driverform.ru"},
        addEventListener(type, listener) {
            if (type === "load") loadListener = listener;
        },
        caches: {
            keys() {
                return Promise.resolve([
                    "settlement-clerk-shell-v1",
                    "clerk-workplace-shell-v1",
                    "dispatcher-shell-v1",
                ]);
            },
            delete(key) {
                deletedCaches.push(key);
                return Promise.resolve(true);
            },
        },
    };
    const context = {
        URL,
        Promise,
        window,
        caches: window.caches,
        navigator: {
            serviceWorker: {
                getRegistrations() {
                    return Promise.resolve(registrations);
                },
            },
        },
    };
    context.globalThis = context;

    vm.runInNewContext(source, context, {filename: "clerk-workplace-pwa.js"});
    assert.equal(typeof loadListener, "function");

    loadListener();
    await flushPromises();

    assert.deepEqual(unregisterCalls, ["legacy-narrow", "legacy-root"]);
    assert.deepEqual(deletedCaches, ["settlement-clerk-shell-v1"]);
});
