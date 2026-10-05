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
    const transport = runtime.context.createDispatcherTransport({getCommandContext: () => options.author || AUTHOR, onAcknowledged: options.onAcknowledged});
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

function mass(r, disband, states = {7: '0', 8: '0'}) {
    return r.transport.storePost(disband ? URL.replace('truck/assign/', 'excavator/move/') : URL,
        {client_action_id: 'mass', excavator_id: '9', expected_assignment_states: states,
            ...(disband ? {zone: 'inactive', expected_zone: 'active'} : {action: 'release_complex'})},
        {queueOnNetworkFailure: false});
}
async function tick(r, delay) {
    for (const [id, timer] of [...r.timers]) {
        if (timer.delay === delay) { r.timers.delete(id); timer.callback(); }
    }
    for (let n = 0; n < 12; n++) await Promise.resolve();
}
function bodies(r) { return r.fetchCalls.filter(c => c.url !== '/assignments/commands/receipt/').map(c => JSON.parse(c.options.body)); }

for (const disband of [false, true]) {
    test((disband ? 'disband' : 'release all') + ': waits for every durable predecessor then sends unchanged map', async () => {
        const r = setup({fetch: (_url, init) => ack(init)});
        const a = r.stored('a', '7'), b = r.stored('b', '8');
        const command = mass(r, disband), original = JSON.stringify(command);
        assert.deepEqual(JSON.parse(JSON.stringify(command.data.expected_assignment_states)), {7: 'command:a', 8: 'command:b'});
        const sending = r.transport.send(command);
        assert.equal(r.transport.getQueueState().waitingDependencyCount, 1);
        assert.equal(bodies(r).length, 0);
        await r.transport.send(a); await tick(r, 500);
        assert.equal(bodies(r).length, 1);
        await r.transport.send(b); await tick(r, 500);
        await sending;
        assert.deepEqual(bodies(r).map(x => x.client_action_id), ['a', 'b', 'mass']);
        assert.equal(JSON.stringify(r.record(command).request), original);
        assert.equal(r.record(command).delivery.state, 'acknowledged');
        assert.equal(r.transport.getQueueState().pendingCount, 0);
        assert.equal(r.transport.getQueueState().waitingDependencyCount, 0);
    });
}

test('truck no longer visible in complex still creates a saved dependency barrier', async () => {
    const r = setup({fetch: (_url, init) => ack(init)});
    const outgoing = r.stored('outgoing', '7');
    const command = mass(r, true, {});
    assert.deepEqual(JSON.parse(JSON.stringify(command.data.assignment_dependencies)), [{truck_id: '7', client_action_id: 'outgoing'}]);
    const sending = r.transport.send(command);
    await r.transport.send(outgoing); await tick(r, 500); await sending;
    assert.deepEqual(bodies(r).map(x => x.client_action_id), ['outgoing', 'mass']);
    assert.deepEqual(bodies(r)[1].expected_assignment_states, {});
});

test('wait expires, releases UI promise, retains held original and never replays it after a late ACK', async () => {
    const r = setup({fetch: (_url, init) => ack(init)});
    const a = r.stored('a'); const command = mass(r, true, {7: '0'});
    const original = JSON.stringify(command), sending = r.transport.send(command);
    const rejected = assert.rejects(sending, {code: 'request_timeout'});
    await tick(r, 12000); await rejected;
    assert.equal(r.record(command).delivery.state, 'held');
    assert.equal(r.transport.getQueueState().pendingCount, 0);
    assert.equal(r.transport.getQueueState().waitingDependencyCount, 0);
    await r.transport.send(a); await tick(r, 500); await r.transport.flush();
    assert.equal(bodies(r).some(body => body.client_action_id === 'mass'), false);
    assert.equal(JSON.stringify(r.record(command).request), original);
});

test('process restart preserves a saved mass command but does not autonomously execute it', async () => {
    const first = setup(); first.stored('a'); const command = mass(first, true, {7: '0'});
    const r = setup({storage: Object.fromEntries(first.storage), fetch: (_url, init) => ack(init)});
    await r.transport.flush(); await r.transport.flush();
    assert.equal(bodies(r).some(body => body.client_action_id === 'mass'), false);
    assert.equal(JSON.stringify(r.record(command).request), JSON.stringify(command));
});

test('changed author while waiting blocks mass HTTP and preserves the original author', async () => {
    const author = {...AUTHOR};
    const r = setup({author, fetch: (_url, init) => ack(init)});
    r.stored('a'); const command = mass(r, false, {7: '0'});
    const sending = r.transport.send(command);
    author.access_id = 'different';
    const rejected = assert.rejects(sending, {code: 'command_author_mismatch'});
    await tick(r, 500); await rejected;
    assert.equal(bodies(r).length, 0);
    assert.equal(r.record(command).request.author.access_id, '2');
    assert.equal(r.record(command).delivery.state, 'held');
});

