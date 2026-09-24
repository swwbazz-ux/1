"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const BACKEND = path.resolve(__dirname, "..", "..", "..");
const SOURCE = fs.readFileSync(
    path.join(BACKEND, "static", "js", "dispatcher-transport-v1.js"),
    "utf8"
);
const QUEUE_KEY = "mining-master-mobile-sync-queue-v3";

function createRuntime(options = {}) {
    const storage = new Map(Object.entries(options.storage || {}));
    const timers = new Map();
    const fetchCalls = [];
    let nextTimerId = 1;
    const context = {
        console,
        Date,
        Error,
        JSON,
        Math,
        Object,
        Promise,
        FormData: class FormDataStub {
            constructor() { this.values = []; }
            append(key, value) { this.values.push([key, value]); }
        },
        localStorage: {
            getItem(key) { return storage.has(key) ? storage.get(key) : null; },
            setItem(key, value) { storage.set(key, String(value)); },
            removeItem(key) { storage.delete(key); },
        },
        setTimeout(callback, delay) {
            const id = nextTimerId++;
            timers.set(id, {callback, delay});
            return id;
        },
        clearTimeout(id) { timers.delete(id); },
        fetch(url, fetchOptions) {
            fetchCalls.push({url, options: fetchOptions});
            if (typeof options.fetch === "function") {
                return options.fetch(url, fetchOptions);
            }
            return Promise.resolve({
                ok: true,
                json: () => Promise.resolve({ok: true}),
            });
        },
        isAppRoleReadonly: () => Boolean(options.readonly),
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(SOURCE, context, {filename: "dispatcher-transport-v1.js"});
    return {context, storage, timers, fetchCalls};
}

test("сохраняет production-ключ очереди и удаляет только устаревшие v1/v2", () => {
    const queued = [{id: "existing", createdAt: 10, attempts: 0}];
    const runtime = createRuntime({
        storage: {
            "mining-master-mobile-sync-queue-v1": "[]",
            "mining-master-mobile-sync-queue-v2": "[]",
            [QUEUE_KEY]: JSON.stringify(queued),
        },
    });
    const transport = runtime.context.createDispatcherTransport({});

    assert.equal(transport.queueKey, QUEUE_KEY);
    assert.equal(runtime.storage.has("mining-master-mobile-sync-queue-v1"), false);
    assert.equal(runtime.storage.has("mining-master-mobile-sync-queue-v2"), false);
    assert.deepEqual(JSON.parse(runtime.storage.get(QUEUE_KEY)), queued);
    assert.equal(transport.getQueueState().length, 1);
});

test("сетевой сбой ставит JSON-команду в совместимую очередь", async () => {
    const runtime = createRuntime({
        fetch: () => Promise.reject(new Error("offline")),
    });
    const transport = runtime.context.createDispatcherTransport({
        getCsrfToken: () => "csrf-token",
    });

    const result = await transport.post("/dispatcher/assign/", {action: "assign"});
    const queue = JSON.parse(runtime.storage.get(QUEUE_KEY));

    assert.deepEqual(JSON.parse(JSON.stringify(result)), {queued: true});
    assert.equal(queue.length, 1);
    assert.equal(queue[0].kind, "json");
    assert.equal(queue[0].url, "/dispatcher/assign/");
    assert.match(queue[0].data.client_action_id, /^mm-/);
    assert.equal(queue[0].attempts, 0);
    assert.equal(runtime.timers.size > 0, true);
});

test("запрещённая offline-очередь возвращает сетевую ошибку без записи", async () => {
    const runtime = createRuntime({
        fetch: () => Promise.reject(new Error("offline")),
    });
    const transport = runtime.context.createDispatcherTransport({});

    await assert.rejects(
        transport.post("/dispatcher/move/", {}, {queueOnNetworkFailure: false}),
        /offline/
    );
    assert.equal(runtime.storage.has(QUEUE_KEY), false);
});

test("серверная ошибка не маскируется offline-очередью", async () => {
    const runtime = createRuntime({
        fetch: () => Promise.resolve({
            ok: false,
            status: 409,
            json: () => Promise.resolve({error: "Конфликт", code: "state_conflict", conflict: true}),
        }),
    });
    const transport = runtime.context.createDispatcherTransport({});

    await assert.rejects(
        transport.post("/dispatcher/assign/", {}),
        (error) => error.code === "state_conflict" && error.status === 409 && error.conflict === true
    );
    assert.equal(runtime.storage.has(QUEUE_KEY), false);
});

test("flush отправляет старую запись с CSRF и удаляет её только после успеха", async () => {
    const queued = [{
        id: "queued-1",
        createdAt: 10,
        attempts: 0,
        kind: "json",
        url: "/dispatcher/assign/",
        data: {action: "release", client_action_id: "existing-action"},
    }];
    const runtime = createRuntime({storage: {[QUEUE_KEY]: JSON.stringify(queued)}});
    const transport = runtime.context.createDispatcherTransport({
        getCsrfToken: () => "csrf-token",
    });

    transport.flush();
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(runtime.fetchCalls.length, 1);
    assert.equal(runtime.fetchCalls[0].options.headers["X-CSRFToken"], "csrf-token");
    assert.equal(runtime.fetchCalls[0].options.credentials, "same-origin");
    assert.deepEqual(JSON.parse(runtime.storage.get(QUEUE_KEY)), []);
    assert.equal(transport.getQueueState().isFlushing, false);
});

test("неактивная роль не отправляет и не изменяет сохранённую очередь", async () => {
    const queued = [{id: "keep", createdAt: 10, attempts: 0}];
    const runtime = createRuntime({
        readonly: true,
        storage: {[QUEUE_KEY]: JSON.stringify(queued)},
    });
    const transport = runtime.context.createDispatcherTransport({});

    await assert.rejects(
        transport.post("/dispatcher/assign/", {}),
        (error) => error.code === "inactive_role" && error.isServerResponse === true
    );
    transport.flush();

    assert.equal(runtime.fetchCalls.length, 0);
    assert.deepEqual(JSON.parse(runtime.storage.get(QUEUE_KEY)), queued);
});

test("диагностика связи остаётся частью transport API", () => {
    const runtime = createRuntime();
    const transport = runtime.context.createDispatcherTransport({});

    transport.updateRealtimeConnection({connected: false, reason: "timeout"});
    const disconnected = transport.getDebugState();
    assert.equal(disconnected.realtimeConnected, false);
    assert.equal(disconnected.realtimeLastReason, "timeout");

    transport.updateRealtimeConnection({connected: true, lastSuccessAt: 12345});
    const connected = transport.getDebugState();
    assert.equal(connected.realtimeConnected, true);
    assert.equal(connected.realtimeLastSuccessAt, 12345);
});
