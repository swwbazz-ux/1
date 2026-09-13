const test = require('node:test');
const assert = require('node:assert/strict');
const createOutbox = require('../excavator-field-outbox-v1.js');

function storage() {
    const values = new Map();
    return {
        getItem: key => values.has(key) ? values.get(key) : null,
        setItem: (key, value) => values.set(key, value),
        removeItem: key => values.delete(key),
    };
}

function fakeIndexedDB() {
    const records = new Map();
    let created = false;
    const copy = value => value == null ? value : JSON.parse(JSON.stringify(value));
    function requestResult(value) {
        const request = {};
        queueMicrotask(() => {
            request.result = copy(value);
            if (request.onsuccess) request.onsuccess();
        });
        return request;
    }
    function objectStore() {
        return {
            createIndex() {},
            index: () => ({
                getAll: queueKey => requestResult([...records.values()].filter(record => record.queue_key === queueKey)),
            }),
            get: key => requestResult(records.get(key)),
            put: record => { records.set(record.storage_id, copy(record)); },
            delete: key => { records.delete(key); },
        };
    }
    const db = {
        objectStoreNames: {contains: () => created},
        createObjectStore: () => { created = true; return objectStore(); },
        transaction: () => {
            const tx = {objectStore};
            queueMicrotask(() => queueMicrotask(() => { if (tx.oncomplete) tx.oncomplete(); }));
            return tx;
        },
    };
    return {
        open: () => {
            const request = {};
            queueMicrotask(() => {
                request.result = db;
                if (!created && request.onupgradeneeded) request.onupgradeneeded();
                if (request.onsuccess) request.onsuccess();
            });
            return request;
        },
    };
}

function loadEvent(id, sequence = 1) {
    return {
        event_id: id,
        event_type: 'excavator.trip.loaded',
        format_version: 1,
        occurred_at: '2026-09-13T10:00:00.000Z',
        sequence,
        depends_on: [],
        local_trip_id: 'local-' + id,
        payload: {truck_id: 63, excavator_id: 5, dump_point_id: 2},
    };
}

function accepted(event) {
    return {
        event_id: event.event_id,
        status: 'accepted',
        server_ids: {trip_id: 100 + Number(event.sequence || 0)},
    };
}

function downtimeEvent(id, type, sequence) {
    return {
        event_id: id,
        event_type: type,
        format_version: 1,
        occurred_at: '2026-09-13T10:00:00.000Z',
        sequence,
        depends_on: [],
        actor_id: 17,
        access_id: 7,
        role_code: 'excavator_operator',
        device_id: 'device-1',
        shift_id: 31,
        equipment_id: 5,
        trip_id: null,
        local_trip_id: null,
        local_downtime_id: null,
        payload: {},
    };
}

test('durable event survives a failed send and a new outbox instance', async () => {
    const local = storage();
    const first = createOutbox({
        localStorage: local,
        queueKey: 'access-7',
        send: async () => { throw new Error('offline'); },
    });
    await first.queue(loadEvent('one'));
    await first.flush();
    assert.equal((await first.pending()).length, 1);

    let received;
    const restarted = createOutbox({
        localStorage: local,
        queueKey: 'access-7',
        send: async events => {
            received = events;
            return {ok: true, results: events.map(accepted)};
        },
    });
    const restored = await restarted.ready();
    assert.equal(restored[0].event_id, 'one');
    await restarted.retryNow();
    assert.equal(received[0].occurred_at, '2026-09-13T10:00:00.000Z');
    assert.deepEqual(await restarted.pending(), []);
});

test('a client shell update and offline reopen preserve a nonempty queue', async () => {
    const local = storage();
    const beforeUpdate = createOutbox({
        localStorage: local,
        queueKey: 'access-7',
        send: async () => { throw new Error('offline'); },
    });
    await beforeUpdate.queue(loadEvent('kept-through-update'));

    const afterUpdate = createOutbox({
        localStorage: local,
        queueKey: 'access-7',
        send: async () => { throw new Error('still offline'); },
    });
    const restored = await afterUpdate.ready();
    assert.equal(restored.length, 1);
    assert.equal(restored[0].event_id, 'kept-through-update');
    assert.equal(restored[0].sync_state, 'pending');
});

