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

function serverAck(url, init) {
    const data = JSON.parse(init.body);
    const result = data.zone === 'inactive' || data.action === 'release_complex'
        ? {ok: true, assignment_state_ids: {7: 71, 8: 72}}
        : {ok: true, truck_id: Number(data.truck_id), assignment_state_id: 81};
    return Promise.resolve({ok: true, status: 200, json: async () => result});
}
for (const disband of [false, true]) {
    const kind = disband ? 'disband' : 'release';
    test(kind + ': queued assign keeps bulk reference before ACK and after restart', async () => {
        const first = setup({fetch: serverAck});
        const command = mass(first, disband, {7: 41});
        const child = first.stored('child', '7', '41');
        assert.equal(child.data.expected_assignment_state_id, 'bulk:' + kind + ':mass');
        const r = setup({storage: Object.fromEntries(first.storage), fetch: serverAck});
        await assert.rejects(r.transport.send(child), {code: 'command_dependency_pending'});
        assert.equal(r.fetchCalls.length, 0);
        // Explicit still-active operation, never a background replay of structural intent.
        await r.transport.send(command); await r.transport.send(child);
        assert.equal(bodies(r)[1].expected_assignment_state_id, 'bulk:' + kind + ':mass');
        assert.equal(JSON.stringify(r.record(child).request), JSON.stringify(child));
    });
    test(kind + ': explicit token remains valid after bulk ACK, numeric confirmed ID needs no dependency', async () => {
        const r = setup({fetch: serverAck});
        const command = mass(r, disband, {7: 41, 8: 42}); await r.transport.send(command);
        const child = r.stored('child', '7', 'bulk:' + kind + ':mass');
        const numeric = r.stored('numeric', '8', '72');
        assert.equal(numeric.data.expected_assignment_state_id, '72');
        await r.transport.send(child); await r.transport.send(numeric);
        assert.equal(r.record(child).delivery.state, 'acknowledged');
        assert.equal(r.record(numeric).delivery.state, 'acknowledged');
    });
    test(kind + ': single → bulk → single selects the causal tail, including a fourth command', async () => {
        const r = setup({fetch: serverAck});
        const a = r.stored('a'); const command = mass(r, disband, {7: '0'});
        const b = r.stored('b'), c = r.stored('c');
        assert.equal(b.data.expected_assignment_state_id, 'bulk:' + kind + ':mass');
        assert.equal(c.data.expected_assignment_state_id, 'command:b');
        await r.transport.send(a); await r.transport.send(command);
        await r.transport.send(b); await r.transport.send(c);
        assert.deepEqual(bodies(r).map(data => data.client_action_id), ['a', 'mass', 'b', 'c']);
    });
}

for (const state of ['held', 'rejected']) {
    test('a ' + state + ' bulk parent blocks successor but not an independent truck', async () => {
        const r = setup({fetch: serverAck});
        const command = mass(r, false, {7: 41}); const child = r.stored('child');
        const saved = r.record(command); saved.delivery.state = state;
        r.storage.set(r.transport.journalPrefix + encodeURIComponent(command.id), JSON.stringify(saved));
        await assert.rejects(r.transport.send(child), {code: 'command_dependency_pending'});
        const other = r.stored('other', '9'); await r.transport.send(other);
        assert.deepEqual(bodies(r).map(data => data.client_action_id), ['other']);
        assert.equal(r.record(child).delivery.state, 'pending');
    });
}

test('ACK without the truck in its result never unlocks that successor', async () => {
    const r = setup({fetch: async () => ({ok: true, status: 200, json: async () => ({ok: true, assignment_state_ids: {8: 72}})})});
    const command = mass(r, true, {7: 41}); const child = r.stored('child');
    await r.transport.send(command);
    await assert.rejects(r.transport.send(child), {code: 'command_dependency_pending'});
    assert.equal(r.fetchCalls.length, 1);
});

