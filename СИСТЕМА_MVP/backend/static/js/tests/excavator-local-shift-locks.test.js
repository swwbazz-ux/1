const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require.resolve('../excavator-local-shift-v1.js'), 'utf8');
const copy = value => JSON.parse(JSON.stringify(value));

function lockManager() {
    const held = new Set();
    return {
        request(name, options, callback) {
            assert.equal(options.mode, 'exclusive');
            assert.equal(options.ifAvailable, true);
            if (held.has(name)) return Promise.resolve().then(() => callback(null));
            held.add(name);
            return Promise.resolve().then(() => callback({name})).finally(() => held.delete(name));
        },
        held,
    };
}
function storage() {
    let value = null;
    return {read: async () => value && copy(value), write: async next => { value = copy(next); }};
}
function windowLedger(adapter, locks, extra = {}) {
    const context = vm.createContext({navigator: {locks}, module: {exports: {}}});
    vm.runInContext(source, context);
    return context.module.exports({adapter, accessId: 7, actorId: 12, deviceId: 'phone', ...extra});
}
function event(id, sequence, type = 'excavator.shift.opened') {
    return {event_id: id, local_shift_id: 'open', event_type: type, sequence,
        actor_id: 12, access_id: 7, role_code: 'excavator_operator', device_id: 'phone',
        occurred_at: '2026-10-05T00:00:00.000Z', equipment_id: 7,
        payload: {local_shift_id: 'open', excavator_id: 7, engine_hours: '1200'}};
}
const ids = async ledger => Array.from(await ledger.events(), e => e.event_id);

async function pair() {
    const adapter = storage();
    const locks = lockManager();
    const a = windowLedger(adapter, locks);
    const b = windowLedger(adapter, locks);
    await a.recordAndQueue(event('open', 1));
    await b.ready();
    return {adapter, locks, a, b};
}

test('two independent windows reload the latest journal before writing', async () => {
    const {a, b, adapter} = await pair();
    await a.recordAndQueue(event('load-a', 2, 'excavator.trip.loaded'));
    await b.recordAndQueue(event('load-b', 3, 'excavator.trip.loaded'));
    assert.deepEqual((await adapter.read()).shifts[0].events.map(e => e.event.event_id), ['open', 'load-a', 'load-b']);
});

test('a stale window cannot add work after another window closed the shift', async () => {
    const {a, b, adapter} = await pair();
    await a.recordAndQueue(event('close', 2, 'excavator.shift.closed'));
    await assert.rejects(b.recordAndQueue(event('late-load', 3, 'excavator.trip.loaded')), /закрыта/);
    assert.equal((await adapter.read()).shifts[0].status, 'closed');
});

test('a late receipt preserves another window’s new events', async () => {
    const {a, b, adapter} = await pair();
    await a.recordAndQueue(event('load-a', 2, 'excavator.trip.loaded'));
    await b.confirm(event('open', 1), {server_ids: {shift_id: 42}});
    const state = await adapter.read();
    assert.equal(state.shifts[0].server_shift_id, 42);
    assert.equal(state.shifts[0].events.length, 2);
});

test('busy writer rejects promptly, keeps its lock through commit, and allows a safe retry', async () => {
    const {a, b, adapter, locks} = await pair();
    const write = adapter.write;
    let entered;
    let release;
    const started = new Promise(resolve => { entered = resolve; });
    adapter.write = next => new Promise(resolve => { release = () => write(next).then(resolve); entered(); });
    const pending = a.recordAndQueue(event('load-a', 2, 'excavator.trip.loaded'));
    await started;
    try {
        assert.equal(locks.held.size, 1);
        await assert.rejects(b.recordAndQueue(event('load-b', 3, 'excavator.trip.loaded')), e => e.code === 'local_shift_busy');
        assert.equal((await adapter.read()).shifts[0].events.length, 1);
    } finally { await release(); await pending; adapter.write = write; }
    await b.recordAndQueue(event('load-b', 3, 'excavator.trip.loaded'));
    assert.equal((await adapter.read()).shifts[0].events.length, 3);
    assert.equal(locks.held.size, 0);
});

test('a failed commit releases the browser lock without deleting the last committed event', async () => {
    const {a, b, adapter, locks} = await pair();
    const write = adapter.write;
    adapter.write = async () => { throw new Error('quota'); };
    await assert.rejects(a.recordAndQueue(event('failed', 2, 'excavator.trip.loaded')), /quota/);
    assert.equal(locks.held.size, 0);
    adapter.write = write;
    await b.recordAndQueue(event('saved', 2, 'excavator.trip.loaded'));
    assert.deepEqual((await adapter.read()).shifts[0].events.map(e => e.event.event_id), ['open', 'saved']);
});

test('without cross-window locks the ledger stays readable but cannot report a successful write', async () => {
    const {adapter} = await pair();
    const queued = [];
    const unlocked = windowLedger(adapter, undefined, {outbox: {queue: async e => { queued.push(e.event_id); }}});
    await unlocked.ready();
    await assert.rejects(unlocked.recordAndQueue(event('unsafe', 2, 'excavator.trip.loaded')), e => e.code === 'local_shift_lock_unavailable');
    assert.deepEqual(await ids(unlocked), ['open']);
    assert.equal((await adapter.read()).shifts[0].events.length, 1);
    assert.ok(!queued.includes('unsafe'));
    // Startup may replay the already saved opening; no unsafe event may be saved.
});

test('explicit refresh updates the stale projection without creating a revision', async () => {
    const {a, b, adapter} = await pair();
    await a.recordAndQueue(event('close', 2, 'excavator.shift.closed'));
    const before = await adapter.read();
    await b.refresh();
    assert.equal(b.currentShift().status, 'closed');
    assert.deepEqual(await adapter.read(), before);
});

test('storage is revalidated under the lock before overwriting it', async () => {
    const {a, adapter} = await pair();
    const foreign = await adapter.read();
    foreign.identity.actor_id = 999;
    await adapter.write(foreign);
    await assert.rejects(a.recordAndQueue(event('wrong-owner', 2, 'excavator.trip.loaded')), /другому доступу/);
    assert.deepEqual(await adapter.read(), foreign);
});
