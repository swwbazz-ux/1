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

const URL = '/mining-master/assignments/truck/assign/';
const AUTHOR = {actor_id: '1', access_id: '2', role: 'mining_master', shift_id: '3'};
function setup(options = {}) {
    const runtime = createRuntime(options);
    const transport = runtime.context.createDispatcherTransport({getCommandContext: () => options.author || AUTHOR});
    function stored(id, truck = '7', state = '0', action = 'assign') {
        return transport.storePost(URL, {client_action_id: id, action, truck_id: truck,
            excavator_id: id, expected_assignment_state_id: state});
    }
    function record(request) { return JSON.parse(runtime.storage.get(transport.journalPrefix + encodeURIComponent(request.id))); }
    return {...runtime, transport, stored, record};
}
function ack(init, state = 41) {
    const payload = JSON.parse(init.body);
    return Promise.resolve({ok: true, status: 200, json: async () => ({ok: true,
        client_action_id: payload.client_action_id, truck_id: Number(payload.truck_id), assignment_state_id: state})});
}

test('assign → assign → release stores a causal chain before HTTP and keeps original bodies after ACK', async () => {
    const r = setup({fetch: (_url, init) => ack(init)});
    const a = r.stored('a'), b = r.stored('b'), c = r.stored('c', '7', '0', 'release');
    assert.equal(r.fetchCalls.length, 0);
    assert.equal(a.data.expected_assignment_state_id, '0');
    assert.equal(b.data.expected_assignment_state_id, 'command:a');
    assert.equal(c.data.expected_assignment_state_id, 'command:b');
    const originals = [a, b, c].map(JSON.stringify);
    await assert.rejects(r.transport.send(c), {code: 'command_dependency_pending'});
    assert.equal(r.fetchCalls.length, 0);
    await r.transport.send(a);
    await r.transport.send(b);
    await r.transport.send(c);
    assert.deepEqual(r.fetchCalls.map(call => JSON.parse(call.options.body).client_action_id), ['a', 'b', 'c']);
    [a,b,c].forEach((request, index) => assert.equal(JSON.stringify(r.record(request).request), originals[index]));
    assert.equal(JSON.parse(r.fetchCalls[1].options.body).expected_assignment_state_id, 'command:a');
});

test('process restart retains the edge, waits for predecessor, and does not reuse the stale numeric token', async () => {
    const initial = setup();
    const a = initial.stored('a'), b = initial.stored('b');
    const r = setup({storage: Object.fromEntries(initial.storage), fetch: (_url, init) => ack(init)});
    await assert.rejects(r.transport.send(b), {code: 'command_dependency_pending'});
    await r.transport.flush();
    await r.transport.flush();
    assert.deepEqual(r.fetchCalls.map(call => JSON.parse(call.options.body).client_action_id), ['a', 'b']);
    assert.equal(r.record(b).request.data.expected_assignment_state_id, 'command:a');
    assert.equal(r.record(a).delivery.state, 'acknowledged');
});

test('slow predecessor cannot be overtaken by a direct post and another truck still progresses', async () => {
    let respond;
    const r = setup({fetch: (_url, init) => JSON.parse(init.body).client_action_id === 'a'
        ? new Promise(resolve => { respond = () => ack(init).then(resolve); }) : ack(init)});
    const a = r.stored('a');
    const sending = r.transport.send(a);
    await Promise.resolve();
    const result = await r.transport.post(URL, {client_action_id: 'b', action: 'assign', truck_id: '7', expected_assignment_state_id: '0'});
    assert.equal(result.queued, true);
    const other = r.stored('other', '8');
    await r.transport.flush();
    assert.deepEqual(r.fetchCalls.map(call => JSON.parse(call.options.body).client_action_id), ['a', 'other']);
    await respond(); await sending;
    await r.transport.flush();
    assert.equal(r.fetchCalls.length, 3);
    assert.equal(r.record(other).delivery.state, 'acknowledged');
});

test('lost response retries the exact predecessor before delivering its child', async () => {
    let lost = true;
    const r = setup({fetch: (_url, init) => {
        if (lost) { lost = false; return Promise.reject(new Error('response lost')); }
        return ack(init);
    }});
    const a = r.stored('a'), b = r.stored('b');
    await assert.rejects(r.transport.send(a), /response lost/);
    await assert.rejects(r.transport.send(b), {code: 'command_dependency_pending'});
    await r.transport.send(a);
    await r.transport.send(b);
    assert.deepEqual(r.fetchCalls.map(call => JSON.parse(call.options.body).client_action_id), ['a', 'a', 'b']);
    assert.equal(r.fetchCalls[0].options.body, r.fetchCalls[1].options.body);
    assert.equal(r.fetchCalls[0].options.headers['X-Command-Context'], r.fetchCalls[1].options.headers['X-Command-Context']);
});

for (const state of ['held', 'rejected']) {
    test('a ' + state + ' predecessor never silently unlocks its child', async () => {
        const r = setup({fetch: (_url, init) => ack(init)});
        const a = r.stored('a'), b = r.stored('b');
        const record = r.record(a); record.delivery.state = state;
        r.storage.set(r.transport.journalPrefix + encodeURIComponent(a.id), JSON.stringify(record));
        await r.transport.flush();
        await assert.rejects(r.transport.send(b), {code: 'command_dependency_pending'});
        assert.equal(r.fetchCalls.filter(call => call.url === URL).length, 0);
        assert.equal(r.record(b).delivery.attempts, 0);
    });
}

