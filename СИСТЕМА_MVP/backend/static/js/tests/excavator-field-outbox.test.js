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

test('accepted free-bucket load without server trip id remains queued', async () => {
    const box = createOutbox({
        localStorage: storage(),
        queueKey: 'access-7',
        send: async events => ({ok: true, results: [{event_id: events[0].event_id, status: 'accepted'}]}),
    });
    const event = loadEvent('free-bucket-load');
    event.event_type = 'excavator.free_bucket.loaded';
    event.depends_on = ['free-bucket-accept'];
    event.payload.free_bucket_acceptance_local_id = 'free-bucket-accept';
    await box.queue(event);
    await box.flush();
    const pending = await box.pending();
    assert.equal(pending.length, 1);
    assert.deepEqual(pending[0].depends_on, ['free-bucket-accept']);
});

test('confirmed free-bucket acceptance survives restart with identity and server mapping', async () => {
    const local = storage();
    const event = {
        event_id: 'free-accept-confirmed',
        event_type: 'excavator.free_bucket.accepted',
        format_version: 1,
        actor_id: 17,
        access_id: 7,
        role_code: 'excavator_operator',
        device_id: 'device-1',
        shift_id: 31,
        equipment_id: 5,
        trip_id: null,
        local_trip_id: null,
        local_downtime_id: null,
        occurred_at: '2026-09-14T10:00:00.000Z',
        sequence: 1,
        depends_on: [],
        payload: {truck_id: 63, truck_number: '63'},
    };
    const first = createOutbox({
        localStorage: local,
        queueKey: 'free-confirmed',
        send: async events => ({results: events.map(item => ({
            event_id: item.event_id,
            status: 'accepted',
            server_ids: {free_bucket_acceptance_id: 701},
        }))}),
    });
    await first.queue(event);
    await first.flush();
    assert.deepEqual(await first.pending(), []);

    const restarted = createOutbox({
        localStorage: local,
        queueKey: 'free-confirmed',
        send: async () => ({results: []}),
    });
    const [confirmation] = await restarted.confirmed();
    assert.equal(confirmation.event.event_id, event.event_id);
    assert.equal(confirmation.event.payload.truck_id, 63);
    assert.equal(confirmation.result.server_ids.free_bucket_acceptance_id, 701);
    assert.deepEqual(await restarted.getServerMapping(event.event_id), {free_bucket_acceptance_id: 701});

    const duplicate = await restarted.queue(event);
    assert.equal(duplicate.sync_state, 'confirmed');
    assert.deepEqual(duplicate.server_result.server_ids, {free_bucket_acceptance_id: 701});
    await assert.rejects(
        restarted.queue({...event, payload: {truck_id: 64, truck_number: '64'}}),
        /Идентификатор события уже занят/
    );
});