test('missing legacy sequence cannot move a restarted IndexedDB queue backwards', async () => {
    const indexedDB = fakeIndexedDB();
    const beforeRestart = createOutbox({
        indexedDB,
        localStorage: storage(),
        queueKey: 'access-7',
        send: async events => ({ok: true, results: events.map(accepted)}),
    });
    await beforeRestart.ready();
    await beforeRestart.queue(loadEvent('first', 41));

    const afterRestart = createOutbox({
        indexedDB,
        localStorage: storage(),
        queueKey: 'access-7',
        send: async events => ({ok: true, results: events.map(accepted)}),
    });
    const restored = await afterRestart.ready();
    assert.equal(restored[0].sequence, 41);
    const nextSequence = await afterRestart.allocateSequence();
    assert.equal(nextSequence, 42);
    const second = loadEvent('second', nextSequence);
    second.depends_on = ['first'];
    await afterRestart.queue(second);
    assert.deepEqual((await afterRestart.pending()).map(event => [event.event_id, event.sequence]), [
        ['first', 41],
        ['second', 42],
    ]);
    await afterRestart.flush();
    assert.equal(await afterRestart.allocateSequence(), 43);
});

test('same event id is idempotent only for an identical immutable wire event', async () => {
    const box = createOutbox({localStorage: storage(), queueKey: 'access-7', send: async () => ({})});
    const original = {
        ...loadEvent('same'),
        actor_id: 17,
        access_id: 7,
        role_code: 'excavator_operator',
        device_id: 'device-1',
        shift_id: 31,
        equipment_id: 5,
        trip_id: null,
    };
    await box.queue(original);
    const reorderedPayload = {
        ...original,
        payload: {dump_point_id: 2, excavator_id: 5, truck_id: 63},
    };
    assert.equal((await box.queue(reorderedPayload)).event_id, 'same');
    await assert.rejects(
        box.queue({...original, actor_id: 18}),
        /идентификатор события уже занят/i
    );
    await assert.rejects(
        box.queue({...original, payload: {...original.payload, dump_point_id: 9}}),
        /идентификатор события уже занят/i
    );
    assert.equal((await box.pending()).length, 1);
});

test('sequential loads retain order and dependency in one batch', async () => {
    const sent = [];
    const box = createOutbox({
        localStorage: storage(),
        queueKey: 'access-7',
        send: async events => {
            sent.push(events);
            return {ok: true, results: events.map(accepted)};
        },
    });
    const first = loadEvent('first', 10);
    const second = loadEvent('second', 11);
    second.depends_on = ['first'];
    second.payload.expected_open_trip_local_id = first.local_trip_id;
    await box.queue(second);
    await box.queue(first);
    await box.flush();
    assert.deepEqual(sent[0].map(event => event.event_id), ['first', 'second']);
    assert.deepEqual(sent[0][1].depends_on, ['first']);
    assert.equal(sent[0][1].payload.expected_open_trip_local_id, 'local-first');
    assert.equal('sync_state' in sent[0][0], false);
    assert.equal('attempt_count' in sent[0][0], false);
    assert.equal('next_retry_at' in sent[0][0], false);
});

test('offline downtime start and close share the exact local reference before sync', async () => {
    const sent = [];
    const box = createOutbox({
        localStorage: storage(),
        queueKey: 'access-7',
        send: async events => {
            sent.push(events);
            return {ok: true, results: events.map(event => ({
                event_id: event.event_id,
                status: 'accepted',
                server_ids: {downtime_event_id: 501},
            }))};
        },
    });
    const started = downtimeEvent('downtime-start', 'excavator.downtime.started', 1);
    started.local_downtime_id = started.event_id;
    started.payload.reason_id = 9;
    const ended = downtimeEvent('downtime-end', 'excavator.downtime.ended', 2);
    ended.local_downtime_id = started.event_id;
    ended.payload.local_downtime_id = started.event_id;
    ended.depends_on = [started.event_id];
    await box.queue(started);
    await box.queue(ended);
    await box.flush();
    assert.deepEqual(sent[0].map(event => event.event_id), ['downtime-start', 'downtime-end']);
    assert.equal(sent[0][0].local_downtime_id, 'downtime-start');
    assert.equal(sent[0][1].local_downtime_id, 'downtime-start');
    assert.deepEqual(sent[0][1].depends_on, ['downtime-start']);
});

