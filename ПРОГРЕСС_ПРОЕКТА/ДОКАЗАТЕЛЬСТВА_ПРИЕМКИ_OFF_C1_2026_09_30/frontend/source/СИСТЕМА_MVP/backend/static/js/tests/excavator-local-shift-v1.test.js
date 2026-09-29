const assert = require('node:assert/strict');
const test = require('node:test');

const createLedger = require('../excavator-local-shift-v1.js');

function clone(value) {
    return JSON.parse(JSON.stringify(value));
}

function memoryAdapter(seed) {
    let value = seed ? clone(seed) : null;
    return {
        kind: 'memory',
        read: async () => value ? clone(value) : null,
        write: async (next) => { value = clone(next); },
        inspect: () => value ? clone(value) : null,
    };
}

function fakeIndexedDatabase() {
    const rows = new Map();
    let created = false;
    return {
        open() {
            const request = {};
            queueMicrotask(() => {
                const database = {
                    objectStoreNames: {contains: () => created},
                    createObjectStore() { created = true; },
                    transaction() {
                        const tx = {
                            objectStore() {
                                return {
                                    get(key) {
                                        const read = {};
                                        queueMicrotask(() => {
                                            read.result = rows.get(key) || undefined;
                                            if (read.onsuccess) read.onsuccess();
                                        });
                                        return read;
                                    },
                                    put(row) {
                                        rows.set(row.storage_id, clone(row));
                                        queueMicrotask(() => { if (tx.oncomplete) tx.oncomplete(); });
                                    },
                                };
                            },
                        };
                        return tx;
                    },
                };
                request.result = database;
                if (!created && request.onupgradeneeded) request.onupgradeneeded();
                if (request.onsuccess) request.onsuccess();
            });
            return request;
        },
    };
}

function fakeOutbox(options = {}) {
    const pending = new Map();
    const confirmed = [];
    let fail = Boolean(options.fail);
    return {
        queue: async (event) => {
            if (fail) throw new Error('queue unavailable');
            const existing = pending.get(event.event_id);
            if (existing) assert.deepEqual(existing, event);
            pending.set(event.event_id, clone(event));
            return Object.assign(clone(event), {sync_state: 'pending'});
        },
        confirmed: async () => clone(confirmed),
        confirm(event, result) {
            pending.delete(event.event_id);
            confirmed.push({event: clone(event), result: clone(result), confirmed_at: new Date().toISOString()});
        },
        setFail(value) { fail = value; },
        events: () => Array.from(pending.values()).map(clone),
    };
}

function create(options = {}) {
    return createLedger(Object.assign({
        adapter: memoryAdapter(),
        outbox: fakeOutbox(),
        accessId: 7,
        actorId: 12,
        roleCode: 'excavator_operator',
        deviceId: 'off-c1-device',
    }, options));
}

function opening(id = 'shift-local-1', sequence = 1, at = '2026-09-29T00:05:00.000Z') {
    return {
        event_id: id,
        event_type: 'excavator.shift.opened',
        format_version: 1,
        actor_id: 12,
        access_id: 7,
        role_code: 'excavator_operator',
        device_id: 'off-c1-device',
        local_shift_id: id,
        shift_id: 0,
        equipment_id: 7,
        occurred_at: at,
        sequence,
        depends_on: [],
        payload: {local_shift_id: id, excavator_id: 7, fuel: '3500', fuel_percent: '50', engine_hours: '1200'},
    };
}

function load(open, id, sequence, at, changes = {}) {
    const localTripId = `trip-${id}`;
    return {
        event_id: id,
        event_type: changes.event_type || 'excavator.trip.loaded',
        format_version: 1,
        actor_id: 12,
        access_id: 7,
        role_code: 'excavator_operator',
        device_id: 'off-c1-device',
        local_shift_id: open.local_shift_id,
        shift_id: 0,
        equipment_id: 7,
        local_trip_id: localTripId,
        occurred_at: at,
        sequence,
        depends_on: [open.event_id],
        payload: Object.assign({
            local_shift_id: open.local_shift_id,
            truck_id: sequence,
            truck_number: String(sequence),
            dump_point_id: 4,
            dump_point_name: 'Дробилка',
            local_fleet_code: 'belaz',
            local_volume_m3: '49.40',
        }, changes.payload || {}),
    };
}

