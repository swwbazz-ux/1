'use strict';
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const CURRENT = fs.readFileSync(path.join(__dirname, '..', 'dispatcher-transport-v1.js'), 'utf8');
// Exact file from PR base 48343af71a8efe5b4cf980483bb2402c1664e942, without edits.
const LEGACY = fs.readFileSync(path.join(__dirname, 'fixtures', 'dispatcher-transport-legacy-48343af.js'), 'utf8');
const QUEUE = 'mining-master-mobile-sync-queue-v3';
const PREFIX = QUEUE + ':command:';
const author = role => ({actor_id: '1', access_id: '2', role, shift_id: '3'});
const drain = () => new Promise(resolve => setImmediate(resolve));
const reply = (body, status = 200) => ({ok: status < 300, status, json: async () => body});
function environment(initial = {}) {
    const storage = new Map(Object.entries(initial)), calls = [], timers = new Map();
    let next = 0, storageFails = () => false, onRead = () => {};
    function client({legacy = false, owner = author('dispatcher'), fetch, readonly = false} = {}) {
        const context = {console, Date, Math, JSON, Object, Promise, Error, FormData: class {},
            localStorage: {get length() { return storage.size; }, key: i => [...storage.keys()][i] || null,
                getItem(k) { const value = storage.get(k) ?? null; onRead(k, value); return value; },
                setItem(k, v) { if (storageFails(k, v)) { const error = new Error('Quota'); error.name = 'QuotaExceededError'; throw error; } storage.set(k, String(v)); },
                removeItem: k => storage.delete(k)},
            setTimeout(fn, delay) { const id = ++next; timers.set(id, {fn, delay}); return id; },
            clearTimeout(id) { timers.delete(id); }, isAppRoleReadonly: () => readonly,
            fetch(url, init) { calls.push({legacy, url, init}); return fetch ? fetch(url, init) : Promise.resolve(reply({ok: true})); }};
        context.window = context; vm.createContext(context); vm.runInContext(legacy ? LEGACY : CURRENT, context);
        const transport = context.createDispatcherTransport({getCommandContext: () => owner});
        return {transport, owner, context};
    }
    const saved = request => JSON.parse(storage.get(PREFIX + encodeURIComponent(request.id)));
    return {storage, calls, timers, client, saved, deny: fn => { storageFails = fn; }, watchReads: fn => { onRead = fn; }};
}
function command(client, id, kind = 'assign') {
    const root = client.owner.role === 'mining_master' ? '/mining-master/assignments/' : '/dispatcher/control/';
    return client.transport.storePost(root + (kind === 'disband' ? 'excavator/move/' : 'truck/assign/'),
        {client_action_id: id, ...(kind === 'disband' ? {zone: 'inactive', expected_zone: 'active', excavator_id: 8, expected_assignment_states: {}}
            : {action: kind, truck_id: 7, excavator_id: 8, expected_assignment_state_id: 0})});
}

for (const role of ['dispatcher', 'mining_master']) {
    for (const kind of ['assign', 'release', 'disband']) {
        test(role + ': cached legacy tab cannot replay modern ' + kind + ' without author context', async () => {
            const e = environment();
            const old = e.client({legacy: true});
            const current = e.client({owner: author(role)});
            const request = command(current, role + kind, kind);
            old.transport.flush(); await drain();
            assert.equal(e.calls.length, 0, 'old sender has no context header and must not see the new command');
            await current.transport.send(request);
            assert.equal(e.calls.length, 1);
            assert.equal(e.calls[0].legacy, false);
            assert.deepEqual(JSON.parse(e.calls[0].init.headers['X-Command-Context']).author, author(role));
            assert.equal(e.saved(request).delivery.state, 'acknowledged');
        });
    }
}

function legacyRequest(id = 'old') {
    return {id: 'legacy-' + id, createdAt: 10, attempts: 0, kind: 'json',
        url: '/dispatcher/control/truck/assign/', data: {client_action_id: id, action: 'release', truck_id: 9, expected_assignment_state_id: 0}};
}
function projection(e, request) {
    return {...e.saved(request).request, attempts: 2, nextAttemptAt: 100, lastError: {code: 'network_error'}};
}

