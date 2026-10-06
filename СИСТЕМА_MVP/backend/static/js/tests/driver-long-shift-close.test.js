const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {createRequire} = require('node:module');
const journal = require('../driver-journal-storage-v1.js');
const planner = require('../driver-shift-close-plan-v1.js');
const idb = require('./helpers/transactional-indexeddb.js');
const {storage, page} = require('./helpers/driver-local-page.js');
const source = path.resolve(__dirname, '../driver-offline-outbox-v2.js');
const sandbox = {module: {exports: {}}, require: createRequire(source), setTimeout, clearTimeout,
    window: {crypto: globalThis.crypto, DriverShiftClosePlan: planner, DriverJournalStorage: journal,
        setTimeout: () => ({unref() {}}), clearTimeout() {}, console}};
vm.runInNewContext(fs.readFileSync(source, 'utf8'), sandbox);
const outbox = sandbox.module.exports;
const clone = value => JSON.parse(JSON.stringify(value));
const identity = {actor_id: 12, access_id: 7, role_code: 'driver', device_id: 'long-driver-phone'};
const openId = 'driver-shift-open:one';
const at = '2026-10-05T01:00:00.000Z';
function closeSpec(id = 'driver-shift-close:one') {
    return {event_id: id, event_type: 'driver.shift.closed', occurred_at: '2026-10-05T02:00:00.000Z',
        payload: {end_fuel: '300', end_mileage: '12040', end_engine_hours: '3008'}};
}
function runtime(repo, ctx, local) {
    return outbox.createDriverOfflineOutbox({repository: repo, localStorage: local, accessId: 7, context: ctx,
        send: async () => { throw Error('offline'); }});
}
async function setup(count = 235, kind = 'indexedDB', options = {}) {
    const local = storage(), db = kind === 'indexedDB' ? idb(options) : null;
    const repo = db ? journal.create(db, {timeoutMs: options.timeoutMs || 2500}) : outbox.localRepository(local, 7);
    const ctx = {actorId: 12, accessId: 7, deviceId: identity.device_id, equipmentId: 9, shiftId: null, localShiftId: openId};
    const box = runtime(repo, ctx, local);
    const opening = await box.enqueue({event_id: openId, event_type: 'driver.shift.opened', local_shift_id: openId,
        occurred_at: at, payload: {truck_id: 9, start_fuel: '410', start_mileage: '12000', start_engine_hours: '3000'}});
    const events = Array.from({length: count}, (_, index) => ({...clone(opening), event_id: 'fact-' + index,
        event_type: 'driver.assignment.accepted', sequence: index + 2, depends_on: [openId],
        payload: {local_shift_id: openId, assignment_id: 8}}));
    await repo.append(() => ({events, meta: {}, value: true}), []);
    return {local, db, repo, box, ctx, events: [clone(opening), ...events], options};
}
function ancestors(events, event) {
    const byId = new Map(events.map(item => [item.event_id, item])), seen = new Set();
    function visit(child) {
        assert.ok(child.depends_on.length <= 32);
        for (const id of child.depends_on) {
            if (seen.has(id)) continue;
            seen.add(id);
            const parent = byId.get(id);
            assert.ok(parent, id); assert.ok(parent.sequence < child.sequence, id);
            visit(parent);
        }
    }
    visit(event); return seen;
}

test('235 actions, trip and downtime close atomically and survive restart before the next offline opening', async () => {
    for (const kind of ['indexedDB', 'localStorage']) {
        const data = await setup(235, kind);
        const load = await data.box.enqueue(outbox.createDriverManualLoadEvent({eventId: 'load', truckId: 9,
            excavatorId: 3, dumpPointId: 4, rockTypeId: 5, assignmentId: 8, occurredAt: at}));
        await data.box.enqueue(outbox.createDriverManualCompletedEvent({eventId: 'unload', localTripId: load.event_id,
            truckId: 9, excavatorId: 3, dumpPointId: 4, occurredAt: at}));
        await data.box.enqueue({event_id: 'stop', event_type: 'driver.downtime.started', occurred_at: at, payload: {reason_id: 4}});
        const before = clone(await data.box.journal());
        const closing = await data.box.closeShift(closeSpec(), {activeDowntimeId: 'local:stop'});
        const events = clone(await data.box.journal()), covered = ancestors(events, closing);
        for (const event of before) { assert.ok(covered.has(event.event_id)); assert.deepEqual(events.find(e => e.event_id === event.event_id), event); }
        const end = events.find(e => e.event_type === 'driver.downtime.ended');
        assert.ok(covered.has(end.event_id)); assert.equal(end.occurred_at, closing.occurred_at);
        assert.equal(events.filter(e => e.event_type === 'driver.shift.checkpoint').length, 8);
        assert.equal((await data.box.closeShift(closeSpec('another-attempt'), {})).event_id, closing.event_id);
        assert.deepEqual(clone(await data.box.journal()), events);
        const restarted = runtime(data.db ? journal.create(data.db) : outbox.localRepository(data.local, 7), data.ctx, data.local);
        assert.deepEqual(clone(await restarted.journal()), events);
        const nextOpen = await restarted.enqueue({event_id: 'driver-shift-open:two', event_type: 'driver.shift.opened',
            local_shift_id: 'driver-shift-open:two', depends_on: [closing.event_id], payload: {truck_id: 9}});
        assert.equal(nextOpen.sequence, closing.sequence + 1);
        assert.deepEqual(clone(nextOpen.depends_on), [closing.event_id]);
        await assert.rejects(data.box.enqueue({event_id: 'stale-window-action', event_type: 'driver.assignment.accepted'}), /уже закрыта/);
    }
});