test('OFF-01: open and first load are durable before any server response', async () => {
    const ledger = create();
    const open = opening();
    const first = load(open, 'load-1', 2, '2026-09-29T00:10:00.000Z');

    await ledger.recordAndQueue(open);
    await ledger.recordAndQueue(first);

    const current = ledger.currentShift();
    assert.equal(current.status, 'open');
    assert.equal(current.local_shift_id, open.event_id);
    assert.equal(current.server_shift_id, null);
    assert.equal((await ledger.facts()).length, 1);
    const report = await ledger.hourlyReport(null, Date.parse('2026-09-29T00:15:00.000Z'));
    assert.equal(report.hours[0].totals.trip_count, 1);
    assert.equal(report.hours[0].totals.volume_m3, 49.4);
});

test('OFF-02/OFF-03: a failed or lost transport write is recovered with the exact envelope', async () => {
    const adapter = memoryAdapter();
    const unavailable = fakeOutbox({fail: true});
    const first = create({adapter, outbox: unavailable});
    const open = opening();

    await first.recordAndQueue(open);
    assert.equal(first.currentShift().status, 'open');
    assert.equal(unavailable.events().length, 0);

    const restoredOutbox = fakeOutbox();
    const restarted = create({adapter, outbox: restoredOutbox});
    await restarted.ready();

    assert.equal(restoredOutbox.events().length, 1);
    assert.deepEqual(restoredOutbox.events()[0], open);
    assert.equal(restarted.currentShift().opened_at, open.occurred_at);
});

test('OFF-02/OFF-09: restart keeps the local shift usable while delivery storage is still unavailable', async () => {
    const adapter = memoryAdapter();
    const first = create({adapter, outbox: fakeOutbox({fail: true})});
    const open = opening('shift-awaiting-delivery');
    await first.recordAndQueue(open);

    const restarted = create({adapter, outbox: fakeOutbox({fail: true})});
    await restarted.ready();

    assert.equal(restarted.currentShift().local_shift_id, open.event_id);
    const snapshot = await restarted.snapshot();
    assert.equal(snapshot.shifts[0].events[0].delivery_state, 'awaiting_outbox');
    assert.deepEqual(snapshot.shifts[0].events[0].event, open);
});

test('OFF-04/OFF-05: 35 loads, cancellation, hour boundary and more than 200 receipts do not truncate facts', async (t) => {
    const ledger = create();
    const open = opening();
    const startedAt = process.hrtime.bigint();
    await ledger.recordAndQueue(open);
    for (let index = 0; index < 235; index += 1) {
        const minute = index % 60;
        const hour = index < 35 ? (index < 20 ? '00' : '01') : '03';
        const event = load(open, `load-${index}`, index + 2, `2026-09-29T${hour}:${String(minute).padStart(2, '0')}:00.000Z`, {
            payload: {local_volume_m3: index === 2 ? '' : '49.40'},
        });
        await ledger.recordAndQueue(event);
        if (index === 0) await ledger.recordAndQueue(event);
    }
    const cancel = {
        event_id: 'cancel-1', event_type: 'excavator.trip.loaded.cancelled', format_version: 1,
        actor_id: 12, access_id: 7, role_code: 'excavator_operator', device_id: 'off-c1-device',
        local_shift_id: open.local_shift_id, shift_id: 0, equipment_id: 7,
        local_trip_id: 'trip-load-1', occurred_at: '2026-09-29T01:20:00.000Z', sequence: 500,
        depends_on: ['load-1'], payload: {local_shift_id: open.local_shift_id, source_load_event_id: 'load-1'},
    };
    await ledger.recordAndQueue(cancel);

    const facts = await ledger.facts();
    assert.equal(facts.length, 235);
    assert.equal(facts.filter((fact) => fact.cancelled).length, 1);
    const report = await ledger.hourlyReport(null, Date.parse('2026-09-29T01:30:00.000Z'));
    assert.equal(report.hours[0].totals.trip_count, 10);
    assert.equal(report.hours[1].totals.trip_count, 19);
    assert.equal(report.hours[1].unknown_volume_trip_count, 1);
    const shiftSummary = await ledger.shiftSummary(null);
    assert.equal(shiftSummary.trip_count, 234);
    assert.equal(shiftSummary.unknown_volume_trip_count, 1);
    assert.ok(Math.abs(shiftSummary.volume_m3 - (233 * 49.4)) < 0.001);
    const elapsedMs = Number(process.hrtime.bigint() - startedAt) / 1e6;
    t.diagnostic(`235 durable loads plus cancellation: ${elapsedMs.toFixed(1)} ms in Node memory adapter`);
    assert.ok(elapsedMs < 5000, `local write path took ${elapsedMs.toFixed(1)} ms`);
});

