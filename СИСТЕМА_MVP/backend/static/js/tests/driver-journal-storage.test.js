const test = require('node:test');
const assert = require('node:assert/strict');
const storageModule = require('../driver-journal-storage-v1.js');
const archive = require('../driver-shift-archive-v1.js');
const {createDriverOfflineOutbox, localRepository} = require('../driver-offline-outbox-v2.js');
const {createDriverManifestLocal, replay, merge, buildReport} = require('../driver-manifest-local-v1.js');
const indexed = require('./helpers/transactional-indexeddb.js');
const {copy, fixture, responder, storage, identity, at} = require('./helpers/driver-archive-fixture.js');
const next = () => new Promise(resolve => setImmediate(resolve));

function shift(number, count = 1, rich = false) {
    const data = fixture(count, rich);
    const ids = new Map(data.events.map(event => [event.event_id, `${number}:${event.event_id}`]));
    function remap(value, key) {
        if (Array.isArray(value)) return value.map(item => remap(item, key));
        if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([name, item]) => [name, remap(item, name)]));
        if (typeof value === 'string' && ids.has(value)) return ids.get(value);
        if (key === 'sequence') return value + number * 1000;
        if (value === 99 && /shift_id$/.test(key)) return 100 + number;
        return value;
    }
    data.events = remap(data.events); data.proof = remap(data.proof);
    data.events.forEach(event => {
        event.context_snapshot = {...identity, source: 'offline', untouched: {unknown: [null, '', false, 0]}};
        if (event.local_shift_id && !event.shift_id && event.event_type !== 'driver.shift.opened') event.depends_on = [event.local_shift_id];
    });
    data.proof.entries.forEach((entry, index) => {
        entry.event.context_snapshot = copy(data.events[index].context_snapshot);
        entry.event.depends_on = copy(data.events[index].depends_on);
    });
    return data;
}
async function setup(count = 235, options = {}) {
    const first = shift(1, count, true), later = [shift(2), shift(3)];
    const db = indexed(options), repo = storageModule.create(db);
    const events = first.events.concat(...later.map(data => data.events));
    for (const event of events) { await repo.put(event); await repo.setMeta('event-identity:' + event.event_id, event); }
    await repo.setMeta('sequence:driver:12:driver-archive-phone', 4000);
    await repo.setMeta('server-map:1:load', {trip_id: 101});
    const target = archive.candidates(events, identity)[0];
    return {db, repo, events, first, later, target};
}
function manifest(events) {
    const controller = createDriverManifestLocal({storage: storage(), now: () => Date.parse(at)});
    controller.observe(events);
    return controller.journal(identity.access_id);
}
function bytes(db) { return JSON.stringify([...db.dump()].map(([key, values]) => [key, [...values]])).length; }

test('235 actions, trips and downtimes compact losslessly; fresh offline manifest and every ID survive restart', async () => {
    const data = await setup(), before = bytes(data.db), prior = manifest(data.events);
    const result = await data.repo.compact(data.first.proof, data.target);
    assert.equal(result.compacted, true);
    assert.ok(result.after_bytes < result.before_bytes);
    assert.ok(bytes(data.db) < before * .8);
    const restarted = storageModule.create(data.db), restored = await restarted.list();
    assert.deepEqual(restored, data.events);
    assert.deepEqual(manifest(restored), prior);
    const reports = journal => journal.shifts.map(shift => buildReport(merge(replay(shift.log), {}),
        {opened_at: shift.opened_at, closed_at: shift.closed_at, now: Date.parse(at)}, 600));
    assert.deepEqual(reports(manifest(restored)), reports(prior));
    for (const event of data.first.events) {
        assert.deepEqual(await restarted.get(event.event_id), event);
        assert.deepEqual(await restarted.getMeta('event-identity:' + event.event_id), event);
    }
    assert.deepEqual(await restarted.getMeta('server-map:1:load'), {trip_id: 101});
    assert.equal(await restarted.getMeta('sequence:driver:12:driver-archive-phone'), 4000);
    assert.equal(data.db.dump().get('meta').has('event-identity:1:load'), false);
    for (const later of data.later) assert.equal((await data.repo.archiveStatus(later.events.at(-1).event_id)).compacted, undefined);
    assert.equal(await restarted.compact(data.first.proof, data.target), false);
});