test('legacy runtime is the byte-exact PR base, not a simulated sender', () => {
    assert.equal(crypto.createHash('sha256').update(LEGACY).digest('hex'), 'f8164e52af07282ead4f34b33e5c81b809eadd1b042a262aa509643278bf38f5');
});

test('old enqueue, failed HTTP and queue deletion cannot erase a modern source or retry it', async () => {
    const e = environment();
    const old = e.client({legacy: true, fetch: async () => reply({ok: false, code: 'unavailable'}, 503)});
    const modern = e.client();
    const request = command(modern, 'modern-pending');
    const raw = e.storage.get(PREFIX + request.id);
    old.transport.enqueue(legacyRequest());
    assert.equal(old.transport.readQueue().length, 1);
    old.transport.flush(); await drain();
    assert.equal(e.calls.length, 1);
    assert.equal(JSON.parse(e.calls[0].init.body).client_action_id, 'old');
    assert.equal(e.storage.get(PREFIX + request.id), raw);
    assert.equal(modern.transport.readQueue().length, 1);
    assert.equal(modern.transport.readQueue()[0].id, request.id);
    const restarted = e.client();
    await restarted.transport.send(request);
    assert.equal(e.saved(request).delivery.state, 'acknowledged');
    old.transport.flush(); await drain();
    assert.equal(e.calls.length, 2);
});

for (const state of ['pending', 'held', 'acknowledged', 'rejected', 'blocked']) {
    test('upgrade removes only the old projection of a durable ' + state + ' record', async () => {
        const e = environment();
        const current = e.client();
        const request = command(current, 'upgrade-' + state);
        const record = e.saved(request); record.delivery.state = state;
        e.storage.set(PREFIX + request.id, JSON.stringify(record));
        const raw = e.storage.get(PREFIX + request.id), oldSource = legacyRequest();
        e.storage.set(QUEUE, JSON.stringify([oldSource, projection(e, request)]));
        e.client();
        assert.deepEqual(JSON.parse(e.storage.get(QUEUE)), [oldSource]);
        assert.equal(e.storage.get(PREFIX + request.id), raw, 'source and outcome unchanged');
        const old = e.client({legacy: true});
        old.transport.flush(); await drain();
        assert.equal(e.calls.length, 1);
        assert.equal(JSON.parse(e.calls[0].init.body).client_action_id, 'old');
    });
}

test('a stale projection reintroduced by another tab is isolated again at the next flush', async () => {
    const e = environment();
    const current = e.client();
    const request = command(current, 'late-projection');
    e.storage.set(QUEUE, JSON.stringify([projection(e, request)]));
    await current.transport.flush();
    assert.deepEqual(JSON.parse(e.storage.get(QUEUE)), []);
    const old = e.client({legacy: true}); old.transport.flush(); await drain();
    assert.equal(e.calls.length, 1);
    assert.equal(e.calls[0].legacy, false);
    assert.equal(e.saved(request).delivery.state, 'acknowledged');
});

test('genuine legacy arrays retain exact bytes while fresh commands are stored and acknowledged', async () => {
    const initial = Object.fromEntries([1,2,3].map(version => ['mining-master-mobile-sync-queue-v' + version,
        JSON.stringify([legacyRequest('version-' + version)], null, 2)]));
    const e = environment(initial), current = e.client();
    const request = command(current, 'independent');
    await current.transport.send(request);
    for (const [key, raw] of Object.entries(initial)) assert.equal(e.storage.get(key), raw);
    assert.equal(e.calls.length, 1);
    assert.equal(e.saved(request).delivery.state, 'acknowledged');
});

test('a receipt-only legacy original is not mistaken for a derived journal projection', async () => {
    const source = {...legacyRequest(), nextAttemptAt: 0, lastError: null};
    const raw = JSON.stringify([source], null, 2);
    const e = environment({[QUEUE]: raw, [PREFIX + source.id]: JSON.stringify({request: source,
        delivery: {state: 'acknowledged', receipt: {ok: true}}})});
    const current = e.client();
    await current.transport.flush();
    assert.equal(e.storage.get(QUEUE), raw);
    assert.equal(e.calls.length, 0);
});