test('OFF-04/OFF-07: a controlled full shift across midnight retains separate hourly and shift totals', async () => {
    const ledger = create();
    const open = opening('night-shift', 1, '2026-09-29T09:00:00.000Z');
    await ledger.recordAndQueue(open);
    await ledger.recordAndQueue(load(open, 'before-midnight', 2, '2026-09-29T14:59:00.000Z'));
    await ledger.recordAndQueue(load(open, 'after-midnight', 3, '2026-09-29T15:01:00.000Z'));
    await ledger.recordAndQueue({
        event_id: 'night-shift-close', event_type: 'excavator.shift.closed', format_version: 1,
        actor_id: 12, access_id: 7, role_code: 'excavator_operator', device_id: 'off-c1-device',
        local_shift_id: open.local_shift_id, shift_id: 0, equipment_id: 7,
        occurred_at: '2026-09-29T21:00:00.000Z', sequence: 4,
        depends_on: ['after-midnight'],
        payload: {local_shift_id: open.local_shift_id, fuel: '3300', engine_hours: '1212'},
    });

    const restarted = create({adapter: memoryAdapter(await ledger.snapshot()), outbox: fakeOutbox()});
    await restarted.ready();
    const summary = await restarted.shiftSummary(null, open.local_shift_id);
    const report = await restarted.hourlyReport(null, Date.parse('2026-09-29T15:30:00.000Z'), open.local_shift_id);

    assert.equal(summary.trip_count, 2);
    assert.equal(summary.volume_m3, 98.8);
    assert.equal(report.hours[0].totals.trip_count, 1);
    assert.equal(report.hours[1].totals.trip_count, 1);
    assert.equal((await restarted.snapshot()).shifts[0].status, 'closed');
});

test('OFF-06: a free-bucket load remains a fact independently of card TTL', async () => {
    const ledger = create();
    const open = opening();
    await ledger.recordAndQueue(open);
    const first = load(open, 'free-load-1', 2, '2026-09-29T00:10:00.000Z', {event_type: 'excavator.free_bucket.loaded'});
    await ledger.recordAndQueue(first);

    const restarted = create({adapter: memoryAdapter(await ledger.snapshot()), outbox: fakeOutbox()});
    await restarted.ready();

    assert.equal((await restarted.facts()).length, 1);
    assert.equal((await restarted.facts())[0].event_id, first.event_id);
});

test('OFF-07: a pending close does not mix two consecutive local shifts', async () => {
    const ledger = create();
    const first = opening('shift-a', 1, '2026-09-29T00:00:00.000Z');
    await ledger.recordAndQueue(first);
    await ledger.recordAndQueue(load(first, 'load-a', 2, '2026-09-29T00:10:00.000Z'));
    await ledger.recordAndQueue({
        event_id: 'close-a', event_type: 'excavator.shift.closed', format_version: 1,
        actor_id: 12, access_id: 7, role_code: 'excavator_operator', device_id: 'off-c1-device',
        local_shift_id: 'shift-a', shift_id: 0, equipment_id: 7, occurred_at: '2026-09-29T01:00:00.000Z',
        sequence: 3, depends_on: ['load-a'], payload: {local_shift_id: 'shift-a', fuel: '3400', engine_hours: '1201'},
    });
    const second = opening('shift-b', 4, '2026-09-29T01:01:00.000Z');
    await ledger.recordAndQueue(second);
    await ledger.recordAndQueue(load(second, 'load-b', 5, '2026-09-29T01:10:00.000Z'));

    const snapshot = await ledger.snapshot();
    assert.equal(snapshot.shifts.length, 2);
    assert.equal(snapshot.shifts[0].status, 'closed');
    assert.equal(snapshot.shifts[0].events.filter((entry) => entry.event.event_type.includes('.loaded')).length, 1);
    assert.equal(snapshot.shifts[1].status, 'open');
    assert.equal((await ledger.facts('shift-b')).length, 1);
});