test('reused ID, late ACK/retry and stale fallback cannot replace a packed original or rewind the sequence', async () => {
    const data = await setup(30);
    assert.ok(await data.repo.compact(data.first.proof, data.target));
    const old = data.first.events[1], local = storage();
    const fallback = localRepository(local, 7);
    await fallback.put({...old, state: 'pending'});
    const box = createDriverOfflineOutbox({indexedDB: data.db, localStorage: local, accessId: 7,
        context: {actorId: 12, accessId: 7, deviceId: identity.device_id, equipmentId: 9, localShiftId: '1:open'},
        send: async () => ({results: []})});
    assert.deepEqual(await box.journal(), data.events);
    assert.deepEqual(await fallback.list(), []);
    assert.deepEqual(await box.enqueue(old), old);
    await assert.rejects(box.enqueue({...old, payload: {...old.payload, changed: true}}), /offline_event_id_reused/);
    await assert.rejects(data.repo.put({...old, state: 'pending'}), /driver_archive_immutable/);
    await assert.rejects(data.repo.remove(old.event_id), /driver_archive_immutable/);
    const added = await box.enqueue({event_id: 'new-action', event_type: 'driver.assignment.accepted', shift_id: 104, payload: {assignment_id: 8}});
    assert.equal(added.sequence, 4001);
    // A v380 window can write fallback after the v381 window has initialized.
    await fallback.put({...added, event_id: 'late-old-window', sequence: 4100});
    assert.ok((await box.journal()).some(event => event.event_id === 'late-old-window'));
});

test('current shift, last two closed shifts, other devices and unconfirmed sources cannot authorize cleanup', async () => {
    const data = await setup(30), before = data.db.dump();
    for (const later of data.later) {
        const target = archive.candidates(data.events, identity).find(item => item.close_event_id === later.events.at(-1).event_id);
        assert.equal(await data.repo.compact(later.proof, target), false);
    }
    assert.deepEqual(data.db.dump(), before);
    const event = data.first.events[1];
    for (const state of ['pending', 'conflict', 'auth_required', 'invalid', 'cancelled_locally']) {
        data.db.mutate(stores => stores.get('events').set(event.event_id, {...event, state}));
        const snapshot = data.db.dump();
        assert.equal(await data.repo.compact(data.first.proof, data.target), false);
        assert.deepEqual(data.db.dump(), snapshot);
    }
    const elsewhere = data.events.map(event => event.event_id.startsWith('1:') ? event : {...event, device_id: 'other'});
    assert.equal(storageModule.eligible(elsewhere, identity, data.target), false);
});

test('quota at any archive/pointer write rolls back the complete transaction', async () => {
    for (const failAt of [1, 2, 7, 33]) {
        const options = {}, data = await setup(30, options), before = data.db.dump();
        let writes = 0;
        options.failPut = () => { if (++writes === failAt) throw Error('QuotaExceededError'); };
        await assert.rejects(data.repo.compact(data.first.proof, data.target), /QuotaExceededError/);
        assert.deepEqual(data.db.dump(), before);
        options.failPut = null;
        assert.deepEqual(await data.repo.list(), data.events);
    }
});

test('another transaction adding a fact or changing an ACK before commit cancels compaction', async () => {
    for (const change of ['fact', 'ack']) {
        const options = {}, data = await setup(30, options);
        options.onTransaction = (names, mode) => {
            if (mode !== 'readwrite' || !names.includes('driver_archives')) return;
            options.onTransaction = null;
            data.db.mutate(stores => {
                const events = stores.get('events'), old = data.first.events[1];
                events.set(change === 'fact' ? 'concurrent' : old.event_id, change === 'fact'
                    ? {...old, event_id: 'concurrent', sequence: 5000, state: 'pending'}
                    : {...old, server_result: {...old.server_result, later: true}});
            });
        };
        assert.equal(await data.repo.compact(data.first.proof, data.target), false);
        assert.equal(data.db.dump().get('driver_archives').size, 0);
        assert.equal((await data.repo.list()).length, data.events.length + (change === 'fact' ? 1 : 0));
    }
});

test('deadline and cancellation abort queued writes; late storage cannot commit a packet', async () => {
    const options = {}, data = await setup(30, options), before = data.db.dump();
    const short = storageModule.create(data.db, {timeoutMs: 25});
    options.onTransaction = (names, mode) => {
        if (mode === 'readwrite' && names.includes('driver_archives')) options.hangTransactions = true;
    };
    await assert.rejects(short.compact(data.first.proof, data.target), /deadline|stale/);
    options.hangTransactions = false; options.onTransaction = null;
    await next(); await next();
    assert.deepEqual(data.db.dump(), before);
    const abort = new AbortController(), allowed = () => !abort.signal.aborted;
    allowed.signal = abort.signal;
    options.failPut = name => { if (name === 'driver_archives') abort.abort(); };
    await assert.rejects(short.compact(data.first.proof, data.target, allowed), /stale/);
    await next(); assert.deepEqual(data.db.dump(), before);
});

