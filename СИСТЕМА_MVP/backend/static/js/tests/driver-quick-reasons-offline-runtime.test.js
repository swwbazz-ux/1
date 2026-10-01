"use strict";
/* Матрица без сети C2 (30.09.2026): быстрый набор причин («звёздочки»)
   меняется на телефоне сразу, но без сети досылался на сервер только со
   следующей загрузкой страницы. Теперь набор помечен неотправленным и уходит
   при возврате сети (событие online), а принятый сервером — пометку снимает. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const DRUM = fs.readFileSync(path.resolve(__dirname, "../driver-downtime-drum-v1.js"), "utf8");

function block(source, signature) {
    const start = source.indexOf(signature);
    assert.notEqual(start, -1, signature + " not found");
    let depth = 0;
    for (let index = source.indexOf("{", start); index < source.length; index += 1) {
        if (source[index] === "{") depth += 1;
        if (source[index] === "}") { depth -= 1; if (depth === 0) return source.slice(start, index + 1); }
    }
    throw new Error("unterminated " + signature);
}

function setup() {
    const values = new Map();
    const listeners = {};
    const posted = [];
    let respond = () => Promise.reject(new Error("offline"));
    const root = {
        localStorage: {
            getItem: (key) => (values.has(key) ? values.get(key) : null),
            setItem: (key, value) => values.set(key, String(value)),
        },
        addEventListener(type, callback) { listeners[type] = callback; },
        fetch(url, options) { posted.push(JSON.parse(options.body)); return respond(); },
    };
    const drum = {dataset: {driverQuickUrl: "/driver/quick-reasons/"}};
    const shell = {dataset: {driverAccessId: "7"}};
    const context = {
        root,
        JSON, Number, String, Array, Promise,
        q(selector) {
            if (selector === "[data-driver-shell]") return shell;
            if (selector === 'meta[name="csrf-token"]') return {content: "t"};
            return null;
        },
        drum: () => drum,
        toast() {},
    };
    const source = [
        block(DRUM, "function quickStorageKey()"),
        block(DRUM, "function readLocalQuick()"),
        block(DRUM, "function writeLocalQuick("),
        block(DRUM, "function saveQuick("),
        block(DRUM, "function resendUnsentQuick()"),
        'root.addEventListener("online", resendUnsentQuick);',
    ].join("\n");
    vm.runInNewContext(source, context);
    return {
        context, listeners, posted, values,
        answer(fn) { respond = fn; },
    };
}

test("a quick set changed offline is resent as soon as the network is back, and the mark clears on acceptance", async () => {
    const run = setup();
    run.context.writeLocalQuick(["3", "5", "8"], "2026-09-30T08:00:00.000Z", true);
    run.context.saveQuick(["3", "5", "8"], "2026-09-30T08:00:00.000Z");
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(run.posted.length, 1);
    assert.equal(run.context.readLocalQuick().unsent, true);

    run.answer(() => Promise.resolve({json: () => ({ok: true, reason_ids: [3, 5, 8], updated_at: "2026-09-30T08:00:00.000Z"})}));
    assert.equal(typeof run.listeners.online, "function");
    run.listeners.online();
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(run.posted.length, 2);
    assert.deepEqual(run.posted[1].reason_ids, [3, 5, 8]);
    assert.equal(run.context.readLocalQuick().unsent, false);

    run.listeners.online();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(run.posted.length, 2, "an accepted set is not sent again");
});