test('same ID with a different original is preserved and cannot confirm the journal receipt', async () => {
    const e = environment();
    const current = e.client();
    const request = command(current, 'collision');
    const record = e.saved(request); record.delivery.state = 'held';
    e.storage.set(PREFIX + request.id, JSON.stringify(record));
    const changed = projection(e, request); changed.data = {...changed.data, truck_id: 999};
    const raw = JSON.stringify([changed], null, 2);
    e.storage.set(QUEUE, raw);
    const restarted = e.client();
    await restarted.transport.flush();
    assert.equal(e.storage.get(QUEUE), raw);
    assert.equal(e.saved(request).delivery.state, 'held');
    assert.equal(e.calls.length, 0, 'neither mutation nor guessed receipt');
});

test('projection cleanup failure preserves both copies and is retried after storage recovers', () => {
    const e = environment();
    const current = e.client();
    const request = command(current, 'quota-upgrade');
    const raw = JSON.stringify([projection(e, request)]);
    e.storage.set(QUEUE, raw);
    const original = e.storage.get(PREFIX + request.id);
    e.deny(key => key === QUEUE);
    const failed = e.client();
    assert.equal(failed.transport.getQueueState().storageError, 'storage_unavailable');
    assert.equal(e.storage.get(QUEUE), raw);
    assert.equal(e.storage.get(PREFIX + request.id), original);
    e.deny(() => false);
    e.client();
    assert.deepEqual(JSON.parse(e.storage.get(QUEUE)), []);
    assert.equal(e.storage.get(PREFIX + request.id), original);
});

test('readonly client preserves an existing projection and sends nothing', async () => {
    const e = environment();
    const request = command(e.client(), 'readonly');
    const raw = JSON.stringify([projection(e, request)]);
    e.storage.set(QUEUE, raw);
    const client = e.client({readonly: true});
    await client.transport.flush();
    assert.equal(e.storage.get(QUEUE), raw);
    assert.equal(e.calls.length, 0);
});

test('an old response can acknowledge legacy input while new input keeps its own author and shift', async () => {
    const old = legacyRequest();
    const raw = JSON.stringify([old]);
    const e = environment({[QUEUE]: raw});
    const current = e.client({fetch: async (url, init) => {
        if (url === '/assignments/commands/receipt/') return reply({ok: true, status: 'acknowledged', receipt: {ok: true},
            evidence: {actor_id: 1, shift_id: 99, client_action_id: 'old', action_type: 'dispatcher_assign_truck'}});
        return reply({ok: true});
    }});
    const request = command(current, 'new-alongside-old');
    await current.transport.flush();
    assert.equal(e.saved(old).delivery.state, 'acknowledged');
    assert.deepEqual(e.saved(old).request, old);
    assert.equal(e.saved(request).delivery.state, 'acknowledged');
    assert.deepEqual(e.saved(request).request.author, author('dispatcher'));
    assert.equal(e.storage.get(QUEUE), raw);
    assert.equal(e.calls.filter(c => c.url === '/assignments/commands/receipt/').length, 1);
    assert.equal(e.calls.filter(c => c.init.headers['X-Command-Context']).length, 1);
});

test('cleanup does not overwrite an old command appended while the journal is being inspected', () => {
    const e = environment();
    const request = command(e.client(), 'concurrent-upgrade');
    const projected = projection(e, request), appended = legacyRequest('arrived-during-read');
    e.storage.set(QUEUE, JSON.stringify([projected]));
    const changed = JSON.stringify([projected, appended]);
    e.watchReads(key => {
        if (key === PREFIX + request.id) { e.watchReads(() => {}); e.storage.set(QUEUE, changed); }
    });
    e.client();
    assert.equal(e.storage.get(QUEUE), changed, 'changed snapshot must not be replaced');
    e.client();
    assert.deepEqual(JSON.parse(e.storage.get(QUEUE)), [appended]);
});