test('a quick close after accepted start uses the confirmed server downtime id', async () => {
    const sent = [];
    const box = createOutbox({
        localStorage: storage(),
        queueKey: 'access-7',
        send: async events => {
            sent.push(events);
            return {ok: true, results: events.map(event => ({
                event_id: event.event_id,
                status: 'accepted',
                server_ids: {downtime_event_id: 501},
            }))};
        },
    });
    const started = downtimeEvent('downtime-start', 'excavator.downtime.started', 1);
    started.local_downtime_id = started.event_id;
    await box.queue(started);
    await box.flush();
    const ended = downtimeEvent('downtime-end', 'excavator.downtime.ended', 2);
    ended.payload.downtime_id = 501;
    await box.queue(ended);
    await box.flush();
    assert.equal(sent[1][0].payload.downtime_id, 501);
    assert.equal(sent[1][0].local_downtime_id, null);
    assert.deepEqual(sent[1][0].depends_on, []);
});

test('partial acknowledgement removes only exactly confirmed events', async () => {
    const box = createOutbox({
        localStorage: storage(),
        queueKey: 'access-7',
        send: async events => ({ok: true, results: [accepted(events[0])]}),
    });
    await box.queue(loadEvent('one', 1));
    await box.queue(loadEvent('two', 2));
    await box.flush();
    const pending = await box.pending();
    assert.equal(pending.length, 1);
    assert.equal(pending[0].event_id, 'two');
    assert.equal(pending[0].sync_state, 'pending');
});

test('accepted load without server trip id remains queued', async () => {
    const box = createOutbox({
        localStorage: storage(),
        queueKey: 'access-7',
        send: async events => ({ok: true, results: [{event_id: events[0].event_id, status: 'accepted'}]}),
    });
    await box.queue(loadEvent('one'));
    await box.flush();
    assert.equal((await box.pending()).length, 1);
});

test('conflict and authorization outcomes remain for review and are not retried', async () => {
    let calls = 0;
    const box = createOutbox({
        localStorage: storage(),
        queueKey: 'access-7',
        send: async events => {
            calls += 1;
            return {ok: true, results: [{event_id: events[0].event_id, status: 'conflict', message: 'assignment changed'}]};
        },
    });
    await box.queue(loadEvent('one'));
    await box.flush();
    await box.flush();
    const pending = await box.pending();
    assert.equal(calls, 1);
    assert.equal(pending[0].sync_state, 'conflict');
    assert.equal(pending[0].last_error, 'assignment changed');
});

test('a pending event depending on a terminal conflict becomes attention instead of hanging', async () => {
    let calls = 0;
    const attention = [];
    const box = createOutbox({
        localStorage: storage(),
        queueKey: 'access-7',
        onAttention: (event, result) => attention.push([event.event_id, result.code]),
        send: async events => {
            calls += 1;
            return {
                ok: true,
                results: events.map(event => event.event_id === 'first'
                    ? {event_id: event.event_id, status: 'conflict', message: 'assignment changed'}
                    : {event_id: event.event_id, status: 'retry', message: 'dependency pending'}),
            };
        },
    });
    const first = loadEvent('first', 1);
    const second = loadEvent('second', 2);
    const third = loadEvent('third', 3);
    second.depends_on = ['first'];
    third.depends_on = ['second'];
    await box.queue(first);
    await box.queue(second);
    await box.queue(third);
    await box.flush();
    await box.flush();
    const pending = await box.pending();
    assert.equal(calls, 1);
    assert.deepEqual(pending.map(event => [event.event_id, event.sync_state]), [
        ['first', 'conflict'],
        ['second', 'conflict'],
        ['third', 'conflict'],
    ]);
    assert.equal(pending[1].last_error_code, 'dependency_rejected');
    assert.equal(pending[2].last_error_code, 'dependency_rejected');
    assert.deepEqual(attention.slice(-2), [
        ['second', 'dependency_rejected'],
        ['third', 'dependency_rejected'],
    ]);
});

