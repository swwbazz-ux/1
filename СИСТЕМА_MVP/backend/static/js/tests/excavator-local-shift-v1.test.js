const assert = require('node:assert/strict');
const test = require('node:test');

const createLedgerModule = require('../excavator-local-shift-v1.js');
// These tests isolate one ledger per device; cross-window behavior is exercised
// separately with independent VM contexts and a shared lock manager.
function createLedger(options) {
    const held = new Set();
    const locks = {request(name, config, callback) {
        if (held.has(name)) return Promise.resolve().then(() => callback(null));
        held.add(name);
        return Promise.resolve().then(() => callback({name})).finally(() => held.delete(name));
    }};
    return createLedgerModule({locks, ...options});
}

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

test('OFF-09: missing IndexedDB supports localStorage, inaccessible replicas reject without false success', async () => {
    const values = new Map();
    const storage = {
        getItem: (key) => values.has(key) ? values.get(key) : null,
        setItem: (key, value) => values.set(key, String(value)),
    };
    const brokenIndexedDB = {open() { throw new Error('idb blocked'); }};
    const fallback = createLedger({
        localStorage: storage, outbox: fakeOutbox(),
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

test('OFF-09: an inaccessible replica blocks restart until both copies can be compared', async () => {
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
    await assert.rejects(restarted.ready(), /хранилище недоступно/i);
    assert.equal(restarted.currentShift(), null);
    assert.equal(JSON.parse([...values.values()][0]).shifts[0].local_shift_id, 'shift-local-1');
});

test('OFF-10: another identity cannot open the stored ledger', async () => {
    const adapter = memoryAdapter();
    const first = create({adapter});
    await first.recordAndQueue(opening());
    const other = createLedger({
        adapter, outbox: fakeOutbox(), accessId: 8, actorId: 99,
        roleCode: 'excavator_operator', deviceId: 'other-device',
    });
    await assert.rejects(other.ready(), /другому доступу/);
    assert.equal(other.currentShift(), null);
    assert.equal(adapter.inspect().shifts.length, 1);
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

test('OFF-C1-R1 C2: a stale cached hour is rebased and includes the next durable load', async () => {
    const ledger = create();
    const open = opening('rebased-shift', 1, '2026-09-29T00:00:00.000Z');
    await ledger.recordAndQueue(open);
    const cached = await ledger.hourlyReport(null, Date.parse('2026-09-29T00:05:00.000Z'));
    await ledger.recordAndQueue(load(open, 'rebased-load', 2, '2026-09-29T00:06:00.000Z'));

    const report = await ledger.hourlyReport(cached, Date.parse('2026-09-29T00:15:00.000Z'));

    assert.equal(report.hours[0].period.start, '2026-09-29T00:00:00.000Z');
    assert.equal(report.hours[0].totals.trip_count, 1);
    assert.equal(report.hours[0].totals.volume_m3, 49.4);
});

test('OFF-C1-R1 C2: server source event evidence prevents a lost receipt from doubling a fact', async () => {
    const ledger = create();
    const open = opening('lost-receipt-shift', 1, '2026-09-29T00:00:00.000Z');
    const fact = load(open, 'lost-receipt-load', 2, '2026-09-29T00:06:00.000Z');
    await ledger.recordAndQueue(open);
    await ledger.recordAndQueue(fact);
    const server = await ledger.hourlyReport(null, Date.parse('2026-09-29T00:15:00.000Z'));
    server.hours[0].rows = [{dump_point_id: 4, dump_point: 'Дробилка', belaz: 1, nhl: 0}];
    server.hours[0].totals = {belaz: 1, nhl: 0, trip_count: 1, volume_m3: '49.40'};
    server.hours[0].source_trip_count = 1;
    server.hours[0].source_trip_ids = [901];
    server.hours[0].source_event_ids = [fact.event_id];
    server.hours[0].is_empty = false;

    const hourly = await ledger.hourlyReport(server, Date.parse('2026-09-29T00:15:00.000Z'));
    const summary = await ledger.shiftSummary({
        trip_count: 1,
        volume_m3: '49.40',
        source_trip_ids: [901],
        source_event_ids: [fact.event_id],
    });

    assert.equal(hourly.hours[0].totals.trip_count, 1);
    assert.equal(summary.trip_count, 1);
    assert.equal((await ledger.facts()).length, 1);
});

test('OFF-C1-R1 C4: a failed durable write rolls back memory and transport failure keeps ledger usable', async () => {
    const failingAdapter = {
        kind: 'failing',
        read: async () => null,
        write: async () => { throw new Error('quota exceeded'); },
    };
    const unsaved = create({adapter: failingAdapter});
    await unsaved.ready();
    await assert.rejects(unsaved.recordAndQueue(opening('unsaved-open')), /quota exceeded/);
    assert.equal(unsaved.currentShift(), null);

    const adapter = memoryAdapter();
    const first = create({adapter, outbox: fakeOutbox({fail: true})});
    await first.recordAndQueue(opening('transport-independent-open'));
    const restarted = create({
        adapter,
        outbox: {
            confirmed: async () => { throw new Error('transport receipt db unavailable'); },
            queue: async () => { throw new Error('transport queue db unavailable'); },
        },
    });
    await restarted.ready();
    assert.equal(restarted.currentShift().local_shift_id, 'transport-independent-open');
});

test('OFF-C1-R1 C8: face context is durable, restart-safe and remains bound to its shift', async () => {
    const adapter = memoryAdapter();
    const first = create({adapter});
    const open = opening('face-context-shift');
    const context = {
        event_id: 'face-context-event', event_type: 'excavator.work_context.changed', format_version: 1,
        actor_id: 12, access_id: 7, role_code: 'excavator_operator', device_id: 'off-c1-device',
        local_shift_id: open.local_shift_id, shift_id: 0, equipment_id: 7,
        occurred_at: '2026-09-29T00:06:00.000Z', sequence: 2, depends_on: [open.event_id],
        payload: {
            local_shift_id: open.local_shift_id,
            rock_type_id: 9,
            dump_point_ids: [4, 5],
            loading_horizon: '777',
            loading_block: '8',
        },
    };
    await first.recordAndQueue(open);
    await first.recordAndQueue(context);

    const restarted = create({adapter, outbox: fakeOutbox()});
    await restarted.ready();
    assert.deepEqual(await restarted.workContext(open.local_shift_id), context.payload);
    assert.deepEqual((await restarted.events()).map((event) => event.event_id), [open.event_id, context.event_id]);
    assert.equal(await restarted.nextSequence(), 3);
});

async function promptly(promise) {
    let timer;
    try {
        return await Promise.race([promise, new Promise((_, reject) => {
            timer = setTimeout(() => reject(new Error('local operation waited for transport')), 100);
        })]);
    } finally { clearTimeout(timer); }
}

test('durable save is not undone by a failing UI notification', async () => {
    const adapter = memoryAdapter();
    const ledger = create({adapter, onChange() { throw new Error('detached screen'); }});
    await ledger.recordAndQueue(opening());
    assert.equal(ledger.currentShift().local_shift_id, opening().event_id);
    assert.equal(adapter.inspect().shifts.length, 1);
});

test('changed duplicate opening is rejected and a closed opening cannot reopen the shift', async () => {
    const ledger = create();
    const open = opening();
    await ledger.recordAndQueue(open);
    await assert.rejects(ledger.recordAndQueue({...open, payload: {...open.payload, engine_hours: '999'}}), /ID/);
    await ledger.recordAndQueue({...open, event_id: 'close-1', event_type: 'excavator.shift.closed', sequence: 2});
    await ledger.recordAndQueue(open);
    assert.equal(ledger.currentShift().status, 'closed');
    assert.equal((await ledger.events()).length, 2);
});

test('failed server import leaves no in-memory shift', async () => {
    const ledger = create({adapter: {read: async () => null, write: async () => { throw new Error('quota'); }}});
    await assert.rejects(ledger.ensureImportedServerShift({id: 44, equipment_id: 7}), /quota/);
    assert.equal(ledger.currentShift(), null);
    assert.equal((await ledger.snapshot()).shifts.length, 0);
});

test('uncommitted opening is not visible while durable write is pending', async () => {
    let finish;
    const ledger = create({adapter: {read: async () => null, write: () => new Promise(resolve => { finish = resolve; })}});
    await ledger.ready();
    const saved = ledger.recordAndQueue(opening());
    for (let i = 0; i < 50 && !finish; i++) await Promise.resolve();
    assert.equal(typeof finish, 'function');
    assert.equal(ledger.currentShift(), null);
    finish();
    await promptly(saved);
    assert.equal(ledger.currentShift().status, 'open');
});

test('durable opening returns even when transport queue never settles', async () => {
    const ledger = create({outbox: {confirmed: async () => [], queue: () => new Promise(() => {})}});
    await promptly(ledger.recordAndQueue(opening()));
    assert.equal(ledger.currentShift().status, 'open');
});

test('restart returns even when transport receipts never settle', async () => {
    const adapter = memoryAdapter();
    await create({adapter}).recordAndQueue(opening());
    const restarted = create({adapter, outbox: {confirmed: () => new Promise(() => {}), queue: async e => e}});
    await promptly(restarted.ready());
    assert.equal(restarted.currentShift().status, 'open');
});

test('failed opening followed by another save cannot erase the successful event', async () => {
    let writes = 0;
    let stored = null;
    const ledger = create({adapter: {
        read: async () => stored,
        write: async value => {
            if (++writes === 1) throw new Error('first write failed');
            stored = clone(value);
        },
    }});
    const first = ledger.recordAndQueue(opening('failed-open'));
    const second = ledger.recordAndQueue(opening('saved-open'));
    await assert.rejects(first, /first write failed/);
    await second;
    assert.equal(ledger.currentShift().local_shift_id, 'saved-open');
    assert.deepEqual((await ledger.events()).map(e => e.event_id), ['saved-open']);
    assert.deepEqual(stored.shifts.map(s => s.local_shift_id), ['saved-open']);
});

test('failed confirmation preserves the pending original and can be retried', async () => {
    let fail = false;
    const storage = memoryAdapter();
    const ledger = create({adapter: {
        read: storage.read,
        write: async value => { if (fail) throw new Error('quota'); await storage.write(value); },
    }});
    const open = opening();
    await ledger.recordAndQueue(open);
    fail = true;
    await assert.rejects(ledger.confirm(open, {server_ids: {shift_id: 71}}), /quota/);
    assert.equal(ledger.currentShift().server_shift_id, null);
    assert.equal((await ledger.snapshot()).shifts[0].events[0].delivery_state, 'awaiting_outbox');
    fail = false;
    await ledger.confirm(open, {server_ids: {shift_id: 71}});
    assert.equal(ledger.currentShift().server_shift_id, 71);
    assert.deepEqual(await ledger.getEvent(open.event_id), open);
});

test('caller mutation cannot change the envelope waiting for storage', async () => {
    const ledger = create();
    const open = opening();
    const original = clone(open);
    const save = ledger.recordAndQueue(open);
    open.payload.engine_hours = '999';
    open.event_id = 'changed-after-submit';
    await save;
    assert.deepEqual(await ledger.getEvent(original.event_id), original);
});

test('another identity and reused sequence cannot contaminate a saved shift', async () => {
    const ledger = create();
    const open = opening();
    await ledger.recordAndQueue(open);
    const next = load(open, 'bad-load', 2, '2026-09-29T00:10:00.000Z');
    await assert.rejects(ledger.recordAndQueue({...next, actor_id: 999}), /доступу/);
    await assert.rejects(ledger.recordAndQueue({...next, sequence: 1}), /Порядок/);
    assert.equal(await ledger.confirm({...open, payload: {...open.payload, fuel: '100'}}, {server_ids: {shift_id: 99}}), false);
    assert.equal(ledger.currentShift().server_shift_id, null);
    assert.deepEqual(await ledger.events(), [open]);
});

test('replica recovery uses committed revision even when the device clock moved backwards', async () => {
    const values = new Map();
    const storage = {
        getItem: key => values.get(key) || null,
        setItem: (key, value) => values.set(key, String(value)),
    };
    const indexedDB = fakeIndexedDatabase();
    const options = {indexedDB, localStorage: storage, accessId: 7, actorId: 12,
        roleCode: 'excavator_operator', deviceId: 'off-c1-device'};
    const first = createLedger(options);
    const open = opening();
    await first.recordAndQueue(open);
    const [key, staleRaw] = Array.from(values.entries())[0];
    await first.recordAndQueue(load(open, 'newest-load', 2, '2026-09-29T00:10:00.000Z'));
    const stale = JSON.parse(staleRaw);
    stale.updated_at = '2999-01-01T00:00:00.000Z';
    storage.setItem(key, JSON.stringify(stale));
    const restarted = createLedger(options);
    assert.deepEqual((await restarted.events()).map(e => e.event_id), [open.event_id, 'newest-load']);
});

test('unknown stored schema is rejected instead of silently replacing its history', async () => {
    const adapter = memoryAdapter();
    await create({adapter}).recordAndQueue(opening());
    const stored = adapter.inspect();
    stored.schema_version = 99;
    await adapter.write(stored);
    await assert.rejects(create({adapter}).ready(), /другую версию/);
    assert.deepEqual(adapter.inspect(), stored);
});

test('hourly facts survive a shift change with the site UTC offset and hour rollover', async () => {
    const ledger = create({reportUtcOffset: '+0530'});
    const first = opening('first', 1, '2026-09-29T17:40:00.000Z');
    await ledger.recordAndQueue(first);
    await ledger.recordAndQueue(load(first, 'old-load', 2, '2026-09-29T18:20:00.000Z'));
    await ledger.recordAndQueue({...first, event_id:'close-first', event_type:'excavator.shift.closed', sequence:3});
    const next = opening('next', 4, '2026-09-29T18:35:00.000Z');
    await ledger.recordAndQueue(next);
    await ledger.recordAndQueue(load(next, 'next-load', 5, '2026-09-29T18:40:00.000Z'));
    const report = await ledger.hourlyReport(null, Date.parse('2026-09-29T18:45:00.000Z'));
    assert.equal(report.hours[0].period.label, '00:00–00:15');
    assert.equal(report.hours[0].totals.trip_count, 1);
    assert.equal(report.hours[1].totals.trip_count, 1);
    assert.match(report.work_date, /30/);
});

test('aggregate without source evidence cannot double local counts', async () => {
    const ledger = create({reportUtcOffset:'+0000'});
    const open = opening();
    await ledger.recordAndQueue(open);
    await ledger.recordAndQueue(load(open, 'load', 2, '2026-09-29T00:10:00.000Z'));
    const at = Date.parse('2026-09-29T00:15:00.000Z');
    const server = await ledger.hourlyReport(null, at);
    delete server.hours[0].source_event_ids;
    const result = await ledger.hourlyReport(server, at);
    assert.equal(result.hours[0].totals.trip_count, 1);
    assert.equal(result.local_projection.includes_server_snapshot, false);
});

test('cancellation subtracts a covered server trip once using server classification despite local aliases', async () => {
    const ledger = create({reportUtcOffset:'+0000'});
    const open = opening();
    await ledger.recordAndQueue(open);
    for (let i=0; i<2; i++) await ledger.recordAndQueue(load(open, 'load-'+i, i+2, '2026-09-29T00:10:00.000Z'));
    const at = Date.parse('2026-09-29T00:15:00.000Z');
    const server = await ledger.hourlyReport(null, at);
    Object.assign(server.hours[0], {
        source_trip_ids:[42], source_event_ids:['load-0','load-1'], source_trip_count:1,
        source_facts:[{trip_id:42,event_ids:['load-0','load-1'],fleet_code:'nhl',dump_point_id:9}],
        rows:[{dump_point_id:9,dump_point:'Server point',belaz:0,nhl:1}],
        totals:{belaz:0,nhl:1,trip_count:1},
    });
    for (let i=0; i<2; i++) await ledger.recordAndQueue({
        ...load(open,'cancel-'+i,i+4,'2026-09-29T00:12:00.000Z'),
        event_type:'excavator.trip.loaded.cancelled',
        payload:{local_shift_id:open.local_shift_id,source_load_event_id:'load-'+i},
    });
    const report = await ledger.hourlyReport(server, at);
    assert.equal(report.hours[0].totals.trip_count, 0);
    assert.equal(report.hours[0].totals.nhl, 0);
    assert.equal(report.hours[0].source_trip_count, 0);
    assert.deepEqual(report.hours[0].rows, []);
});

function faultStorage() {
    const control = {row:null, unavailable:false, hangOpen:false, hangWrite:false, aborted:0, closed:0, late:[], opens:[]};
    const db = {
        objectStoreNames:{contains:()=>true}, close(){control.closed++;},
        transaction(name, mode) {
            let aborted = false;
            const tx = {
                abort(){aborted=true;control.aborted++;if(tx.onabort)tx.onabort();},
                objectStore(){return {
                    get(){const r={};if(control.hangRead)return r;queueMicrotask(()=>{r.result=control.row;if(r.onsuccess)r.onsuccess();});return r;},
                    put(row){
                        const commit=()=>{if(aborted)return;control.row=clone(row);if(tx.oncomplete)tx.oncomplete();};
                        if(control.hangWrite) control.late.push(commit); else queueMicrotask(commit);
                    },
                };},
            };
            return tx;
        },
    };
    control.indexedDB={open(){
        if(control.unavailable)throw Error('unavailable');
        const r={result:db};control.opens.push(r);
        if(!control.hangOpen)queueMicrotask(()=>r.onsuccess&&r.onsuccess());
        return r;
    }};
    const values=new Map();
    control.mirrorUnavailable=false;control.mirrorWriteFails=false;
    control.localStorage={
        getItem(key){if(control.mirrorUnavailable)throw Error('unavailable');return values.get(key)||null;},
        setItem(key,value){if(control.mirrorUnavailable||control.mirrorWriteFails)throw Error('quota');values.set(key,value);},
    };
    control.values=values;
    control.ledger=()=>createLedger({indexedDB:control.indexedDB,localStorage:control.localStorage,
        storageTimeoutMs:15,accessId:7,actorId:12,deviceId:'off-c1-device'});
    return control;
}

test('hung IndexedDB open expires, late success closes without a transaction, same ledger can retry', async () => {
    const f=faultStorage();f.hangOpen=true;
    const ledger=f.ledger();
    await assert.rejects(ledger.ready(), {code:'local_shift_storage_unavailable'});
    f.opens[0].onsuccess();
    assert.equal(f.closed,1);
    assert.equal(f.row,null);
    f.hangOpen=false;
    await ledger.recordAndQueue(opening());
    assert.equal(ledger.currentShift().local_shift_id,'shift-local-1');
});

test('timed-out IndexedDB write is aborted before the next revision; late completion cannot overwrite it', async () => {
    const f=faultStorage();const ledger=f.ledger();const open=opening();
    await ledger.recordAndQueue(open);
    f.hangWrite=true;
    await ledger.recordAndQueue(load(open,'load-a',2,'2026-09-29T00:10:00.000Z'));
    assert.equal(f.aborted,1);
    assert.equal((await ledger.events()).length,2);
    f.hangWrite=false;
    await ledger.recordAndQueue(load(open,'load-b',3,'2026-09-29T00:11:00.000Z'));
    f.late.forEach(commit=>commit());
    assert.deepEqual((await f.ledger().events()).map(e=>e.event_id),[open.event_id,'load-a','load-b']);
    assert.equal(f.row.state.revision,3);
});

test('newer IndexedDB-only commit is never replaced by stale mirror when IndexedDB becomes inaccessible', async () => {
    const f=faultStorage();const ledger=f.ledger();const open=opening();
    await ledger.recordAndQueue(open);f.mirrorWriteFails=true;
    await ledger.recordAndQueue(load(open,'durable',2,'2026-09-29T00:10:00.000Z'));
    const before=clone(f.row);
    f.unavailable=true;f.mirrorWriteFails=false;
    const restarted=f.ledger();
    await assert.rejects(restarted.recordAndQueue(load(open,'next',3,'2026-09-29T00:11:00.000Z')),
        {code:'local_shift_storage_unavailable'});
    assert.deepEqual(f.row,before);
    f.unavailable=false;
    await restarted.recordAndQueue(load(open,'next',3,'2026-09-29T00:11:00.000Z'));
    assert.deepEqual((await restarted.events()).map(e=>e.event_id),[open.event_id,'durable','next']);
});

test('newer mirror-only commit is preserved when the mirror later becomes inaccessible', async () => {
    const f=faultStorage();const ledger=f.ledger();const open=opening();
    await ledger.recordAndQueue(open);f.hangWrite=true;
    await ledger.recordAndQueue(load(open,'durable',2,'2026-09-29T00:10:00.000Z'));
    f.hangWrite=false;f.mirrorUnavailable=true;
    const restarted=f.ledger();
    await assert.rejects(restarted.ready(),{code:'local_shift_storage_unavailable'});
    assert.equal(f.row.state.revision,1);
    f.mirrorUnavailable=false;
    assert.deepEqual((await restarted.events()).map(e=>e.event_id),[open.event_id,'durable']);
});

test('equal-revision divergent replicas stop without overwriting either history', async () => {
    const f=faultStorage();await f.ledger().recordAndQueue(opening());
    const [key,raw]=[...f.values.entries()][0];
    const conflicting=JSON.parse(raw);conflicting.next_sequence=99;
    f.values.set(key,JSON.stringify(conflicting));
    const before=clone(f.row);
    await assert.rejects(f.ledger().recordAndQueue(load(opening(),'next',2,'2026-09-29T00:10:00.000Z')),
        {code:'local_shift_replica_conflict'});
    assert.deepEqual(f.row,before);
    assert.equal(JSON.parse(f.values.get(key)).next_sequence,99);
});


test('hung IndexedDB read expires and releases startup for a later retry', async () => {
    const f=faultStorage();f.hangRead=true;const ledger=f.ledger();
    await assert.rejects(ledger.ready(),{code:'local_shift_storage_unavailable'});
    assert.equal(f.aborted,1);
    f.hangRead=false;
    await ledger.recordAndQueue(opening());
    assert.equal((await ledger.events()).length,1);
});

test('timeout plus mirror write failure reports no success and retries the original event safely', async () => {
    const f=faultStorage();const ledger=f.ledger();const open=opening();
    await ledger.recordAndQueue(open);
    const event=load(open,'retry',2,'2026-09-29T00:10:00.000Z');
    f.hangWrite=true;f.mirrorWriteFails=true;
    await assert.rejects(ledger.recordAndQueue(event),{code:'local_shift_storage_unavailable'});
    assert.deepEqual((await ledger.events()).map(e=>e.event_id),[open.event_id]);
    f.hangWrite=false;f.mirrorWriteFails=false;
    await ledger.recordAndQueue(event);
    f.late.forEach(commit=>commit());
    assert.deepEqual((await f.ledger().events()).map(e=>e.event_id),[open.event_id,'retry']);
});