test('32/33/1025 source boundaries keep every leaf through multiple levels without implicit extra parent', async () => {
    for (const count of [31, 32, 1025]) {
        const data = await setup(count);
        const close = await data.box.closeShift(closeSpec(), {}), events = clone(await data.box.journal());
        const covered = ancestors(events, close);
        for (const event of data.events) assert.ok(covered.has(event.event_id));
        assert.equal(events.filter(e => e.event_type === 'driver.shift.checkpoint').length, count === 31 ? 0 : count === 32 ? 2 : 35);
    }
});

test('checkpoint or closing write failure leaves no partial batch; retry preserves originals and reserved order', async () => {
    for (const kind of ['indexedDB', 'localStorage']) {
        for (const failType of ['driver.shift.checkpoint', 'driver.shift.closed']) {
            const options = {}, data = await setup(40, kind, options), before = clone(await data.box.journal());
            const setItem = data.local.setItem;
            if (data.db) options.failPut = (name, value) => { if (name === 'events' && value.event_type === failType) throw Error('QuotaExceededError'); };
            else data.local.setItem = (key, value) => { if (key === 'driver-offline-events-v2:7' && value.includes(failType)) throw Error('QuotaExceededError'); setItem(key, value); };
            await assert.rejects(data.box.closeShift(closeSpec(), {}), /QuotaExceededError/);
            assert.deepEqual(clone(await data.box.journal()), before);
            options.failPut = null; data.local.setItem = setItem;
            const close = await data.box.closeShift(closeSpec('retry'), {});
            assert.ok(ancestors(clone(await data.box.journal()), close).has('fact-39'));
        }
    }
});

test('two windows cannot create two closes, or append an unaccounted action behind a close', async () => {
    const data = await setup(40), second = runtime(journal.create(data.db), data.ctx, data.local);
    const results = await Promise.all([data.box.closeShift(closeSpec('close-A'), {}), second.closeShift(closeSpec('close-B'), {})]);
    assert.equal(results[0].event_id, results[1].event_id);
    assert.equal((await data.box.journal()).filter(e => e.event_type === 'driver.shift.closed').length, 1);
    await assert.rejects(second.closeShift({...closeSpec('changed'), payload: {end_fuel: '1'}}, {}), /другими показаниями/);
    const race = await setup(40), other = runtime(journal.create(race.db), race.ctx, race.local);
    const [close, action] = await Promise.allSettled([race.box.closeShift(closeSpec(), {}),
        other.enqueue({event_id: 'racing-action', event_type: 'driver.assignment.accepted', payload: {assignment_id: 8}})]);
    assert.equal(close.status, 'fulfilled');
    const events = clone(await race.box.journal());
    if (action.status === 'fulfilled') assert.ok(ancestors(events, close.value).has(action.value.event_id));
    else { assert.match(action.reason.message, /уже закрыта/); assert.equal(events.some(e => e.event_id === 'racing-action'), false); }
});

test('journal read failure and storage deadline cannot masquerade as an empty shift or commit late', async () => {
    const options = {timeoutMs: 30}, data = await setup(40, 'indexedDB', options), before = clone(await data.box.journal());
    const list = data.repo.list;
    data.repo.list = async () => { throw Error('read-unavailable'); };
    await assert.rejects(data.box.closeShift(closeSpec(), {}), /read-unavailable/);
    data.repo.list = list;
    options.onTransaction = (names, mode) => { if (mode === 'readwrite') options.hangTransactions = true; };
    await assert.rejects(data.box.closeShift(closeSpec(), {}), /deadline|stale/);
    options.hangTransactions = false; options.onTransaction = null;
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(clone(await data.box.journal()), before);
});

