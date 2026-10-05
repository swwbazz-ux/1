const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const archiveModule = require('../driver-shift-archive-v1.js');
const {createDriverOfflineOutbox, localRepository} = require('../driver-offline-outbox-v2.js');
const {copy, identity, at, storage, raw, fixture, responder} = require("./helpers/driver-archive-fixture.js");
function client(data, extra = {}) {
    return archiveModule.create({identity, storage: data.store, outbox: {journal: async () => copy(data.events)},
        fetch: responder(data.proof), url: 'https://example.test/offline-events/driver-shift-archive/', ...extra});
}
function saved(data) { return [...data.store.values.values()].map(value => JSON.parse(value)); }

test('235 sources are fully paginated and proof survives restart without altering journal or ACKs', async () => {
    const data = fixture(235, true), original = copy(data.events), offsets = [];
    const worker = client(data, {fetch: responder(data.proof, (url, options) => {
        offsets.push(Number(url.searchParams.get('offset')));
        assert.equal(options.cache, 'no-store');
        assert.equal(url.searchParams.get('device_id'), identity.device_id);
        if (offsets.length > 1) assert.equal(url.searchParams.get('snapshot_id'), data.proof.snapshot_id);
    })});
    const first = worker.refresh();
    assert.equal(first, worker.refresh());
    const covered = await first;
    assert.ok(covered);
    assert.deepEqual(offsets, [0, 100, 200]);
    assert.equal(covered.manifest.length, 243);
    assert.equal(covered.trip_facts.length, 2);
    assert.equal(covered.downtime_facts.length, 1);
    assert.deepEqual(data.events, original);
    assert.deepEqual(saved(data), [covered]);
    const restarted = client(data, {fetch: async () => { throw Error('proof should already cover sources'); }});
    assert.equal(await restarted.refresh(), false);
    assert.deepEqual(data.events, original);
});

test('ACK alone, incomplete pages and a locally cancelled pair never create coverage', async () => {
    const data = fixture(235);
    assert.equal(data.store.values.size, 0);
    const response = responder(data.proof);
    const missing = client(data, {fetch: async (...args) => {
        const body = await (await response(...args)).json(); body.next_offset = null;
        return {ok: true, json: async () => body};
    }});
    assert.equal(await missing.refresh(), false);
    assert.equal(data.store.values.size, 0);
    data.events[1].state = 'cancelled_locally';
    assert.equal(await client(data).refresh(), false);
    assert.equal(data.store.values.size, 0);
});

test('server-opened and mixed local/numeric shifts are covered, including a lost opening ACK', async () => {
    for (const serverOpened of [false, true]) {
        const data = fixture(4, true);
        if (serverOpened) {
            data.events.shift(); data.proof.entries.shift(); data.proof.event_count--;
            data.proof.projection.source_event_ids.shift();
            data.proof.shift.local_shift_id = data.proof.shift.open_event_id = '';
            for (const event of [...data.events, ...data.proof.entries.map(entry => entry.event)]) {
                event.shift_id = 99; event.local_shift_id = null; event.payload = {};
            }
        } else {
            data.events[0].state = 'pending'; delete data.events[0].server_result;
            const close = data.events.at(-1), original = data.proof.entries.at(-1).event;
            for (const event of [close, original]) { event.shift_id = 99; event.local_shift_id = null; event.payload = {}; }
        }
        assert.ok(await client(data).refresh());
        assert.equal(saved(data)[0].event_count, data.events.length);
    }
});

