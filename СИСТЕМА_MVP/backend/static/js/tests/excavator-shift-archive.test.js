const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const createLedger = require('../excavator-local-shift-v1.js');
const createController = require('../excavator-autonomous-shift-v1.js');
const createArchive = require('../excavator-shift-archive-v1.js');
const copy = value => JSON.parse(JSON.stringify(value));
const identity = {actor_id: 12, access_id: 7, role_code: 'excavator_operator', device_id: 'archive-phone'};
function setup(disk) {
    let value = null;
    disk ||= {read: async () => copy(value), write: async next => { value = copy(next); }};
    const ledger = createLedger({adapter: disk, accessId: 7, actorId: 12, deviceId: identity.device_id,
        locks: {request: (name, options, callback) => Promise.resolve().then(() => callback({name}))}});
    const controller = createController({ledger, transport: {queue: async () => {}}, identity});
    return {disk, ledger, controller};
}
function event(id, sequence, type = 'excavator.trip.loaded') {
    return {...identity, event_id: id, event_type: type, sequence, format_version: 1,
        local_shift_id: 'open', equipment_id: 7, shift_id: 0, depends_on: ['open'],
        occurred_at: '2026-10-05T01:00:00.000Z',
        payload: {local_shift_id: 'open', truck_id: 3, local_volume_m3: '10'}};
}
async function closed(count = 1) {
    const result = setup();
    await result.controller.open({excavator_id: 7, fuel: '100', engine_hours: '1200'}, 'open');
    if (count) await result.ledger.recordPreparedBatch(state => Array.from({length: count}, (_, index) => event('load-' + index, state.next_sequence + index)));
    await result.controller.outbox.queue(event('close', 1, 'excavator.shift.closed'));
    return result;
}
async function proofFor(ledger, index = 0) {
    const state = await ledger.snapshot();
    const shift = state.shifts[index];
    const entries = shift.events.map((entry, index) => {
        const event = copy(entry.event);
        const load = event.event_type === 'excavator.trip.loaded';
        const result = {server_ids: {event_receipt_id: index + 1, shift_id: 99, ...(load ? {trip_id: index + 100} : {})}};
        return {event: {...event, sent_live: true}, receipt_id: index + 1, status: 'accepted',
            fingerprint: (index + 1).toString(16).padStart(64, '0'), result,
            load_fact: load ? {event_id: event.event_id, trip_id: index + 100, occurred_at: event.occurred_at,
                cancelled: false, volume_m3: '10', fleet_code: 'belaz', dump_point_id: 1, dump_point: 'Point'} : null};
    });
    const loads = entries.filter(entry => entry.load_fact);
    return {ok: true, schema_version: 1, snapshot_id: 'a'.repeat(64), generated_at: '2026-10-05T02:00:00.000Z',
        identity: copy(identity), shift: {local_shift_id: shift.local_shift_id, open_event_id: shift.open_event_id, close_event_id: shift.close_event_id,
            equipment_id: 7, server_shift_id: 99, opened_at: shift.opened_at, closed_at: shift.closed_at},
        entries, event_count: entries.length, projection: {source_event_ids: loads.map(entry => entry.event.event_id),
            source_trip_ids: loads.map(entry => entry.load_fact.trip_id), trip_count: loads.length,
            cancelled_trip_count: 0, volume_m3: String(loads.length * 10), unknown_volume_trip_count: 0}};
}
function responder(proof, observe = () => {}) {
    return async (url, options) => {
        const request = new URL(url);
        const offset = Number(request.searchParams.get('offset'));
        observe(request, options);
        const end = Math.min(offset + 100, proof.entries.length);
        return {ok: true, json: async () => ({...copy(proof), offset, entries: copy(proof.entries.slice(offset, end)),
            next_offset: end < proof.entries.length ? end : null})};
    };
}
function client(ledger, fetch, extra = {}) {
    return createArchive({ledger, url: 'https://example.test/offline-events/excavator-shift-archive/', fetch, ...extra});
}

test('ACK alone never proves coverage; a full snapshot survives restart without deleting originals', async () => {
    const {controller, ledger, disk} = await closed(4);
    const originals = await ledger.events();
    const proof = await proofFor(ledger);
    for (let i = 0; i < originals.length; i++) await controller.confirm(originals[i], proof.entries[i].result);
    assert.equal(ledger.currentShift().archive_coverage, undefined);
    await ledger.confirmArchive(proof);
    assert.deepEqual(await ledger.events(), originals);
    const restored = setup(disk);
    await restored.ledger.ready();
    assert.equal(restored.ledger.currentShift().archive_coverage.event_count, originals.length);
    assert.equal(restored.ledger.currentShift().archive_coverage.projection.trip_count, 4);
    assert.deepEqual(await restored.ledger.events(), originals);
});

