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
            get length() { return storage.size; },
            key(index) { return Array.from(storage.keys())[index] || null; },
            getItem(key) { return storage.has(key) ? storage.get(key) : null; },
            setItem(key, value) {
                if (options.storageFails && options.storageFails(key, value)) throw new Error("QuotaExceededError");
                storage.set(key, String(value));
            },
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

test("сохраняет production-ключ и исходные legacy v1/v2 без опасного автоповтора", () => {
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
    assert.equal(runtime.storage.has("mining-master-mobile-sync-queue-v1"), true);
    assert.equal(runtime.storage.has("mining-master-mobile-sync-queue-v2"), true);
    assert.deepEqual(JSON.parse(runtime.storage.get(QUEUE_KEY)), queued);
    assert.equal(transport.getQueueState().length, 1);
});

test("сетевой сбой сохраняет JSON-команду в журнале, не выдавая её старому отправителю", async () => {
    const runtime = createRuntime({
        fetch: () => Promise.reject(new Error("offline")),
    });
    const transport = runtime.context.createDispatcherTransport({
        getCsrfToken: () => "csrf-token",
    });

    const result = await transport.post("/dispatcher/assign/", {action: "assign"});
    const queue = transport.readQueue();
    assert.equal(runtime.storage.has(QUEUE_KEY), false);

    assert.deepEqual(JSON.parse(JSON.stringify(result)), {queued: true});
    assert.equal(queue.length, 1);
    assert.equal(queue[0].kind, "json");
    assert.equal(queue[0].url, "/dispatcher/assign/");
    assert.match(queue[0].data.client_action_id, /^mm-/);
    assert.equal(queue[0].attempts, 1);
    assert.equal(runtime.timers.size > 0, true);
});

test("legacy-запрет автоповтора хранит исходник, возвращает ошибку и не исполняет откатившийся UI позже", async () => {
    const runtime = createRuntime({
        fetch: () => Promise.reject(new Error("offline")),
    });
    const transport = runtime.context.createDispatcherTransport({});

    await assert.rejects(
        transport.post("/dispatcher/move/", {}, {queueOnNetworkFailure: false}),
        /offline/
    );
    assert.equal(transport.readQueue().length, 0);
    const records = Array.from(runtime.storage.entries()).filter(([key]) => key.startsWith(transport.journalPrefix));
    assert.equal(records.length, 1);
    assert.equal(JSON.parse(records[0][1]).delivery.state, "held");
    await transport.flush();
    assert.equal(runtime.fetchCalls.length, 1);
});

test("серверная ошибка не маскируется успехом и сохраняет команду для восстановления", async () => {
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
    assert.equal(transport.readQueue().length, 1);
});

