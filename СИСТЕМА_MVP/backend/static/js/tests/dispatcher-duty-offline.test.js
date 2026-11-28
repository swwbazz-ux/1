const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {webcrypto} = require("node:crypto");
const test = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../dispatcher-duty-offline.js"), "utf8");
const baseTime = Date.UTC(2026, 10, 26, 0);
function storage() {
    const store = new Map();
    return {get length() { return store.size; }, key: i => [...store.keys()][i],
        getItem: key => store.get(key) ?? null, setItem: (key, value) => store.set(key, value),
        removeItem: key => store.delete(key), keys: () => [...store.keys()]};
}

function recorder({store, network, owner = "1", ticket = "signed-grant", online = false}) {
    const timers = new Map(), listeners = new Map();
    let now = 0, next = 0;
    const on = (type, call) => listeners.set(type, call);
    const nav = {onLine: online, sendBeacon: () => true};
    const doc = {hidden: false, currentScript: {dataset: {owner, ticket, serverMs: String(baseTime), endpoint: "/observations/"}},
        querySelector: () => ({content: "local-csrf"}), addEventListener: on};
    function timer(fn, delay, repeat) {
        const id = ++next;
        timers.set(id, {fn, due: now + delay, repeat: repeat ? delay : 0});
        return id;
    }
    const context = {window: {addEventListener: on}, document: doc, navigator: nav, localStorage: store,
        crypto: webcrypto, Date: {now: () => baseTime + now}, performance: {now: () => now, getEntriesByType: () => []},
        AbortController, URLSearchParams, fetch: async (_url, {body}) => network(body),
        setTimeout: (fn, delay) => timer(fn, delay, false), setInterval: (fn, delay) => timer(fn, delay, true),
        clearInterval: id => timers.delete(id), clearTimeout: id => timers.delete(id)};
    vm.runInNewContext(source, context);
    return {
        emit: (kind, props = {}) => listeners.get(kind)(props),
        hide(value) { doc.hidden = value; listeners.get("visibilitychange")(); },
        connect() { nav.onLine = true; listeners.get("online")(); },
        async advance(ms) {
            const until = now + ms;
            for (;;) {
                const due = [...timers].filter(([, t]) => t.due <= until).sort((a, b) => a[1].due - b[1].due)[0];
                if (!due) break;
                const [id, task] = due;
                now = task.due;
                if (task.repeat) task.due += task.repeat;
                else timers.delete(id);
                task.fn();
                await new Promise(resolve => setImmediate(resolve));
            }
            now = until;
        }
    };
}

function ok(body) {
    const samples = JSON.parse(body.get("events"));
    return {ok: true, redirected: false, json: async () => ({window: body.get("window"), ack: samples.at(-1)[0], retired: false})};
}

test("real transport failure while the browser reports online retries the same stored times", async () => {
    const store = storage(), delivered = [];
    const page = recorder({store, online: true, network: body => {
        delivered.push(JSON.parse(body.get("events")));
        if (delivered.length === 1) throw Error("response lost");
        return ok(body);
    }});
    await page.advance(5);
    page.hide(true);
    await page.advance(1950);
    assert.equal(delivered.length, 1);
    assert.equal(store.keys().filter(k => k.includes("duty.v1")).length, 2);
    await page.advance(75);
    assert.deepEqual(delivered[1], [[1, baseTime, "visible"], [2, baseTime + 5, "hidden"]]);
    assert.equal(store.keys().filter(k => k.includes("duty.v1")).length, 0);
    page.emit("pagehide", {persisted: false}); // An accepted sendBeacon still needs a receipt.
    assert.equal(store.keys().filter(k => k.includes("duty.v1")).length, 1);
});

test("after browser restart only the original owner flushes all batches, including a closed window", async () => {
    const store = storage(), delivered = [];
    const network = body => { delivered.push(JSON.parse(body.get("events"))); return ok(body); };
    const page = recorder({store, network});
    page.hide(true);
    await page.advance(65 * 60000);
    page.hide(false);
    page.emit("pagehide", {persisted: false});
    assert.equal(store.keys().filter(k => k.includes("duty.v1")).length, 69);
    const other = recorder({store, network, owner: "2", ticket: "", online: true});
    await other.advance(100);
    assert.equal(delivered.length, 0);
    const original = recorder({store, network, ticket: "", online: true});
    await original.advance(100);
    assert.deepEqual(delivered.map(batch => batch.length), [64, 5]);
    assert.deepEqual(delivered.flat().map(sample => sample[0]), Array.from({length: 69}, (_, i) => i + 1));
    assert.equal(delivered.flat().at(-1)[2], "closed");
    assert.deepEqual(store.keys(), []);
});

test("back-forward cache records a hidden window and resumes the same sequence", async () => {
    const store = storage(), delivered = [];
    const page = recorder({store, network: body => { delivered.push(JSON.parse(body.get("events"))); return ok(body); }});
    await page.advance(7000);
    page.emit("pagehide", {persisted: true});
    await page.advance(600000);
    page.emit("pageshow", {persisted: true});
    page.connect();
    await page.advance(5);
    const events = delivered.flat();
    assert.deepEqual(events.slice(0, 3), [[1, baseTime, "visible"], [2, baseTime + 7000, "hidden"],
        [3, baseTime + 607000, "visible"]]);
    assert.ok(events.every(e => e[2] !== "closed"));
});