test('foreign, changed or inconsistent originals, receipts, domain facts and totals are rejected', () => {
    const changes = [p => { p.identity.actor_id++; }, p => { p.identity.access_id++; }, p => { p.identity.device_id = 'other'; },
        p => { p.entries[1].event.sequence++; }, p => { p.entries[1].event.payload.changed = true; },
        p => { p.entries[1].event.context_snapshot.changed = true; }, p => { p.entries.pop(); },
        p => { p.entries[1] = p.entries[0]; }, p => { p.entries[1].status = 'retry'; },
        p => { p.entries[1].receipt_id++; }, p => { p.entries[1].fingerprint = 'bad'; },
        p => { p.shift.server_shift_id++; }, p => { p.shift.equipment_id++; }, p => { p.shift.close_event_id = 'other'; },
        p => { p.entries[2].trip_fact = null; }, p => { p.entries[3].trip_fact.volume_m3 = '99'; },
        p => { p.entries[2].result.server_ids.trip_id++; }, p => { p.entries[6].downtime_fact.shift_seconds++; },
        p => { p.projection.credited_volume_m3 = '999'; }, p => { p.projection.credited_trip_count++; },
        p => { p.projection.source_event_ids = []; }, p => { p.projection.source_trip_ids = []; },
        p => { p.projection.downtime_seconds++; }, p => { p.snapshot_id = ''; }];
    for (const change of changes) {
        const data = fixture(1, true), candidate = archiveModule.candidates(data.events, identity)[0];
        change(data.proof);
        assert.throws(() => archiveModule.verify(data.proof, data.events, candidate));
        assert.equal(data.store.values.size, 0);
    }
});

test('a carrying trip has one completion but no second credit in the unloading shift', async () => {
    const data = fixture(1, true);
    for (const entry of data.proof.entries) if (entry.trip_fact?.trip_id === 101) {
        Object.assign(entry.trip_fact, {is_carryover: true, driver_control_shift_id: 98, credited_shift_id: 98});
    }
    data.proof.projection.credited_trip_count = 0;
    data.proof.projection.credited_volume_m3 = '0';
    const result = await client(data).refresh();
    assert.ok(result);
    assert.equal(result.projection.completed_trip_count, 1);
    assert.equal(result.projection.credited_trip_count, 0);
});

test('server microseconds do not round a sub-second downtime up to an extra second', async () => {
    const data = fixture(1, true);
    for (const entry of data.proof.entries) if (entry.downtime_fact) Object.assign(entry.downtime_fact, {
        started_at: '2026-10-05T01:00:00.000900+00:00', ended_at: '2026-10-05T01:00:01.000100+00:00', shift_seconds: 0,
    });
    data.proof.projection.downtime_seconds = 0;
    const result = await client(data).refresh();
    assert.ok(result);
    assert.equal(result.projection.downtime_seconds, 0);
});

test('quota leaves originals untouched and a later successful pass recovers the proof', async () => {
    const data = fixture(), original = copy(data.events), write = data.store.setItem;
    data.store.setItem = () => { throw Error('quota'); };
    assert.equal(await client(data).refresh(), false);
    assert.deepEqual(data.events, original);
    assert.equal(data.store.values.size, 0);
    data.store.setItem = write;
    assert.ok(await client(data).refresh());
});

test('new facts in the old shift prevent stale coverage while another shift remains independent', async () => {
    for (const sameShift of [true, false]) {
        const data = fixture(), response = responder(data.proof);
        const result = await client(data, {fetch: async (...args) => {
            data.events.push({...raw('later', 100), local_shift_id: sameShift ? 'open' : 'next',
                payload: {local_shift_id: sameShift ? 'open' : 'next'}});
            return response(...args);
        }}).refresh();
        assert.equal(Boolean(result), !sameShift);
        assert.equal(data.events.at(-1).event_id, 'later');
    }
});

test('altered stored proof and a changed source both force a new server pass', async () => {
    const data = fixture(); let calls = 0;
    const worker = client(data, {fetch: responder(data.proof, () => calls++)});
    assert.ok(await worker.refresh());
    const [key, rawProof] = [...data.store.values.entries()][0];
    const altered = JSON.parse(rawProof); altered.projection.credited_trip_count = 99;
    data.store.setItem(key, JSON.stringify(altered));
    assert.ok(await worker.refresh());
    assert.equal(calls, 2);
    data.events[1].payload.changed = true;
    assert.equal(await worker.refresh(), false);
    assert.equal(calls, 3);
    assert.equal(saved(data)[0].projection.credited_trip_count, 0);
});