test('235 loads and checkpoint groups are covered by all pages; lost ACKs are restored once', async () => {
    const {ledger, controller} = await closed(235);
    const originals = await ledger.events();
    const proof = await proofFor(ledger);
    const offsets = [];
    const archive = client(ledger, responder(proof, (url, options) => {
        offsets.push(Number(url.searchParams.get('offset')));
        if (offsets.length > 1) assert.equal(url.searchParams.get('snapshot_id'), proof.snapshot_id);
        assert.equal(options.cache, 'no-store');
    }));
    const first = archive.refresh();
    assert.equal(first, archive.refresh());
    const result = await first;
    assert.ok(result);
    assert.deepEqual(offsets, [0, 100, 200]);
    assert.equal(result.event_count, 245);
    assert.equal(result.load_facts.length, 235);
    assert.equal(result.projection.trip_count, 235);
    assert.equal((await controller.outbox.pending()).length, 0);
    assert.deepEqual(await ledger.events(), originals);
});

test('partial, foreign, altered and malformed proof cannot mark any source as covered', async () => {
    const changes = [
        p => p.entries.pop(), p => { p.entries[1] = p.entries[0]; },
        p => { p.event_count--; }, p => { p.identity.actor_id++; }, p => { p.identity.access_id++; },
        p => { p.identity.device_id = 'foreign-phone'; }, p => { p.shift.local_shift_id = 'foreign'; },
        p => { p.shift.close_event_id = 'old-close'; }, p => { p.shift.equipment_id++; },
        p => { p.entries[1].event.payload.local_volume_m3 = '999'; },
        p => { p.entries[1].status = 'retry'; }, p => { p.entries[1].load_fact = null; },
        p => { p.entries[1].result.server_ids.trip_id++; }, p => { p.entries[0].result.server_ids.shift_id++; },
        p => { p.entries[0].fingerprint = ''; }, p => { p.entries[0].receipt_id++; },
        p => { p.projection.source_event_ids = []; }, p => { p.projection.trip_count = 0; },
        p => { p.projection.volume_m3 = '999'; }, p => { p.entries[1].load_fact.volume_m3 = ''; },
        p => { p.snapshot_id = 'bad'; }, p => { p.generated_at = 'bad'; },
    ];
    for (const change of changes) {
        const {ledger, disk} = await closed();
        const before = await disk.read();
        const proof = await proofFor(ledger);
        change(proof);
        await assert.rejects(ledger.confirmArchive(proof), /не подтверждено/);
        assert.deepEqual(await disk.read(), before);
    }
});

test('quota failure rolls back coverage and repaired receipts; retry retains all source events', async () => {
    const {ledger, disk} = await closed();
    const proof = await proofFor(ledger);
    const before = await disk.read();
    const write = disk.write;
    disk.write = async () => { throw new Error('quota'); };
    await assert.rejects(ledger.confirmArchive(proof), /quota/);
    assert.deepEqual(await disk.read(), before);
    assert.equal(ledger.currentShift().archive_coverage, undefined);
    disk.write = write;
    assert.ok(await ledger.confirmArchive(proof));
    assert.equal((await ledger.events()).length, before.shifts[0].events.length);
});

test('proof of the old shift preserves another window’s new shift and actions', async () => {
    const first = await closed();
    const proof = await proofFor(first.ledger);
    const second = setup(first.disk);
    await second.controller.ready();
    await second.controller.open({excavator_id: 7, fuel: '90', engine_hours: '1201'}, 'next');
    await second.controller.outbox.queue(event('next-load', 1));
    await first.ledger.confirmArchive(proof);
    const saved = await first.disk.read();
    assert.equal(saved.current_local_shift_id, 'next');
    assert.equal(saved.shifts[1].events.at(-1).event.event_id, 'next-load');
    assert.equal(saved.shifts[1].archive_coverage, undefined);
});

test('a late older snapshot cannot overwrite newer archive coverage', async () => {
    const {ledger} = await closed();
    const old = await proofFor(ledger);
    const recent = {...copy(old), generated_at: '2026-10-05T03:00:00.000Z', snapshot_id: 'b'.repeat(64)};
    await ledger.confirmArchive(recent);
    await assert.rejects(ledger.confirmArchive(old), /не подтверждено/);
    assert.equal(ledger.currentShift().archive_coverage.snapshot_id, recent.snapshot_id);
});