test('missing local predecessor blocks HTTP without dropping the original child', async () => {
    const r = setup();
    const child = r.stored('b', '7', 'command:missing');
    await assert.rejects(r.transport.send(child), {code: 'command_dependency_pending'});
    assert.equal(r.fetchCalls.length, 0);
    assert.equal(r.record(child).delivery.state, 'pending');
});

test('author, role and shift changes never attach a predecessor from another context', () => {
    const r = setup(); r.stored('a');
    for (const field of ['actor_id', 'access_id', 'role', 'shift_id']) {
        const next = setup({storage: Object.fromEntries(r.storage), author: {...AUTHOR, [field]: 'different'}});
        assert.equal(next.stored('b').data.expected_assignment_state_id, '0');
    }
});

test('quota while storing the child does not change its predecessor or create a false child', () => {
    let fail = false;
    const r = setup({storageFails: key => fail && key.includes(':command:sync-b')});
    const a = r.stored('a'), original = JSON.stringify(r.record(a));
    fail = true;
    assert.throws(() => r.stored('b'), {code: 'storage_unavailable'});
    assert.equal(JSON.stringify(r.record(a)), original);
    assert.equal(r.transport.readQueue().length, 1);
    assert.equal(r.fetchCalls.length, 0);
});

test('a confirmed card keeps an explicit command token, even after parent ACK', async () => {
    const r = setup({fetch: (_url, init) => ack(init)});
    const a = r.stored('a'); await r.transport.send(a);
    const b = r.stored('b', '7', 'command:a');
    await r.transport.send(b);
    assert.equal(JSON.parse(r.fetchCalls[1].options.body).expected_assignment_state_id, 'command:a');
    assert.equal(r.record(b).delivery.state, 'acknowledged');
});

test('two unresolved branches are retained and a third command is refused before confirmation', () => {
    const r = setup(); const a = r.stored('a');
    const fork = JSON.parse(JSON.stringify(r.record(a)));
    fork.request.id = 'fork'; fork.request.data.client_action_id = 'fork';
    r.storage.set(r.transport.journalPrefix + 'fork', JSON.stringify(fork));
    assert.throws(() => r.stored('c'), {code: 'command_dependency_conflict'});
    assert.equal(r.transport.readQueue().length, 2);
    assert.equal(r.fetchCalls.length, 0);
});

test('ACK storage failure keeps the child waiting until the predecessor receipt is durable', async () => {
    let failAck = true;
    const r = setup({fetch: (_url, init) => ack(init), storageFails: (key, value) =>
        failAck && key.endsWith(':command:sync-a') && JSON.parse(value).delivery.state === 'acknowledged'});
    const a = r.stored('a'), b = r.stored('b');
    await assert.rejects(r.transport.send(a), /QuotaExceededError/);
    await assert.rejects(r.transport.send(b), {code: 'command_dependency_pending'});
    assert.equal(r.fetchCalls.length, 1);
    failAck = false;
    await r.transport.send(a); await r.transport.send(b);
    assert.deepEqual(r.fetchCalls.map(call => JSON.parse(call.options.body).client_action_id), ['a', 'a', 'b']);
});

test('read-only receipt recovery unlocks the child without executing its held predecessor again', async () => {
    const r = setup({fetch: (url, init) => url === '/assignments/commands/receipt/'
        ? Promise.resolve({ok: true, json: async () => ({ok: true, status: 'acknowledged',
            evidence: {client_action_id: 'a', actor_id: 1}, receipt: {ok: true, truck_id: 7, assignment_state_id: 41}})})
        : ack(init)});
    const a = r.stored('a'), b = r.stored('b');
    const record = r.record(a); record.delivery.state = 'held';
    r.storage.set(r.transport.journalPrefix + encodeURIComponent(a.id), JSON.stringify(record));
    await r.transport.flush(); await r.transport.flush();
    assert.deepEqual(r.fetchCalls.filter(call => call.url === URL).map(call => JSON.parse(call.options.body).client_action_id), ['b']);
    assert.equal(JSON.stringify(r.record(a).request), JSON.stringify(a));
    assert.equal(r.record(b).delivery.state, 'acknowledged');
});

test('real Master queue helper and card token retain causality even when the predecessor ACK has already arrived', async () => {
    const r = setup({fetch: (_url, init) => ack(init)});
    const template = fs.readFileSync(path.join(BACKEND, 'templates/trips/dispatcher_control.html'), 'utf8');
    const code = template.slice(template.indexOf('    function dispatcherPostQueued('), template.indexOf('    function applyHaulAssignmentStates('));
    Object.assign(r.context, {
        dispatcherRoleIsReadonly: () => false, dispatcherTransport: r.transport,
        enqueueDispatcherSyncRequest: r.transport.enqueue, dispatcherMobileSyncFlushDelayMs: 300,
    });
    vm.runInContext(code, r.context);
    const node = {dataset: {haulAssignmentStateId: '0'}};
    const saved = await r.context.dispatcherPostQueued(URL, {client_action_id: 'a', action: 'assign', truck_id: '7', expected_assignment_state_id: '0'});
    r.context.applyHaulAssignmentState(saved, node);
    assert.equal(node.dataset.haulAssignmentStateId, 'command:a');
    await r.transport.flush();
    await r.context.dispatcherPostQueued(URL, {client_action_id: 'b', action: 'release', truck_id: '7', expected_assignment_state_id: r.context.haulAssignmentStateId(node)});
    await r.transport.flush();
    assert.equal(JSON.parse(r.fetchCalls[1].options.body).expected_assignment_state_id, 'command:a');
});