test('hung headers, body and journal have a deadline; late completion cannot save proof', async () => {
    for (const stage of ['headers', 'body', 'journal']) {
        const data = fixture(); let release;
        const hung = new Promise(resolve => { release = resolve; });
        const body = {...data.proof, offset: 0, next_offset: null};
        const extra = {timeoutMs: 20};
        if (stage === 'headers') extra.fetch = () => hung;
        if (stage === 'body') extra.fetch = async () => ({ok: true, json: () => hung});
        if (stage === 'journal') extra.outbox = {journal: () => hung};
        assert.equal(await client(data, extra).refresh(), false);
        release(stage === 'body' ? body : stage === 'journal' ? data.events : {ok: true, json: async () => body});
        await new Promise(resolve => setImmediate(resolve));
        assert.equal(data.store.values.size, 0);
    }
});

test('session changes and snapshot changes between pages never save another user’s proof', async () => {
    for (const changedSession of [true, false]) {
        const data = fixture(235), response = responder(data.proof); let current = true, calls = 0;
        const worker = client(data, {isCurrent: () => current, fetch: async (...args) => {
            const body = await (await response(...args)).json();
            if (++calls === 2) {
                if (changedSession) current = false; else body.snapshot_id = 'b'.repeat(64);
            }
            return {ok: true, json: async () => body};
        }});
        assert.equal(await worker.refresh(), false);
        assert.equal(data.store.values.size, 0);
    }
});

test('real outbox journals retain pending and confirmed originals alongside the coverage sidecar', async () => {
    const store = storage(), repo = localRepository(store, 7), fixtureData = fixture(0);
    const context = {actorId: 12, accessId: 7, deviceId: identity.device_id, equipmentId: 9};
    const box = createDriverOfflineOutbox({accessId: 7, repository: repo, context, send: async batch => ({
        results: batch.events.map((event, index) => ({event_id: event.event_id, status: 'accepted',
            server_ids: {shift_id: 99, event_receipt_id: index + 1}})),
    })});
    await box.enqueue({...raw('open', 1, 'driver.shift.opened'), payload: {local_shift_id: 'open', truck_id: 9,
        start_fuel: '100', start_mileage: '12000', start_engine_hours: '3000'}});
    context.localShiftId = 'open';
    await box.enqueue(raw('close', 2, 'driver.shift.closed'));
    await box.flush();
    fixtureData.events = await box.journal();
    fixtureData.proof.entries = fixtureData.events.map((event, index) => {
        const original = copy(event);
        for (const key of ['state', 'attempt_count', 'next_retry_at', 'last_error', 'updated_at', 'created_session', 'created_mono', 'server_result']) delete original[key];
        return {...fixtureData.proof.entries[index], event: original, result: event.server_result};
    });
    const before = await box.journal();
    assert.ok(await client(fixtureData, {outbox: box, storage: store}).refresh());
    assert.deepEqual(await box.journal(), before);
    assert.equal((await box.pending()).length, 0);
    context.localShiftId = 'next';
    await box.enqueue({...raw('next', 3, 'driver.shift.opened'), local_shift_id: 'next', payload: {local_shift_id: 'next', truck_id: 9}});
    assert.equal((await box.journal()).length, 3);
});

test('background failure backs off and an already covered archive stops requesting pages', async () => {
    const data = fixture(), timers = new Map(); let timerId = 0, calls = 0;
    const context = vm.createContext({module: {exports: {}}, URL, TextEncoder, crypto: globalThis.crypto, AbortController,
        setTimeout: (callback, delay) => { const id = ++timerId; timers.set(id, {callback, delay}); return id; }, clearTimeout: id => timers.delete(id)});
    vm.runInContext(fs.readFileSync(require.resolve('../driver-shift-archive-v1.js'), 'utf8'), context);
    const response = responder(data.proof);
    const worker = context.module.exports.create({identity, storage: data.store, outbox: {journal: async () => copy(data.events)},
        url: 'https://example.test/archive', fetch: async (...args) => ++calls === 1 ? {ok: false} : response(...args)});
    async function tick(delay) {
        const [id, timer] = [...timers.entries()].find(([, value]) => value.delay === delay) || [];
        assert.ok(timer, 'missing timer ' + delay);
        timers.delete(id); timer.callback(); await worker.refresh();
    }
    worker.schedule();
    await tick(1000); assert.equal(calls, 1);
    await tick(5000); assert.equal(calls, 2); assert.equal(data.store.values.size, 1);
    await tick(1000); assert.equal(timers.size, 0); assert.equal(calls, 2);
    worker.stop();
});