test('missing page, repeated cursor and changed snapshot leave the full archive on device', async () => {
    for (const mutation of [body => { body.next_offset = null; }, body => { body.next_offset = 0; },
        body => { body.snapshot_id = 'b'.repeat(64); }]) {
        const {ledger} = await closed(235);
        const proof = await proofFor(ledger);
        const normal = responder(proof);
        let calls = 0;
        const archive = client(ledger, async (...args) => {
            const response = await normal(...args);
            const body = await response.json();
            if (++calls === 2) mutation(body);
            return {ok: true, json: async () => body};
        });
        assert.equal(await archive.refresh(), false);
        assert.equal(ledger.currentShift().archive_coverage, undefined);
        assert.equal((await ledger.events()).length, 245);
    }
});

test('unfinished headers or body expire without holding up a new shift, and late body cannot commit', async () => {
    for (const hangBody of [false, true]) {
        const {ledger, controller} = await closed();
        const proof = await proofFor(ledger);
        let release;
        const hung = new Promise(resolve => { release = resolve; });
        const archive = client(ledger, () => hangBody ? Promise.resolve({ok: true, json: () => hung}) : hung,
            {timeoutMs: 20});
        const result = archive.refresh();
        await controller.open({excavator_id: 7, fuel: '90', engine_hours: '1201'}, 'next');
        assert.equal(controller.currentShift().local_shift_id, 'next');
        assert.equal(await result, false);
        const body = {...proof, offset: 0, next_offset: null};
        release(hangBody ? body : {ok: true, json: async () => body});
        await new Promise(resolve => setImmediate(resolve));
        assert.equal((await ledger.snapshot()).shifts[0].archive_coverage, undefined);
        assert.ok(await client(ledger, responder(proof)).refresh());
    }
});

test('a session switch while reading prevents the old callback from saving coverage', async () => {
    const {ledger} = await closed();
    const proof = await proofFor(ledger);
    let current = true;
    const archive = client(ledger, async () => ({ok: true, json: async () => {
        current = false;
        return {...proof, offset: 0, next_offset: null};
    }}), {isCurrent: () => current});
    assert.equal(await archive.refresh(), false);
    assert.equal(ledger.currentShift().archive_coverage, undefined);
});

test('temporary failure retries automatically with backoff and stops after all closed shifts are covered', async () => {
    const {ledger} = await closed();
    const proof = await proofFor(ledger);
    const timers = new Map();
    let nextId = 0, calls = 0;
    const context = vm.createContext({module: {exports: {}}, URL, AbortController,
        setTimeout: (callback, delay) => { const id = ++nextId; timers.set(id, {callback, delay}); return id; },
        clearTimeout: id => timers.delete(id)});
    vm.runInContext(fs.readFileSync(require.resolve('../excavator-shift-archive-v1.js'), 'utf8'), context);
    const normal = responder(proof);
    const archive = context.module.exports({ledger, url: 'https://example.test/archive', fetch: async (...args) => {
        if (++calls === 1) return {ok: false};
        return normal(...args);
    }});
    async function tick(delay) {
        const timer = [...timers.entries()].find(([, item]) => item.delay === delay);
        assert.ok(timer, 'missing timer ' + delay);
        timers.delete(timer[0]);
        timer[1].callback();
        await new Promise(resolve => setImmediate(resolve));
    }
    archive.schedule();
    await tick(1000);
    assert.equal(calls, 1);
    assert.equal(ledger.currentShift().archive_coverage, undefined);
    await tick(5000);
    assert.equal(calls, 2);
    assert.ok(ledger.currentShift().archive_coverage);
    await tick(1000);
    assert.equal(timers.size, 0);
    archive.stop();
});

test('an unresolved old archive does not starve a later completed shift', async () => {
    const {ledger, controller} = await closed();
    await controller.open({excavator_id: 7, fuel: '90', engine_hours: '1201'}, 'next');
    await controller.outbox.queue(event('next-close', 1, 'excavator.shift.closed'));
    const proof = await proofFor(ledger, 1);
    const normal = responder(proof);
    const archive = client(ledger, (url, options) => new URL(url).searchParams.get('local_shift_id') === 'open'
        ? Promise.resolve({ok: false}) : normal(url, options));
    assert.equal(await archive.refresh(), false);
    assert.ok(await archive.refresh());
    const state = await ledger.snapshot();
    assert.equal(state.shifts[0].archive_coverage, undefined);
    assert.ok(state.shifts[1].archive_coverage);
});