test("flush сохраняет исходник старой записи после успеха и исключает его из рабочей очереди", async () => {
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
    assert.deepEqual(JSON.parse(runtime.storage.get(QUEUE_KEY)), queued);
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

const drain = () => new Promise((resolve) => setImmediate(resolve));
const command = (id, extra = {}) => Object.assign({
    id, kind: "json", url: "/dispatcher/assign/", coalesceKey: "truck-7",
    data: {client_action_id: id, truck_id: 7, excavator_id: 9, expected_assignment_state_id: 11},
}, extra);

test("первая отправка происходит после надёжной записи; ACK оставляет исходную команду в журнале", async () => {
    let transport;
    const r = createRuntime({fetch: async () => {
        assert.equal(transport.readQueue().length, 1);
        return {ok: true, json: async () => ({ok: true, assignment_state_id: 12})};
    }});
    transport = r.context.createDispatcherTransport({});
    await transport.post("/dispatcher/assign/", {client_action_id: "durable-first", truck_id: 7});
    assert.equal(transport.readQueue().length, 0);
    const records = Array.from(r.storage.entries()).filter(([key]) => key.startsWith(transport.journalPrefix));
    assert.equal(records.length, 1);
    const record = JSON.parse(records[0][1]);
    assert.equal(record.request.data.client_action_id, "durable-first");
    assert.equal(record.delivery.state, "acknowledged");
    assert.equal(record.delivery.receipt.assignment_state_id, 12);
});

test("quota не подтверждает несохранённую команду и не отправляет её в сеть", async () => {
    const r = createRuntime({storageFails: () => true});
    const transport = r.context.createDispatcherTransport({});
    assert.equal(transport.enqueue(command("quota")), false);
    await assert.rejects(transport.post("/dispatcher/assign/", {}), (error) => error.code === "storage_unavailable");
    assert.equal(r.fetchCalls.length, 0);
    assert.equal(transport.readQueue().length, 0);
});

for (const status of [409, 503]) {
    test("HTTP " + status + " сохраняет исходник и освобождает отправителя", async () => {
        const r = createRuntime({fetch: async () => ({ok: false, status, json: async () => ({error: "fixture"})})});
        const transport = r.context.createDispatcherTransport({});
        transport.enqueue(command("keep-" + status));
        await transport.flush();
        await drain();
        assert.equal(transport.readQueue().length, 1);
        assert.equal(transport.readQueue()[0].data.client_action_id, "keep-" + status);
        assert.equal(transport.getQueueState().isFlushing, false);
    });
}

test("разные команды одного самосвала не объединяются, payload изолирован от дальнейших UI-правок", () => {
    const r = createRuntime();
    const transport = r.context.createDispatcherTransport({});
    const first = command("first");
    transport.enqueue(first);
    first.data.excavator_id = 666;
    transport.enqueue(command("second"));
    const queue = transport.readQueue();
    assert.equal(queue.length, 2);
    assert.deepEqual(Array.from(queue, (item) => item.data.client_action_id), ["first", "second"]);
    assert.equal(queue[0].data.excavator_id, 9);
});

test("повтор с тем же ID и другим содержимым не переписывает исходник", () => {
    const r = createRuntime();
    const transport = r.context.createDispatcherTransport({});
    assert.equal(transport.enqueue(command("same")), true);
    assert.equal(transport.enqueue(command("same", {data: {client_action_id: "same", truck_id: 8}})), false);
    assert.equal(transport.readQueue()[0].data.truck_id, 7);
});

test("сбой mirror очереди не теряет уже записанный журнал при перезапуске", () => {
    const r = createRuntime({storageFails: (key) => key === QUEUE_KEY});
    const transport = r.context.createDispatcherTransport({});
    assert.equal(transport.enqueue(command("journal-only")), true);
    const restarted = r.context.createDispatcherTransport({});
    assert.equal(restarted.readQueue().length, 1);
    assert.equal(restarted.readQueue()[0].data.client_action_id, "journal-only");
});

test("неверный JSON ответа 2xx не удаляет команду", async () => {
    const r = createRuntime({fetch: async () => ({ok: true, json: async () => { throw new Error("login HTML"); }})});
    const transport = r.context.createDispatcherTransport({});
    transport.enqueue(command("invalid-ack"));
    await transport.flush();
    await drain();
    assert.equal(transport.readQueue().length, 1);
});

test("deadline тела без AbortController освобождает очередь; поздний ACK не завершает другую попытку", async () => {
    let finishBody;
    const r = createRuntime({fetch: async () => ({ok: true, json: () => new Promise((resolve) => { finishBody = resolve; })})});
    const transport = r.context.createDispatcherTransport({});
    transport.enqueue(command("hung-body"));
    const flush = transport.flush();
    await drain();
    const deadline = Array.from(r.timers.values()).find((timer) => timer.delay === 12000);
    assert.ok(deadline, "deadline survives headers even without AbortController");
    deadline.callback();
    await flush;
    await drain();
    assert.equal(transport.getQueueState().isFlushing, false);
    assert.equal(transport.readQueue().length, 1);
    finishBody({ok: true});
    await drain();
    assert.equal(transport.readQueue().length, 1);
});

test("команда сохраняет автора; новый пользователь и legacy без автора не присваивают её", async () => {
    let access = "A";
    const r = createRuntime();
    const transport = r.context.createDispatcherTransport({getCommandContext: () => ({access_id: access, role: "dispatcher", shift_id: "10"})});
    transport.enqueue(command("author-A"));
    access = "B";
    transport.enqueue(command("author-B"));
    await transport.flush();
    await drain();
    assert.equal(r.fetchCalls.length, 1);
    assert.equal(JSON.parse(r.fetchCalls[0].options.body).client_action_id, "author-B");
    assert.equal(transport.readQueue()[0].author.access_id, "A");
    assert.equal(transport.readOwnQueue().length, 0, "чужая очередь не удерживает обновление экрана сменщика");
    const old = createRuntime({storage: {[QUEUE_KEY]: JSON.stringify([command("unknown-author")])}});
    const oldTransport = old.context.createDispatcherTransport({getCommandContext: () => ({access_id: "B"})});
    await oldTransport.flush();
    assert.equal(old.fetchCalls.length, 0);
    assert.equal(oldTransport.readQueue().length, 1);
});

test("409 первой команды не удерживает независимую вторую команду в готовой очереди", async () => {
    const r = createRuntime({fetch: async (url, init) => {
        const id = JSON.parse(init.body).client_action_id;
        return id === "conflict" ? {ok: false, status: 409, json: async () => ({code: "conflict"})}
            : {ok: true, status: 200, json: async () => ({ok: true})};
    }});
    const transport = r.context.createDispatcherTransport({});
    transport.enqueue(command("conflict"));
    transport.enqueue(command("independent", {data: {client_action_id: "independent", truck_id: 8}}));
    await transport.flush();
    await transport.flush();
    assert.equal(r.fetchCalls.length, 2);
    assert.equal(transport.readQueue().length, 1);
    assert.equal(transport.readQueue()[0].data.client_action_id, "conflict");
});

test("перезапуск после потери ответа сохраняет ID, время и автора при повторном POST", async () => {
    let offline = true;
    const r = createRuntime({fetch: async () => {
        if (offline) throw new Error("response lost");
        return {ok: true, status: 200, json: async () => ({ok: true, deduplicated: true})};
    }});
    const transport = r.context.createDispatcherTransport({});
    const payload = {client_action_id: "lost-response", truck_id: 7};
    await transport.post("/assign/", payload);
    const before = JSON.parse(JSON.stringify(transport.readQueue()[0]));
    offline = false;
    const restarted = r.context.createDispatcherTransport({});
    await restarted.post("/assign/", payload);
    const record = JSON.parse(r.storage.get(restarted.journalPrefix + encodeURIComponent(before.id)));
    assert.equal(record.request.occurredAt, before.occurredAt);
    assert.equal(record.request.createdAt, before.createdAt);
    assert.equal(record.request.data.client_action_id, "lost-response");
    assert.equal(restarted.readQueue().length, 0);
});

test("quota записи ACK оставляет сохранённый исходник на повтор", async () => {
    const r = createRuntime({storageFails: (key, value) => key.includes(":command:") && JSON.parse(value).delivery.state === "acknowledged"});
    const transport = r.context.createDispatcherTransport({});
    transport.enqueue(command("ack-quota"));
    await transport.flush();
    assert.equal(transport.readQueue().length, 1);
    assert.equal(transport.readQueue()[0].data.client_action_id, "ack-quota");
});

test("повреждённая старая очередь не перезаписывается пустым массивом", async () => {
    const r = createRuntime({storage: {[QUEUE_KEY]: "{broken-original"}});
    const transport = r.context.createDispatcherTransport({});
    assert.equal(transport.enqueue(command("new")), false);
    await assert.rejects(transport.post("/assign/", {}), (error) => error.code === "storage_unavailable");
    assert.equal(r.storage.get(QUEUE_KEY), "{broken-original");
    assert.equal(r.fetchCalls.length, 0);
});

test("refresh deadline действует после заголовков до окончания тела", async () => {
    const r = createRuntime({fetch: async () => ({ok: true, text: () => new Promise(() => {})})});
    const transport = r.context.createDispatcherTransport({});
    const refreshed = transport.fetchWithTimeout("/board/", {}, 12000);
    await drain();
    const deadline = Array.from(r.timers.values()).find((timer) => timer.delay === 12000);
    assert.ok(deadline);
    deadline.callback();
    await assert.rejects(refreshed, (error) => error.code === "request_timeout");
});

test("доказанный invalid 400 сохраняется как отказ без бесконечного автоповтора", async () => {
    const r = createRuntime({fetch: async () => ({ok: false, status: 400,
        json: async () => ({ok: false, error: "Некорректное действие с самосвалом."})})});
    const transport = r.context.createDispatcherTransport({});
    transport.enqueue(command("invalid-command"));
    await transport.flush();
    await transport.flush();
    assert.equal(r.fetchCalls.length, 1);
    assert.equal(transport.readQueue().length, 0);
    const record = JSON.parse(r.storage.get(transport.journalPrefix + "invalid-command"));
    assert.equal(record.request.data.client_action_id, "invalid-command");
    assert.equal(record.delivery.state, "rejected");
    assert.equal(record.delivery.receipt.ok, false);
});

test("quota после сетевого сбоя не снимает запрет автоповтора structural-команды при restart", async () => {
    const r = createRuntime({
        fetch: async () => { throw new Error("offline"); },
        storageFails: (key, value) => key.includes(":command:") && JSON.parse(value).delivery.state === "held",
    });
    const transport = r.context.createDispatcherTransport({});
    await assert.rejects(transport.post("/move/", {client_action_id: "manual-only"}, {queueOnNetworkFailure: false}), /offline/);
    const record = JSON.parse(r.storage.get(transport.journalPrefix + "sync-manual-only"));
    assert.equal(record.request.autoRetry, false);
    assert.equal(record.delivery.state, "pending", "fixture reproduces failed held persistence");
    const restarted = r.context.createDispatcherTransport({});
    await restarted.flush();
    assert.equal(r.fetchCalls.length, 1);
    assert.equal(restarted.readQueue().length, 0);
});

test("quota служебного счётчика не отменяет уже записанное намерение", async () => {
    const r = createRuntime({storageFails: (key, value) => key.includes(":command:") && JSON.parse(value).delivery.attempts > 0});
    const transport = r.context.createDispatcherTransport({});
    const result = await transport.post("/assign/", {client_action_id: "metadata-quota"});
    assert.equal(result.queued, true, "ACK also cannot be persisted: original waits for confirmation");
    assert.equal(r.fetchCalls.length, 1);
    assert.equal(transport.readQueue().length, 1);
    assert.equal(transport.readQueue()[0].data.client_action_id, "metadata-quota");
});

test("HTTP carries the saved author, shift and time even when the cookie changes after the UI check", async () => {
    let accepted = false;
    let shown = {actor_id: "12", access_id: "7", role: "dispatcher", shift_id: "10"};
    const r = createRuntime({fetch: async () => ({ok: accepted, status: accepted ? 200 : 409,
        json: async () => accepted ? {ok: true} : {ok: false, code: "command_author_mismatch", error: "Другой сотрудник"}})});
    const transport = r.context.createDispatcherTransport({getCommandContext: () => shown});
    const payload = {client_action_id: "original-author", truck_id: 7};
    await assert.rejects(transport.post("/dispatcher/control/truck/assign/", payload),
        error => error.code === "command_author_mismatch");
    const source = transport.readQueue()[0];
    const firstHeader = r.fetchCalls[0].options.headers["X-Command-Context"];
    const context = JSON.parse(firstHeader);
    assert.deepEqual(context, {version: 1, id: source.id, author: shown, occurred_at: source.occurredAt});
    assert.deepEqual(JSON.parse(r.fetchCalls[0].options.body), payload);
    shown = {...shown, shift_id: "20"};
    accepted = true;
    await transport.send(source);
    assert.equal(r.fetchCalls[1].options.headers["X-Command-Context"], firstHeader);
    const record = JSON.parse(r.storage.get(transport.journalPrefix + encodeURIComponent(source.id)));
    assert.deepEqual(record.request.author, context.author);
    assert.equal(record.delivery.state, "acknowledged");
});

test("restarting the transport preserves the first command context instead of adopting the new shift", async () => {
    const oldAuthor = {actor_id: "12", access_id: "7", role: "mining_master", shift_id: "10"};
    const r = createRuntime({fetch: async () => { throw Error("offline"); }});
    const transport = r.context.createDispatcherTransport({getCommandContext: () => oldAuthor});
    await transport.post("/assign/", {client_action_id: "restart-context"});
    const firstHeader = r.fetchCalls[0].options.headers["X-Command-Context"];
    const next = createRuntime({storage: Object.fromEntries(r.storage)});
    const restarted = next.context.createDispatcherTransport({getCommandContext: () => ({...oldAuthor, shift_id: "20"})});
    await restarted.send(restarted.readQueue()[0]);
    assert.equal(next.fetchCalls[0].options.headers["X-Command-Context"], firstHeader);
});

test("fresh fragment identity wins over the stale outer shell for newly saved commands", async () => {
    for (const mobile of [true, false]) {
        const r = createRuntime();
        const fresh = {dispatcherCommandActorId: "12", dispatcherCommandAccessId: "7", dispatcherCommandRole: "mining_master", dispatcherCommandShiftId: "30"};
        r.context.document = {querySelector: selector => {
            if (selector.startsWith(".mm-mobile-shell")) return mobile ? {dataset: fresh} : null;
            if (selector.startsWith(".dispatcher-board")) return {dataset: fresh};
            return {dataset: {...fresh, dispatcherCommandShiftId: "10"}};
        }};
        const transport = r.context.createDispatcherTransport({});
        await transport.post("/assign/", {client_action_id: "fresh-fragment"});
        assert.equal(JSON.parse(r.fetchCalls[0].options.headers["X-Command-Context"]).author.shift_id, "30");
    }
});

test("unsupported legacy endpoint retains its original behavior without invented context", async () => {
    const author = {access_id: "7", role: "dispatcher", shift_id: ""};
    const legacy = command("old-incomplete", {author});
    const r = createRuntime({storage: {[QUEUE_KEY]: JSON.stringify([legacy])}, fetch: async () => ({ok:false,status:409,
        json:async()=>({ok:false,code:"command_context_invalid",error:"Контекст неполон"})})});
    const transport = r.context.createDispatcherTransport({getCommandContext: () => ({...author, actor_id:"12",shift_id:"30"})});
    await transport.flush();
    await drain();
    const header = JSON.parse(r.fetchCalls[0].options.headers["X-Command-Context"]);
    assert.deepEqual(header.author, author);
    assert.equal(transport.readQueue().length,1);
    assert.equal(transport.readQueue()[0].author.shift_id, "");
});

const receiptOwner = {actor_id: "12", access_id: "7", role: "dispatcher", shift_id: "30"};
const receiptCommand = (extra = {}) => command("sync-old", {url: "/dispatcher/control/truck/assign/",
    author: {access_id: "7", role: "dispatcher", shift_id: ""}, ...extra});
const receiptResult = (request) => ({ok: true, status: "acknowledged", receipt: {ok: true, deduplicated: true},
    evidence: {actor_id: 12, shift_id: 10, client_action_id: request.data.client_action_id, action_type: "dispatcher_assign_truck"}});
const receiptReply = (request) => ({ok: true, json: async () => receiptResult(request)});

for (const version of [1, 2, 3]) {
    test("legacy v" + version + " recovers only the receipt and keeps the exact original array and author", async () => {
        const source = receiptCommand();
        const key = "mining-master-mobile-sync-queue-v" + version;
        const raw = JSON.stringify([source], null, 2);
        const r = createRuntime({storage: {[key]: raw}, fetch: async () => receiptReply(source)});
        const transport = r.context.createDispatcherTransport({getCommandContext: () => receiptOwner});
        await transport.flush();
        assert.equal(r.fetchCalls.length, 1);
        assert.equal(r.fetchCalls[0].url, "/assignments/commands/receipt/");
        assert.deepEqual(JSON.parse(r.fetchCalls[0].options.body), source);
        assert.equal(r.storage.get(key), raw);
        const record = JSON.parse(r.storage.get(transport.journalPrefix + source.id));
        assert.deepEqual(record.request, source);
        assert.equal(record.delivery.state, "acknowledged");
        assert.equal(record.delivery.reconciliation.evidence.shift_id, 10);
        assert.equal(record.request.author.shift_id, "");
        assert.equal(transport.readQueue().length, 0);
        const restarted = r.context.createDispatcherTransport({getCommandContext: () => receiptOwner});
        await restarted.flush();
        assert.equal(r.fetchCalls.length, 1, "durable receipt prevents duplicate lookup after restart");
    });
}

test("held and pending autoRetry:false recover accepted commands without reposting the mutation", async () => {
    for (const state of ["held", "pending"]) {
        const source = receiptCommand({autoRetry: false, author: {...receiptOwner, shift_id: "10"}, occurredAt: "2026-10-05T00:00:00Z"});
        const key = QUEUE_KEY + ":command:" + source.id;
        const r = createRuntime({storage: {[key]: JSON.stringify({request: source, delivery: {state}})},
            fetch: async () => receiptReply(source)});
        const transport = r.context.createDispatcherTransport({getCommandContext: () => receiptOwner});
        await transport.flush();
        assert.deepEqual(r.fetchCalls.map(c => c.url), ["/assignments/commands/receipt/"]);
        assert.equal(JSON.parse(r.storage.get(key)).delivery.state, "acknowledged");
        assert.deepEqual(JSON.parse(r.storage.get(key)).request, source);
    }
});

test("unresolved or wrong receipts never remove, reject, execute or enrich the legacy source", async () => {
    for (const result of [{ok: true, status: "unresolved"}, {ok: true}, {ok: false},
        {...receiptResult(receiptCommand()), evidence: {actor_id: 13, client_action_id: "sync-old"}},
        {...receiptResult(receiptCommand()), evidence: {actor_id: 12, client_action_id: "different"}}]) {
        const raw = JSON.stringify([receiptCommand()]);
        const r = createRuntime({storage: {[QUEUE_KEY]: raw}, fetch: async () => ({ok: true, json: async () => result})});
        const transport = r.context.createDispatcherTransport({getCommandContext: () => receiptOwner});
        await transport.flush();
        await transport.flush();
        assert.equal(r.fetchCalls.length, 1, "unresolved lookup is throttled");
        assert.equal(r.storage.get(QUEUE_KEY), raw);
        assert.equal(r.storage.has(transport.journalPrefix + "sync-old"), false);
        await assert.rejects(transport.send(receiptCommand()), e => e.code === "command_context_incomplete");
        assert.equal(r.fetchCalls.length, 1);
    }
});

test("receipt lookup waits for the original author and never uses a changed session as proof", async () => {
    let current = {...receiptOwner, access_id: "8", actor_id: "13"};
    const source = receiptCommand(), raw = JSON.stringify([source]);
    const r = createRuntime({storage: {[QUEUE_KEY]: raw}, fetch: async () => receiptReply(source)});
    const transport = r.context.createDispatcherTransport({getCommandContext: () => current});
    await transport.flush();
    assert.equal(r.fetchCalls.length, 0);
    current = receiptOwner;
    await transport.flush();
    assert.equal(JSON.parse(r.storage.get(transport.journalPrefix + source.id)).delivery.state, "acknowledged");
});

test("receipt quota failure preserves original and restart can retry without executing it", async () => {
    const source = receiptCommand(), raw = JSON.stringify([source]);
    const r = createRuntime({storage: {[QUEUE_KEY]: raw}, storageFails: () => true,
        fetch: async () => receiptReply(source)});
    await r.context.createDispatcherTransport({getCommandContext: () => receiptOwner}).flush();
    assert.equal(r.storage.get(QUEUE_KEY), raw);
    assert.equal(r.storage.size, 1);
    const next = createRuntime({storage: Object.fromEntries(r.storage), fetch: async () => receiptReply(source)});
    const transport = next.context.createDispatcherTransport({getCommandContext: () => receiptOwner});
    await transport.flush();
    assert.equal(JSON.parse(next.storage.get(transport.journalPrefix + source.id)).delivery.state, "acknowledged");
});

test("hanging receipt body releases the transport on deadline and late response cannot write ACK", async () => {
    const source = receiptCommand(), raw = JSON.stringify([source]);
    let respond;
    const r = createRuntime({storage: {[QUEUE_KEY]: raw}, fetch: async () => ({ok: true,
        json: () => new Promise(resolve => {respond = resolve;})})});
    const transport = r.context.createDispatcherTransport({getCommandContext: () => receiptOwner});
    const pending = transport.flush();
    await drain();
    Array.from(r.timers.values()).find(timer => timer.delay === 12000).callback();
    await pending;
    respond(receiptResult(source));
    await drain();
    assert.equal(r.storage.size, 1);
    assert.equal(r.storage.get(QUEUE_KEY), raw);
    assert.equal(transport.getQueueState().isFlushing, false);
});

test("corrupt legacy and duplicate IDs with different originals are preserved and never guessed", async () => {
    const source = receiptCommand();
    const v1 = "mining-master-mobile-sync-queue-v1", v2 = "mining-master-mobile-sync-queue-v2";
    const first = JSON.stringify([source]);
    const second = JSON.stringify([{...source, data: {...source.data, truck_id: 999}}]);
    const r = createRuntime({storage: {[v1]: first, [v2]: second, [QUEUE_KEY]: "broken"}});
    await r.context.createDispatcherTransport({getCommandContext: () => receiptOwner}).flush();
    assert.equal(r.fetchCalls.length, 0);
    assert.deepEqual(Object.fromEntries(r.storage), {[v1]: first, [v2]: second, [QUEUE_KEY]: "broken"});
});

test("receipt backlog advances past an unresolved first record and never delays a new send timer", async () => {
    const first = receiptCommand(), second = receiptCommand({id: "sync-second", data: {client_action_id: "second"}});
    const r = createRuntime({storage: {[QUEUE_KEY]: JSON.stringify([first, second])},
        fetch: async (url, init) => JSON.parse(init.body).id === first.id
            ? {ok: true, json: async () => ({ok: true, status: "unresolved"})} : receiptReply(second)});
    const transport = r.context.createDispatcherTransport({getCommandContext: () => receiptOwner});
    transport.scheduleFlush(0);
    await transport.flush();
    assert.ok(Array.from(r.timers.values()).some(timer => timer.delay === 0), "receipt callback cannot postpone new work");
    await transport.flush();
    assert.equal(r.fetchCalls.length, 2);
    assert.equal(JSON.parse(r.storage.get(transport.journalPrefix + second.id)).delivery.state, "acknowledged");
    assert.equal(transport.readQueue().length, 1);
    await transport.flush();
    assert.equal(r.fetchCalls.length, 2);
});

test("changed original during receipt HTTP cannot be overwritten by an old acknowledgement", async () => {
    const source = receiptCommand(), key = QUEUE_KEY + ":command:" + source.id;
    let resolve;
    const r = createRuntime({storage: {[key]: JSON.stringify({request: source, delivery: {state: "held"}})},
        fetch: () => new Promise(done => {resolve = done;})});
    const transport = r.context.createDispatcherTransport({getCommandContext: () => receiptOwner});
    const pending = transport.flush();
    await drain();
    const replaced = JSON.stringify({request: {...source, data: {...source.data, truck_id: 888}}, delivery: {state: "held"}});
    r.storage.set(key, replaced);
    resolve(receiptReply(source));
    await pending;
    assert.equal(r.storage.get(key), replaced);
});

test("legacy autoRetry:false with full context is checked without automatically executing it", async () => {
    const source = receiptCommand({autoRetry: false, author: receiptOwner, occurredAt: "2026-10-05T00:00:00Z"});
    const raw = JSON.stringify([source]);
    const r = createRuntime({storage: {[QUEUE_KEY]: raw}, fetch: async () => ({ok: true,
        json: async () => ({ok: true, status: "unresolved"})})});
    const transport = r.context.createDispatcherTransport({getCommandContext: () => receiptOwner});
    await transport.flush();
    assert.deepEqual(r.fetchCalls.map(c => c.url), ["/assignments/commands/receipt/"]);
    assert.equal(r.storage.get(QUEUE_KEY), raw);
});