test('failed late fallback import cannot be mistaken for a complete journal at closure', async () => {
    for (const failure of ['corrupt', 'identity']) {
        const data = await setup(40);
        const box = outbox.createDriverOfflineOutbox({indexedDB: data.db, localStorage: data.local,
            accessId: 7, context: data.ctx, send: async () => { throw Error('offline'); }});
        await box.journal();
        const before = clone(await data.repo.list());
        const raw = failure === 'corrupt' ? '{broken' : JSON.stringify([{...data.events[0], payload: {truck_id: 100}}]);
        data.local.setItem('driver-offline-events-v2:7', raw);
        await assert.rejects(box.closeShift(closeSpec(), {}), /весь журнал смены/);
        assert.deepEqual(clone(await data.repo.list()), before);
        assert.equal(data.local.getItem('driver-offline-events-v2:7'), raw);
        data.local.setItem('driver-offline-events-v2:7', '[]');
        const close = await box.closeShift(closeSpec(), {});
        assert.ok(ancestors(clone(await box.journal()), close).has('fact-39'));
    }
});

test('confirmed and terminal sources stay in proof; locally annihilated pairs and other actors do not enter it', async () => {
    const data = await setup(35);
    for (const [index, state] of [[1, 'confirmed'], [2, 'conflict'], [3, 'auth_required'], [4, 'invalid'], [5, 'cancelled_locally'], [6, 'cancelled_locally']]) {
        await data.repo.put({...data.events[index], state});
    }
    await data.repo.put({...data.events[7], event_id: 'other-driver', actor_id: 99, sequence: 50});
    const close = await data.box.closeShift(closeSpec(), {}), events = clone(await data.box.journal()), covered = ancestors(events, close);
    for (const index of [1, 2, 3, 4]) assert.ok(covered.has(data.events[index].event_id));
    for (const id of [data.events[5].event_id, data.events[6].event_id, 'other-driver']) assert.equal(covered.has(id), false);
    assert.equal(events.filter(e => e.state === 'cancelled_locally').length, 2);
});

test('numeric and local aliases belong to one shift; a server-opened shift needs no invented opening', async () => {
    const data = await setup(40);
    await data.repo.put({...data.events[0], state: 'confirmed', server_result: {server_ids: {shift_id: 77}}});
    data.ctx.shiftId = 77; data.ctx.localShiftId = null;
    await data.box.enqueue({event_id: 'after-opening-ack', event_type: 'driver.assignment.accepted', payload: {assignment_id: 8}});
    const close = await data.box.closeShift(closeSpec(), {}), events = clone(await data.box.journal());
    const covered = ancestors(events, close);
    assert.ok(covered.has('fact-39')); assert.ok(covered.has('after-opening-ack')); assert.ok(covered.has(openId));
    const server = await setup(0), raw = clone(server.events[0]);
    await server.repo.remove(raw.event_id); server.ctx.shiftId = 88; server.ctx.localShiftId = null;
    await server.box.enqueue({event_id: 'server-action', event_type: 'driver.assignment.accepted', payload: {assignment_id: 8}});
    const serverClose = await server.box.closeShift(closeSpec(), {});
    assert.equal(serverClose.shift_id, 88); assert.deepEqual(clone(serverClose.depends_on), ['server-action']);
});

test('UI restores closed and next open shift from durable journal when projection storage fails', async () => {
    globalThis.window = globalThis;
    let view = page({preparedTruckId: '9'}); globalThis.document = view.document;
    const projection = storage(); projection.setItem = () => { throw Error('quota on derived view'); };
    const db = idb(), repo = journal.create(db);
    const context = () => ({actorId: 12, accessId: 7, deviceId: identity.device_id, equipmentId: 9,
        shiftId: view.shell.dataset.driverShiftId, localShiftId: view.shell.dataset.driverLocalShiftId});
    const box = runtime(repo, context, storage());
    const {createDriverLocalShift} = require('../driver-local-shift-v1.js');
    const controller = createDriverLocalShift({storage: projection, outbox: box});
    const opening = await controller.open(view.openForm);
    await box.enqueue({event_id: 'gesture', event_type: 'driver.assignment.accepted', payload: {assignment_id: 8}});
    const closing = await controller.close(view.closeForm);
    assert.equal(view.shell.dataset.driverShiftOpen, 'false');
    view = page({preparedTruckId: '9'}); globalThis.document = view.document;
    const restarted = createDriverLocalShift({storage: projection, outbox: box});
    assert.equal(restarted.observe(clone(await box.journal()), identity), true);
    assert.equal(restarted.state(view.shell).close_event_id, closing.event_id);
    assert.equal(view.shell.dataset.driverShiftOpen, 'false');
    assert.equal(view.openForm.querySelector('[name="start_mileage"]').value, '12040');
    const nextOpen = await restarted.open(view.openForm);
    assert.deepEqual(clone(nextOpen.depends_on), [closing.event_id]);
    assert.notEqual(nextOpen.event_id, opening.event_id);
    const restored = planner.restored(clone(await box.journal()), identity);
    assert.equal(restored.status, 'open'); assert.equal(restored.open_event_id, nextOpen.event_id);
    assert.equal(planner.restored(clone(await box.journal()), {...identity, actor_id: 99}), null);
});