test('packet corruption, missing or mismatched pointers fail closed without erasing evidence', async () => {
    for (const corrupt of ['packet', 'missing', 'pointer', 'raw']) {
        const data = await setup(30);
        await data.repo.compact(data.first.proof, data.target);
        data.db.mutate(stores => {
            if (corrupt === 'packet') stores.get('driver_archives').values().next().value.data.values[0] = 'broken';
            if (corrupt === 'missing') stores.get('events').delete('1:action-0');
            if (corrupt === 'pointer') stores.get('events').get('1:action-0').archive_index++;
            if (corrupt === 'raw') stores.get('events').set('1:action-0', data.first.events[1]);
        });
        const damaged = data.db.dump();
        await assert.rejects(storageModule.create(data.db).list(), /corrupt/);
        assert.deepEqual(data.db.dump(), damaged);
    }
});

function open(db, version) {
    return new Promise((resolve, reject) => {
        const request = db.open('field-offline-events-v1', version);
        request.onupgradeneeded = () => {
            request.result.createObjectStore('events', {keyPath: 'event_id'});
            request.result.createObjectStore('meta');
        };
        request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
    });
}
test('v380 connection blocks schema upgrade, preserves its raw journal, and recovers after that window closes', async () => {
    const db = indexed(), old = await open(db, 1), local = storage();
    const event = shift(1).events[0];
    db.mutate(stores => stores.get('events').set(event.event_id, event));
    const box = createDriverOfflineOutbox({indexedDB: db, localStorage: local, accessId: 7});
    await assert.rejects(box.journal(), /upgrade_blocked/);
    assert.equal(old.version, 1);
    assert.deepEqual(db.dump().get('events').get(event.event_id), event);
    assert.equal(local.values.has('driver-offline-events-v2:7'), false);
    old.close(); for (let i = 0; i < 6; i++) await next();
    assert.deepEqual(await box.journal(), [event]);
    await assert.rejects(open(db, 1), /VersionError/);
});

test('old source proof alone never packs: worker downloads a fresh paginated snapshot and stops once covered', async () => {
    const data = await setup(), local = storage();
    const box = {journal: () => data.repo.list(), archiveStatus: id => data.repo.archiveStatus(id),
        compactArchive: (proof, target, current) => data.repo.compact(proof, target, current)};
    let requests = 0;
    const proofs = [data.first, ...data.later];
    const worker = archive.create({outbox: box, storage: local, identity, url: 'https://example.test/archive',
        fetch: (url, options) => {
            requests++;
            const closeId = new URL(url).searchParams.get('close_event_id');
            return responder(proofs.find(item => item.proof.shift.close_event_id === closeId).proof)(url, options);
        }});
    assert.ok(await worker.refresh());
    assert.equal(requests, 3);
    assert.equal((await data.repo.archiveStatus(data.target.close_event_id)).compacted, true);
    assert.ok(await worker.refresh()); assert.ok(await worker.refresh());
    assert.equal(await worker.refresh(), false);
    assert.equal(requests, 5);
    assert.deepEqual(await box.journal(), data.events);
});

test('incomplete or changed server proof and absent byte savings leave raw journal intact', async () => {
    const data = await setup(30), original = data.db.dump();
    for (const change of [proof => proof.entries.pop(), proof => proof.entries[0].event.payload.changed = true,
        proof => proof.projection.downtime_seconds++, proof => proof.identity.actor_id++]) {
        const proof = copy(data.first.proof); change(proof);
        await assert.rejects(data.repo.compact(proof, data.target), /unproven/);
        assert.deepEqual(data.db.dump(), original);
    }
    const small = shift(1, 0), db = indexed(), repo = storageModule.create(db);
    const events = small.events.concat(shift(2).events, shift(3).events);
    for (const event of events) await repo.put(event);
    assert.equal(await repo.compact(small.proof, archive.candidates(events, identity)[0]), false);
    assert.deepEqual(await repo.list(), events);
    assert.ok((await repo.archiveStatus('1:close')).source_digest);
});