test('concurrent flush calls share one network request', async () => {
    let release;
    let calls = 0;
    const box = createOutbox({
        localStorage: storage(),
        queueKey: 'access-7',
        send: events => {
            calls += 1;
            return new Promise(resolve => { release = () => resolve({ok: true, results: events.map(accepted)}); });
        },
    });
    await box.queue(loadEvent('one'));
    const first = box.flush();
    const second = box.flush();
    await new Promise(resolve => setImmediate(resolve));
    release();
    await Promise.all([first, second]);
    assert.equal(calls, 1);
});

test('failed durable write rejects the action before UI success', async () => {
    const broken = {
        getItem: () => null,
        setItem: () => { throw new Error('quota full'); },
        removeItem: () => {},
    };
    const box = createOutbox({localStorage: broken, queueKey: 'access-7', send: async () => ({})});
    await assert.rejects(box.queue(loadEvent('one')), /quota full/);
});

test('an unsent action can be discarded only while it has no dependants', async () => {
    const box = createOutbox({localStorage: storage(), queueKey: 'access-7', send: async () => ({})});
    const first = loadEvent('first', 1);
    const second = loadEvent('second', 2);
    second.depends_on = ['first'];
    await box.queue(first);
    await box.queue(second);
    assert.equal(await box.discardUnsent('first'), false);
    assert.equal(await box.discardUnsent('second'), true);
    assert.equal(await box.discardUnsent('first'), true);
});

test('flush drains a queue in bounded batches', async () => {
    const calls = [];
    const box = createOutbox({
        localStorage: storage(),
        queueKey: 'access-7',
        batchSize: 2,
        send: async events => {
            calls.push(events.map(event => event.event_id));
            return {ok: true, results: events.map(accepted)};
        },
    });
    for (let index = 1; index <= 5; index += 1) {
        await box.queue(loadEvent('event-' + index, index));
    }
    await box.flush();
    assert.deepEqual(calls, [['event-1', 'event-2'], ['event-3', 'event-4'], ['event-5']]);
    assert.deepEqual(await box.pending(), []);
});

test('a backoff on an earlier event is not bypassed by later events', async () => {
    const calls = [];
    let retryFirst = true;
    const box = createOutbox({
        localStorage: storage(),
        queueKey: 'access-7',
        batchSize: 1,
        send: async events => {
            calls.push(events[0].event_id);
            if (retryFirst) {
                retryFirst = false;
                return {ok: true, results: [{event_id: events[0].event_id, status: 'retry'}]};
            }
            return {ok: true, results: events.map(accepted)};
        },
    });
    await box.queue(loadEvent('first', 1));
    await box.queue(loadEvent('second', 2));
    await box.flush();
    assert.deepEqual(calls, ['first']);
    await box.retryNow();
    assert.deepEqual(calls, ['first', 'first', 'second']);
});

test('offline restart retains auth review until a fresh authenticated page resumes it', async () => {
    const local = storage();
    const beforeLogin = createOutbox({
        localStorage: local,
        queueKey: 'access-7',
        send: async events => ({ok: false, results: events.map(event => ({
            event_id: event.event_id,
            status: 'auth_required',
        }))}),
    });
    await beforeLogin.queue(loadEvent('one'));
    await beforeLogin.flush();
    assert.equal((await beforeLogin.pending())[0].sync_state, 'auth_required');

    let calls = 0;
    const afterLogin = createOutbox({
        localStorage: local,
        queueKey: 'access-7',
        send: async events => {
            calls += 1;
            return {ok: true, results: events.map(accepted)};
        },
    });
    const stillBlocked = await afterLogin.ready();
    assert.equal(stillBlocked[0].sync_state, 'auth_required');
    assert.equal(calls, 0);
    const restored = await afterLogin.ready({resumeAuthRequired: true});
    assert.equal(restored[0].sync_state, 'pending');
    await afterLogin.flush();
    assert.equal(calls, 1);
    assert.deepEqual(await afterLogin.pending(), []);
});

test('confirmation callback observes the already updated queue count', async () => {
    let visibleCount = null;
    const summaries = [];
    const box = createOutbox({
        localStorage: storage(),
        queueKey: 'access-7',
        onChange: summary => summaries.push(summary.total),
        onConfirmed: () => { visibleCount = summaries.at(-1); },
        send: async events => ({ok: true, results: events.map(accepted)}),
    });
    await box.queue(loadEvent('one'));
    await box.flush();
    assert.equal(visibleCount, 0);
});