test('OFF-08: receipt mapping does not remove facts and covering server projection does not double count', async () => {
    const outbox = fakeOutbox();
    const ledger = create({outbox});
    const open = opening();
    const fact = load(open, 'load-covered', 2, '2026-09-29T00:10:00.000Z');
    await ledger.recordAndQueue(open);
    await ledger.recordAndQueue(fact);
    await ledger.confirm(open, {server_ids: {shift_id: 81}, effective_occurred_at: open.occurred_at});
    await ledger.confirm(fact, {server_ids: {trip_id: 901, shift_id: 81}});

    const server = await ledger.hourlyReport(null, Date.parse('2026-09-29T00:15:00.000Z'));
    server.hours[0].rows = [{dump_point_id: 4, dump_point: 'Дробилка', belaz: 1, nhl: 0}];
    server.hours[0].totals = {belaz: 1, nhl: 0, trip_count: 1, volume_m3: '49.40'};
    server.hours[0].source_trip_count = 1;
    server.hours[0].source_trip_ids = [901];
    server.hours[0].is_empty = false;
    const merged = await ledger.hourlyReport(server, Date.parse('2026-09-29T00:15:00.000Z'));
    const shiftSummary = await ledger.shiftSummary({
        trip_count: 1,
        volume_m3: '49.40',
        source_trip_ids: [901],
    });

    assert.equal(ledger.currentShift().server_shift_id, 81);
    assert.equal((await ledger.facts()).length, 1);
    assert.equal(merged.hours[0].totals.trip_count, 1);
    assert.equal(merged.hours[0].totals.volume_m3, '49.40');
    assert.equal(shiftSummary.trip_count, 1);
    assert.equal(shiftSummary.volume_m3, 49.4);
});

test('OFF-09: IndexedDB failure falls back, both storages unavailable reject without false success', async () => {
    const values = new Map();
    const storage = {
        getItem: (key) => values.has(key) ? values.get(key) : null,
        setItem: (key, value) => values.set(key, String(value)),
    };
    const brokenIndexedDB = {open() { throw new Error('idb blocked'); }};
    const fallback = createLedger({
        indexedDB: brokenIndexedDB, localStorage: storage, outbox: fakeOutbox(),
        accessId: 7, actorId: 12, roleCode: 'excavator_operator', deviceId: 'off-c1-device',
    });
    await fallback.ready();
    assert.equal(await fallback.storageKind(), 'localStorage');
    await fallback.recordAndQueue(opening());

    const unavailable = createLedger({
        indexedDB: brokenIndexedDB,
        localStorage: {getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); }},
        outbox: fakeOutbox(), accessId: 8, actorId: 12, roleCode: 'excavator_operator', deviceId: 'off-c1-device',
    });
    await assert.rejects(unavailable.ready(), /хранилище недоступно/i);
});

test('OFF-09: successful IndexedDB writes are mirrored for a later fallback-only restart', async () => {
    const values = new Map();
    const storage = {
        getItem: (key) => values.has(key) ? values.get(key) : null,
        setItem: (key, value) => values.set(key, String(value)),
    };
    const first = createLedger({
        indexedDB: fakeIndexedDatabase(), localStorage: storage, outbox: fakeOutbox(),
        accessId: 7, actorId: 12, roleCode: 'excavator_operator', deviceId: 'off-c1-device',
    });
    await first.ready();
    await first.recordAndQueue(opening());
    assert.equal(await first.storageKind(), 'indexedDB+localStorage-fallback');

    const restarted = createLedger({
        indexedDB: {open() { throw new Error('idb unavailable after restart'); }},
        localStorage: storage, outbox: fakeOutbox(),
        accessId: 7, actorId: 12, roleCode: 'excavator_operator', deviceId: 'off-c1-device',
    });
    await restarted.ready();

    assert.equal(await restarted.storageKind(), 'localStorage');
    assert.equal(restarted.currentShift().local_shift_id, 'shift-local-1');
});

test('OFF-10: another identity cannot open the stored ledger', async () => {
    const adapter = memoryAdapter();
    const first = create({adapter});
    await first.recordAndQueue(opening());
    const other = createLedger({
        adapter, outbox: fakeOutbox(), accessId: 8, actorId: 99,
        roleCode: 'excavator_operator', deviceId: 'other-device',
    });
    await other.ready();
    assert.equal(other.currentShift(), null);
    assert.equal((await other.snapshot()).shifts.length, 0);
});

test('OFF-08: a stale server shift cannot replace a newer unmapped local shift', async () => {
    const adapter = memoryAdapter();
    const outbox = fakeOutbox();
    const ledger = create({adapter, outbox});
    const open = opening('local-shift-newer');

    await ledger.recordAndQueue(open);
    await ledger.ensureImportedServerShift({
        id: 998,
        equipment_id: open.equipment_id,
        opened_at: '2026-09-28T20:00:00.000Z',
    });

    const current = ledger.currentShift();
    assert.equal(current.local_shift_id, open.event_id);
    assert.equal(current.server_shift_id, null);
    assert.equal((await ledger.snapshot()).shifts.length, 1);
});