test('IndexedDB confirmation writes receipt and removes pending event in one restart-safe state', async () => {
    const indexedDB = fakeIndexedDB();
    const event = loadEvent('idb-confirmed-free', 1);
    event.event_type = 'excavator.free_bucket.accepted';
    event.local_trip_id = null;
    const first = createOutbox({
        indexedDB,
        localStorage: storage(),
        queueKey: 'free-idb-confirmed',
        send: async events => ({results: events.map(item => ({
            event_id: item.event_id,
            status: 'accepted',
            server_ids: {free_bucket_acceptance_id: 702},
        }))}),
    });
    await first.queue(event);
    await first.flush();

    const restarted = createOutbox({
        indexedDB,
        localStorage: storage(),
        queueKey: 'free-idb-confirmed',
        send: async () => ({results: []}),
    });
    assert.deepEqual(await restarted.pending(), []);
    assert.equal((await restarted.confirmed())[0].result.server_ids.free_bucket_acceptance_id, 702);
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

test('restart retries a legacy device clock conflict and its dependency chain', async () => {
    const local = storage();
    const beforeUpdate = createOutbox({
        localStorage: local,
        queueKey: 'access-7',
        send: async events => ({
            ok: true,
            results: events.map(event => event.event_id === 'clock-first'
                ? {
                    event_id: event.event_id,
                    status: 'conflict',
                    message: 'Часы устройства заметно опережают сервер. Требуется сверка.',
                }
                : {
                    event_id: event.event_id,
                    status: 'conflict',
                    message: 'Предыдущее событие требует сверки или отклонено.',
                }),
        }),
    });
    const first = loadEvent('clock-first', 1);
    const second = loadEvent('clock-second', 2);
    second.depends_on = [first.event_id];
    await beforeUpdate.queue(first);
    await beforeUpdate.queue(second);
    await beforeUpdate.flush();
    assert.deepEqual((await beforeUpdate.pending()).map(event => event.sync_state), ['conflict', 'conflict']);

    let replayed = [];
    const afterUpdate = createOutbox({
        localStorage: local,
        queueKey: 'access-7',
        send: async events => {
            replayed = events.map(event => event.event_id);
            return {ok: true, results: events.map(accepted)};
        },
    });
    const restored = await afterUpdate.ready();
    assert.deepEqual(restored.map(event => [event.event_id, event.sync_state]), [
        ['clock-first', 'pending'],
        ['clock-second', 'pending'],
    ]);
    await afterUpdate.flush();
    assert.deepEqual(replayed, ['clock-first', 'clock-second']);
    assert.deepEqual(await afterUpdate.pending(), []);
});

test('restart retries a legacy post-unload cooldown refusal with the immutable event', async () => {
    const local = storage();
    const original = loadEvent('legacy-post-unload-cooldown', 17);
    original.occurred_at = '2026-10-02T01:02:03.000Z';
    const beforeUpdate = createOutbox({
        localStorage: local,
        queueKey: 'access-e2',
        send: async events => ({
            ok: true,
            results: events.map(event => ({
                event_id: event.event_id,
                status: 'conflict',
                code: 'post_unload_cooldown',
                message: 'Возвращается к экскаватору · 10 мин.',
            })),
        }),
    });
    await beforeUpdate.queue(original);
    await beforeUpdate.flush();
    assert.deepEqual((await beforeUpdate.pending()).map(event => [event.event_id, event.sync_state]), [
        [original.event_id, 'conflict'],
    ]);

    let replayed = [];
    const afterUpdate = createOutbox({
        localStorage: local,
        queueKey: 'access-e2',
        send: async events => {
            replayed = events.map(event => ({
                event_id: event.event_id,
                sequence: event.sequence,
                occurred_at: event.occurred_at,
                payload: event.payload,
            }));
            return {ok: true, results: events.map(accepted)};
        },
    });

    const restored = await afterUpdate.ready();
    assert.deepEqual(restored.map(event => [event.event_id, event.sync_state]), [
        [original.event_id, 'pending'],
    ]);
    await afterUpdate.flush();
    assert.deepEqual(replayed, [{
        event_id: original.event_id,
        sequence: original.sequence,
        occurred_at: original.occurred_at,
        payload: original.payload,
    }]);
    assert.deepEqual(await afterUpdate.pending(), []);
});

test('restart restores the full removed-cooldown chain with immutable envelopes', async () => {
    const local = storage();
    const root = loadEvent('removed-cooldown-root', 31);
    root.occurred_at = '2026-10-02T02:00:01.000Z';
    const child = loadEvent('removed-cooldown-child', 32);
    child.event_type = 'excavator.trip.loaded.cancelled';
    child.occurred_at = '2026-10-02T02:00:02.000Z';
    child.depends_on = [root.event_id];
    child.local_trip_id = root.local_trip_id;
    child.payload = {local_trip_id: root.local_trip_id, truck_id: root.payload.truck_id};
    const grandchild = loadEvent('removed-cooldown-grandchild', 33);
    grandchild.occurred_at = '2026-10-02T02:00:03.000Z';
    grandchild.depends_on = [child.event_id];
    const beforeUpdate = createOutbox({
        localStorage: local,
        queueKey: 'access-e2-r1-chain',
        send: async events => ({
            ok: true,
            results: events.map(event => ({
                event_id: event.event_id,
                status: 'conflict',
                code: event.event_id === root.event_id ? 'post_unload_cooldown' : 'dependency_rejected',
                message: 'legacy conflict',
            })),
        }),
    });
    for (const event of [root, child, grandchild]) await beforeUpdate.queue(event);
    await beforeUpdate.flush();
    assert.deepEqual((await beforeUpdate.pending()).map(event => event.sync_state), [
        'conflict', 'conflict', 'conflict',
    ]);

    const sent = [];
    const afterUpdate = createOutbox({
        localStorage: local,
        queueKey: 'access-e2-r1-chain',
        send: async events => {
            sent.push(...events);
            return {ok: true, results: events.map(accepted)};
        },
    });
    const restored = await afterUpdate.ready();
    assert.deepEqual(restored.map(event => [event.event_id, event.sync_state]), [
        [root.event_id, 'pending'],
        [child.event_id, 'pending'],
        [grandchild.event_id, 'pending'],
    ]);
    await afterUpdate.flush();
    assert.deepEqual(sent.map(event => ({
        event_id: event.event_id,
        occurred_at: event.occurred_at,
        sequence: event.sequence,
        depends_on: event.depends_on,
        payload: event.payload,
        local_recovery_marker: event.removed_refusal_root_ids,
    })), [root, child, grandchild].map(event => ({
        event_id: event.event_id,
        occurred_at: event.occurred_at,
        sequence: event.sequence,
        depends_on: event.depends_on,
        payload: event.payload,
        local_recovery_marker: undefined,
    })));
    assert.deepEqual(await afterUpdate.pending(), []);
});

test('confirmed removed-cooldown root recovers saved children after root left the queue', async () => {
    const local = storage();
    const queueKey = 'access-e2-r1-confirmed-root';
    const storageKey = 'excavator-field-outbox-v1:' + queueKey;
    const confirmedKey = storageKey + ':confirmed';
    const root = loadEvent('confirmed-removed-cooldown-root', 41);
    const child = loadEvent('confirmed-removed-cooldown-child', 42);
    child.depends_on = [root.event_id];
    const grandchild = loadEvent('confirmed-removed-cooldown-grandchild', 43);
    grandchild.depends_on = [child.event_id];
    const oldShell = createOutbox({
        localStorage: local,
        queueKey,
        send: async events => ({ok: true, results: events.map(event => ({
            event_id: event.event_id,
            status: 'conflict',
            code: event.event_id === root.event_id ? 'post_unload_cooldown' : 'dependency_rejected',
            message: 'legacy conflict',
        }))}),
    });
    for (const event of [root, child, grandchild]) await oldShell.queue(event);
    await oldShell.flush();

    const queue = JSON.parse(local.getItem(storageKey));
    local.setItem(storageKey, JSON.stringify(queue.filter(event => event.event_id !== root.event_id)));
    local.setItem(confirmedKey, JSON.stringify([{
        event: root,
        result: {
            event_id: root.event_id,
            status: 'accepted',
            server_ids: {trip_id: 141},
            conflict_recovery: {
                reason: 'removed_refusal',
                original_code: 'post_unload_cooldown',
                root_event_ids: [root.event_id],
            },
        },
    }]));

    const replayed = [];
    const restarted = createOutbox({
        localStorage: local,
        queueKey,
        send: async events => {
            replayed.push(...events.map(event => event.event_id));
            return {ok: true, results: events.map(accepted)};
        },
    });
    const restored = await restarted.ready();
    assert.deepEqual(restored.map(event => [event.event_id, event.sync_state]), [
        [child.event_id, 'pending'],
        [grandchild.event_id, 'pending'],
    ]);
    await restarted.flush();
    assert.deepEqual(replayed, [child.event_id, grandchild.event_id]);
    assert.deepEqual(await restarted.pending(), []);
});

test('confirmed removed-cooldown child after retry recovers its saved descendant on restart', async () => {
    const local = storage();
    const queueKey = 'access-e2-r2-confirmed-child';
    const storageKey = 'excavator-field-outbox-v1:' + queueKey;
    const confirmedKey = storageKey + ':confirmed';
    const root = loadEvent('confirmed-retry-root', 44);
    const child = loadEvent('confirmed-retry-child', 45);
    child.depends_on = [root.event_id];
    const grandchild = loadEvent('confirmed-retry-grandchild', 46);
    grandchild.depends_on = [child.event_id];
    const oldShell = createOutbox({
        localStorage: local,
        queueKey,
        send: async events => ({ok: true, results: events.map(event => ({
            event_id: event.event_id,
            status: 'conflict',
            code: event.event_id === root.event_id ? 'post_unload_cooldown' : 'dependency_rejected',
            message: 'legacy conflict',
        }))}),
    });
    for (const event of [root, child, grandchild]) await oldShell.queue(event);
    await oldShell.flush();

    const queue = JSON.parse(local.getItem(storageKey));
    local.setItem(storageKey, JSON.stringify(queue.filter(event => (
        event.event_id !== root.event_id && event.event_id !== child.event_id
    ))));
    local.setItem(confirmedKey, JSON.stringify([{
        event: child,
        result: {
            event_id: child.event_id,
            status: 'accepted',
            server_ids: {trip_id: 145},
            dependency_recovery: {
                reason: 'removed_refusal_chain',
                root_event_ids: [root.event_id],
            },
        },
    }]));

    const replayed = [];
    const restarted = createOutbox({
        localStorage: local,
        queueKey,
        send: async events => {
            replayed.push(...events);
            return {ok: true, results: events.map(accepted)};
        },
    });
    const restored = await restarted.ready();
    assert.deepEqual(restored.map(event => [event.event_id, event.sync_state]), [
        [grandchild.event_id, 'pending'],
    ]);
    await restarted.flush();
    assert.deepEqual(replayed.map(event => ({
        event_id: event.event_id,
        occurred_at: event.occurred_at,
        sequence: event.sequence,
        depends_on: event.depends_on,
        payload: event.payload,
    })), [{
        event_id: grandchild.event_id,
        occurred_at: grandchild.occurred_at,
        sequence: grandchild.sequence,
        depends_on: grandchild.depends_on,
        payload: grandchild.payload,
    }]);
    assert.deepEqual(await restarted.pending(), []);
});

test('lost root response and restart resend the same removed-cooldown chain', async () => {
    const local = storage();
    const root = loadEvent('lost-response-root', 51);
    const child = loadEvent('lost-response-child', 52);
    child.depends_on = [root.event_id];
    const beforeRestart = createOutbox({
        localStorage: local,
        queueKey: 'access-e2-r1-lost-response',
        send: async () => { throw new Error('response lost after server commit'); },
    });
    await beforeRestart.queue(root);
    await beforeRestart.queue(child);
    await beforeRestart.flush();
    const before = await beforeRestart.pending();
    assert.deepEqual(before.map(event => [event.event_id, event.sync_state]), [
        [root.event_id, 'pending'],
        [child.event_id, 'pending'],
    ]);

    const replayed = [];
    const restarted = createOutbox({
        localStorage: local,
        queueKey: 'access-e2-r1-lost-response',
        send: async events => {
            replayed.push(...events);
            return {ok: true, results: events.map(event => ({
                ...accepted(event),
                status: event.event_id === root.event_id ? 'deduplicated' : 'accepted',
            }))};
        },
    });
    await restarted.ready();
    await restarted.retryNow();
    assert.deepEqual(replayed.map(event => ({
        event_id: event.event_id,
        occurred_at: event.occurred_at,
        sequence: event.sequence,
        depends_on: event.depends_on,
        payload: event.payload,
    })), [root, child].map(event => ({
        event_id: event.event_id,
        occurred_at: event.occurred_at,
        sequence: event.sequence,
        depends_on: event.depends_on,
        payload: event.payload,
    })));
    assert.deepEqual(await restarted.pending(), []);
});

test('restart does not retry an unrelated terminal conflict', async () => {
    const local = storage();
    const before = createOutbox({
        localStorage: local,
        queueKey: 'access-7',
        send: async events => ({ok: true, results: events.map(event => ({
            event_id: event.event_id,
            status: 'conflict',
            code: 'assignment_context_changed',
            message: 'Назначение изменилось.',
        }))}),
    });
    await before.queue(loadEvent('real-conflict'));
    await before.flush();

    const after = createOutbox({localStorage: local, queueKey: 'access-7', send: async () => ({})});
    const restored = await after.ready();
    assert.equal(restored[0].sync_state, 'conflict');
    assert.equal(restored[0].last_error_code, 'assignment_context_changed');
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

test('retry backoff after repeated failures on a live connection never exceeds ten seconds', async () => {
    // Карьер: связь с сервером есть, но нестабильна (не браузерное online/offline
    // событие) — экспоненциальный откат не должен растягиваться до старых 60 с,
    // иначе водитель долго не видит, куда ехать, после погрузки машинистом.
    const box = createOutbox({
        localStorage: storage(),
        queueKey: 'access-backoff',
        send: async events => ({
            ok: true,
            results: events.map(event => ({event_id: event.event_id, status: 'retry'})),
        }),
    });
    await box.queue(loadEvent('flaky-network', 1));
    for (let attempt = 0; attempt < 6; attempt += 1) {
        await box.retryNow();
    }
    const [event] = await box.pending();
    const delay = Number(event.next_retry_at) - Date.now();
    assert.ok(delay <= 10000, `expected retry delay <= 10000ms, got ${delay}ms`);
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

/* Часы машиниста 24.09.2026: телефон с отведёнными назад часами писал в базу
   своё время, а переключение причины простоя и вовсе отклонялось как «раньше
   начала». Событие, ушедшее сразу после нажатия, помечается для сервера:
   телефон не мог быть офлайн эти секунды, значит его час просто неверен. */
test('an event that left at once is marked so the server can use its own receipt', async () => {
    let received;
    const box = createOutbox({
        localStorage: storage(),
        queueKey: 'access-7',
        send: async events => { received = events; return {ok: true, results: events.map(accepted)}; },
    });

    await box.queue(loadEvent('live-one'));
    await box.flush();

    assert.equal(received.length, 1);
    assert.equal(received[0].sent_live, true);
    // Служебные метки остаются на телефоне и в отпечаток события не входят.
    assert.equal('created_session' in received[0], false);
    assert.equal('created_mono' in received[0], false);
});

test('an event that waited in the queue or survived a reload is not marked live', async () => {
    const local = storage();
    const queueKey = 'excavator-field-outbox-v1:access-7';
    const offline = createOutbox({
        localStorage: local,
        queueKey: 'access-7',
        send: async () => { throw new Error('offline'); },
    });
    await offline.queue(loadEvent('stale-one'));
    await offline.flush();

    // Перезагрузка страницы обнуляет отсчёт монотонных часов, поэтому метка
    // страницы обязана расходиться — иначе пролежавшее событие выглядело бы
    // только что созданным.
    const stored = JSON.parse(local.getItem(queueKey));
    stored[0].created_session = 'page-from-a-previous-load';
    local.setItem(queueKey, JSON.stringify(stored));

    let received;
    const reopened = createOutbox({
        localStorage: local,
        queueKey: 'access-7',
        send: async events => { received = events; return {ok: true, results: events.map(accepted)}; },
    });
    await reopened.ready();
    await reopened.retryNow();

    assert.equal(received.length, 1);
    assert.equal(received[0].event_id, 'stale-one');
    assert.equal('sent_live' in received[0], false);
});