test('numeric map plus explicit ACK token and an unrelated pending truck keep all required predecessors', async () => {
    const r = setup({fetch: (_url, init) => ack(init)});
    const a = r.stored('a', '7'); await r.transport.send(a);
    const b = r.stored('b', '8');
    const command = mass(r, false, {7: 'command:a'});
    const sending = r.transport.send(command);
    await r.transport.send(b); await tick(r, 500); await sending;
    assert.equal(bodies(r).at(-1).expected_assignment_states['7'], 'command:a');
    assert.deepEqual(bodies(r).at(-1).assignment_dependencies, [{truck_id: '8', client_action_id: 'b'}]);
});

test('double send during the dependency wait has one promise and one mass HTTP', async () => {
    const r = setup({fetch: (_url, init) => ack(init)});
    const a = r.stored('a'), command = mass(r, false, {7: '0'});
    const one = r.transport.send(command), two = r.transport.send(command);
    assert.equal(one, two);
    await r.transport.send(a); await tick(r, 500); await Promise.all([one, two]);
    assert.equal(bodies(r).filter(body => body.client_action_id === 'mass').length, 1);
});

test('ACK quota keeps mass waiting; expiry and a later retry never silently execute the rolled back mass command', async () => {
    let quota = true;
    const r = setup({fetch: (_url, init) => ack(init), storageFails: (key, value) =>
        quota && key.endsWith(':command:sync-a') && JSON.parse(value).delivery.state === 'acknowledged'});
    const a = r.stored('a'), command = mass(r, true, {7: '0'});
    const sending = r.transport.send(command);
    await assert.rejects(r.transport.send(a)); await tick(r, 500);
    assert.equal(bodies(r).length, 1);
    const rejected = assert.rejects(sending, {code: 'request_timeout'});
    await tick(r, 12000); await rejected;
    quota = false; await r.transport.send(a); await tick(r, 500);
    assert.equal(bodies(r).some(body => body.client_action_id === 'mass'), false);
});

test('quota on the mass source causes no effects and leaves predecessor unchanged', () => {
    let quota = false;
    const r = setup({storageFails: key => quota && key.includes(':command:sync-mass')});
    const a = r.stored('a'); const original = JSON.stringify(r.record(a)); quota = true;
    assert.throws(() => mass(r, true, {7: '0'}), {code: 'storage_unavailable'});
    assert.equal(JSON.stringify(r.record(a)), original);
    assert.equal(r.fetchCalls.length, 0);
});

test('previous employee or previous shift commands are never attached to mass operations', () => {
    const first = setup(); first.stored('old');
    for (const key of ['access_id', 'shift_id']) {
        const r = setup({storage: Object.fromEntries(first.storage), author: {...AUTHOR, [key]: 'new'}});
        const command = mass(r, true, {});
        assert.equal(command.data.assignment_dependencies, undefined);
    }
});

test('real Master ACK callback does not refresh the board in the middle of a waiting structural gesture', async () => {
    const r = setup({fetch: (_url, init) => ack(init)});
    const template = fs.readFileSync(path.join(BACKEND, 'templates/trips/dispatcher_control.html'), 'utf8');
    const start = template.indexOf('        onAcknowledged: function (request, response) {');
    const end = template.indexOf('\n    });', start);
    const callback = template.slice(start, end).trim().replace(/^onAcknowledged: /, '');
    let refreshes = 0;
    Object.assign(r.context, {dispatcherTransport: r.transport, readDispatcherSyncQueue: () => [],
        refreshMobileBoardFromServer: () => { refreshes++; return Promise.resolve(); }});
    const onAck = vm.runInContext('(' + callback + ')', r.context);
    const a = r.stored('a'), command = mass(r, false, {7: '0'});
    const sending = r.transport.send(command);
    onAck({refreshMobileBoard: true}); assert.equal(refreshes, 0);
    await r.transport.send(a); await tick(r, 500); await sending;
    onAck({refreshMobileBoard: true}); assert.equal(refreshes, 1);
});

for (const delay of [500, 12000]) {
    test('durable mass receipt arriving during dependency wait wins over ' + delay + 'ms callback without new HTTP', async () => {
        const r = setup(); r.stored('a'); const command = mass(r, true, {7: '0'});
        const sending = r.transport.send(command);
        const saved = r.record(command);
        saved.delivery = {state: 'acknowledged', receipt: {ok: true, scheduled: 1, deduplicated: true}};
        r.storage.set(r.transport.journalPrefix + encodeURIComponent(command.id), JSON.stringify(saved));
        await tick(r, delay);
        const receipt = await sending;
        assert.equal(receipt.ok, true);
        assert.equal(receipt.deduplicated, true);
        assert.equal(r.fetchCalls.length, 0);
        assert.equal(r.transport.getQueueState().pendingCount, 0);
        assert.equal(r.record(command).delivery.state, 'acknowledged');
    });
}