test('receipt recovery unlocks the successor without re-executing a held bulk command', async () => {
    const r = setup({fetch: (url, init) => url === '/assignments/commands/receipt/'
        ? Promise.resolve({ok: true, json: async () => ({ok: true, status: 'acknowledged',
            evidence: {client_action_id: 'mass', actor_id: 1}, receipt: {ok: true, assignment_state_ids: {7: 71}}})})
        : serverAck(url, init)});
    const command = mass(r, true, {7: 41}); const child = r.stored('child');
    const saved = r.record(command); saved.delivery.state = 'held';
    r.storage.set(r.transport.journalPrefix + encodeURIComponent(command.id), JSON.stringify(saved));
    await r.transport.flush(); await r.transport.flush();
    assert.deepEqual(bodies(r).map(data => data.client_action_id), ['child']);
    assert.equal(r.record(child).delivery.state, 'acknowledged');
});

test('bulk receipt updates every Master card kind only while its own token is still current', () => {
    const r = setup(); const template = fs.readFileSync(path.join(BACKEND, 'templates/trips/dispatcher_control.html'), 'utf8');
    vm.runInContext(template.slice(template.indexOf('    function haulAssignmentStateId('), template.indexOf('    window.addEventListener("focus"')), r.context);
    const ids = ['equipmentId', 'mmMobileHomeTruckId', 'mmMobileAssignedTruckId', 'mmMobileFillTruckId', 'equipmentCardId'];
    const nodes = ids.map(key => ({dataset: {[key]: '7', haulAssignmentStateId: 'bulk:release:mass'}}));
    nodes.push({dataset: {equipmentCardId: '7', haulAssignmentStateId: 'command:newer'}});
    nodes.push({dataset: {equipmentCardId: '8', haulAssignmentStateId: 'bulk:release:mass'}});
    assert.equal(r.context.haulAssignmentStateId(nodes[0]), 'bulk:release:mass');
    r.context.applyHaulAssignmentStates({assignment_state_ids: {7: 71}}, {querySelectorAll: () => nodes}, {7: 'bulk:release:mass', 8: 'bulk:release:mass'});
    assert.deepEqual(nodes.map(node => node.dataset.haulAssignmentStateId), ['71', '71', '71', '71', '71', 'command:newer', 'bulk:release:mass']);
});

test('real Master receipt callback also reconciles garage copies after a recovered bulk ACK', () => {
    const r = setup(); const template = fs.readFileSync(path.join(BACKEND, 'templates/trips/dispatcher_control.html'), 'utf8');
    vm.runInContext(template.slice(template.indexOf('    function applyHaulAssignmentStateMap('), template.indexOf('    window.addEventListener("focus"')), r.context);
    const node = {dataset: {mmMobileFillTruckId: '7', haulAssignmentStateId: 'bulk:release:mass'}};
    Object.assign(r.context, {document: {querySelector: () => ({querySelectorAll: () => [node]})},
        dispatcherTransport: r.transport, readDispatcherSyncQueue: () => [], refreshMobileBoardFromServer: () => Promise.resolve()});
    const start = template.indexOf('        onAcknowledged: function (request, response) {');
    const end = template.indexOf('\n    });', start);
    const callback = vm.runInContext('(' + template.slice(start, end).trim().replace(/^onAcknowledged: /, '') + ')', r.context);
    const command = {data: {action: 'release_complex', client_action_id: 'mass', expected_assignment_states: {7: 41}}};
    callback(command, {ok: true, assignment_state_ids: {7: 71}});
    assert.equal(node.dataset.haulAssignmentStateId, '71');
    node.dataset.haulAssignmentStateId = 'command:newer';
    callback(command, {ok: true, assignment_state_ids: {7: 71}});
    assert.equal(node.dataset.haulAssignmentStateId, 'command:newer');
});

test('a later mass barrier follows the single-command tail through the intervening bulk node', () => {
    const r = setup(); r.stored('a'); mass(r, false, {7: 0}); const child = r.stored('child');
    const next = r.transport.storePost(URL, {client_action_id: 'next-mass', action: 'release_complex', excavator_id: '9',
        expected_assignment_states: {7: 0}}, {queueOnNetworkFailure: false});
    assert.equal(child.data.expected_assignment_state_id, 'bulk:release:mass');
    assert.deepEqual(JSON.parse(JSON.stringify(next.data.assignment_dependencies)), [{truck_id: '7', client_action_id: 'child'}]);
    assert.equal(next.data.expected_assignment_states['7'], 'command:child');
    assert.equal(r.fetchCalls.length, 0);
});
