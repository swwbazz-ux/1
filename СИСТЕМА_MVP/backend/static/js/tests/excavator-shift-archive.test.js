const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const createLedger = require('../excavator-local-shift-v1.js');
const createController = require('../excavator-autonomous-shift-v1.js');
const createArchive = require('../excavator-shift-archive-v1.js');
const copy = value => JSON.parse(JSON.stringify(value));
const identity = {actor_id: 12, access_id: 7, role_code: 'excavator_operator', device_id: 'archive-phone'};
function setup(disk, extra = {}) {
    let value = null;
    disk ||= {read: async () => copy(value), write: async next => { value = copy(next); }};
    const ledger = createLedger({adapter: disk, accessId: 7, actorId: 12, deviceId: identity.device_id,
        locks: {request: (name, options, callback) => Promise.resolve().then(() => callback({name}))}, ...extra});
    const controller = createController({ledger, transport: {queue: async () => {}}, identity});
    return {disk, ledger, controller};
}
function event(id, sequence, type = 'excavator.trip.loaded') {
    return {...identity, event_id: id, event_type: type, sequence, format_version: 1,
        local_shift_id: 'open', equipment_id: 7, shift_id: 0, depends_on: ['open'],
        occurred_at: '2026-10-05T01:00:00.000Z',
        payload: {local_shift_id: 'open', truck_id: 3, local_volume_m3: '10'}};
}
async function closed(count = 1, beforeClose) {
    const result = setup();
    await result.controller.open({excavator_id: 7, fuel: '100', engine_hours: '1200'}, 'open');
    if (count) await result.ledger.recordPreparedBatch(state => Array.from({length: count}, (_, index) => event('load-' + index, state.next_sequence + index)));
    if (beforeClose) await beforeClose(result);
    await result.controller.outbox.queue(event('close', 1, 'excavator.shift.closed'));
    return result;
}
async function proofFor(ledger, index = 0) {
    const state = await ledger.snapshot();
    const shift = state.shifts[index];
    const facts = await ledger.facts(shift.local_shift_id);
    const entries = shift.events.map((entry, index) => {
        const event = copy(entry.event);
        const load = ['excavator.trip.loaded', 'excavator.free_bucket.loaded'].includes(event.event_type);
        const result = {server_ids: {event_receipt_id: index + 1, shift_id: 99, ...(load ? {trip_id: index + 100} : {})}};
        return {event: {...event, sent_live: true}, receipt_id: index + 1, status: 'accepted',
            fingerprint: (index + 1).toString(16).padStart(64, '0'), result,
            load_fact: load ? {event_id: event.event_id, trip_id: index + 100, occurred_at: event.occurred_at,
                cancelled: facts.find(fact => fact.event_id === event.event_id).cancelled,
                volume_m3: '10', fleet_code: 'belaz', dump_point_id: 1, dump_point: 'Point'} : null};
    });
    const loads = entries.filter(entry => entry.load_fact);
    const active = loads.filter(entry => !entry.load_fact.cancelled);
    return {ok: true, schema_version: 1, snapshot_id: 'a'.repeat(64), generated_at: '2026-10-05T02:00:00.000Z',
        identity: copy(identity), shift: {local_shift_id: shift.local_shift_id, open_event_id: shift.open_event_id, close_event_id: shift.close_event_id,
            equipment_id: 7, server_shift_id: 99, opened_at: shift.opened_at, closed_at: shift.closed_at},
        entries, event_count: entries.length, projection: {source_event_ids: loads.map(entry => entry.event.event_id),
            source_trip_ids: loads.map(entry => entry.load_fact.trip_id), trip_count: active.length,
            cancelled_trip_count: loads.length - active.length, volume_m3: String(active.length * 10), unknown_volume_trip_count: 0}};
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
        await archive.refresh();
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

async function laterShifts(controller) {
    for (const id of ['recent-1', 'recent-2']) {
        await controller.open({excavator_id: 7, fuel: '90', engine_hours: '1201'}, id);
        await controller.outbox.queue(event(id + '-close', 1, 'excavator.shift.closed'));
    }
    await controller.open({excavator_id: 7, fuel: '90', engine_hours: '1202'}, 'current');
    await controller.outbox.queue(event('pending-load', 1));
}

async function reports(ledger) {
    const local = await ledger.hourlyReport(null, '2026-10-05T01:40:00.000Z', 'open');
    return {local, merged: await ledger.hourlyReport(local, '2026-10-05T01:40:00.000Z', 'open'),
        summary: await ledger.shiftSummary(null, 'open'), facts: await ledger.facts('open'),
        context: await ledger.workContext('open')};
}

test('old 235-load archive shrinks while offline reports, cancellation, context and pending facts survive restart', async t => {
    const {ledger, controller, disk} = await closed(235, async ({controller, ledger, disk}) => {
        await controller.outbox.queue({...event('context', 1, 'excavator.work_context.changed'),
            payload: {face_id: 4, face_name: 'Забой №4', dump_point_ids: [1], dump_point_name: 'Дробилка'}});
        await controller.outbox.queue({...event('free', 1, 'excavator.free_bucket.loaded'),
            payload: {local_fleet_code: 'nhl', local_volume_m3: null, dump_point_id: 1, dump_point_name: 'Склад'}});
        await controller.outbox.queue({...event('cancel', 1, 'excavator.trip.loaded.cancelled'),
            payload: {source_load_event_id: 'load-0'}});
        const state = await disk.read();
        state.shifts[0].events.find(entry => entry.event.event_id === 'load-1').event.payload = {
            local_shift_id: 'open', local_fleet_code: 'nhl', local_volume_m3: '49.40', dump_point_name: 'Местная точка'};
        await disk.write(state);
        await ledger.refresh();
    });
    const proof = await proofFor(ledger);
    proof.entries[2].result.effective_occurred_at = '2026-10-05T00:30:00.000Z';
    await ledger.confirmArchive(proof);
    await laterShifts(controller);
    const before = await disk.read(), beforeReports = await reports(ledger);
    const pending = await controller.outbox.pending(), sequence = await ledger.nextSequence();
    assert.equal(await ledger.compactArchive('open'), true);
    const after = await disk.read();
    assert.equal(after.schema_version, 2);
    assert.equal(after.shifts[0].events.length, 2);
    assert.deepEqual(after.shifts.slice(1), before.shifts.slice(1));
    assert.deepEqual(after.shifts[0].archive_coverage, before.shifts[0].archive_coverage);
    assert.deepEqual(await reports(ledger), beforeReports);
    assert.deepEqual(await controller.outbox.pending(), pending);
    assert.equal(await ledger.nextSequence(), sequence);
    const byteSize = value => Buffer.byteLength(JSON.stringify(value));
    assert.ok(byteSize(after) < byteSize(before) * .7);
    t.diagnostic(`journal bytes: ${byteSize(before)} -> ${byteSize(after)}`);
    const restored = setup(disk);
    await restored.controller.ready();
    assert.deepEqual(await reports(restored.ledger), beforeReports);
    assert.deepEqual(await restored.controller.outbox.pending(), pending);
    assert.equal(await restored.ledger.compactArchive('open'), false);
});

test('ACK-only, unsealed legacy coverage, current and two latest closed shifts are never compacted', async () => {
    const {ledger, controller, disk} = await closed(3);
    const proof = await proofFor(ledger);
    await laterShifts(controller);
    for (const item of proof.entries) {
        const original = await ledger.getEvent(item.event.event_id);
        await ledger.confirm(original, item.result);
    }
    const acked = await disk.read();
    assert.equal(await ledger.compactArchive('open'), false);
    assert.deepEqual(await disk.read(), acked);
    await ledger.confirmArchive(proof);
    for (const id of ['recent-1', 'recent-2', 'current']) assert.equal(await ledger.compactArchive(id), false);
    const legacy = await disk.read();
    delete legacy.shifts[0].archive_coverage.source_digest;
    await disk.write(legacy);
    assert.equal(await ledger.compactArchive('open'), false);
    assert.deepEqual(await disk.read(), legacy);
    assert.ok(ledger.archiveCandidates(await ledger.snapshot()).some(shift => shift.local_shift_id === 'open'));
});

test('changed original, receipt or proof prevents deletion despite a previously valid coverage marker', async () => {
    for (const change of [
        shift => { shift.events[1].event.payload.local_volume_m3 = '999'; },
        shift => { shift.events[1].server_result.effective_occurred_at = '2026-10-05T05:00:00Z'; },
        shift => { shift.events[1].server_ids.trip_id++; },
        shift => { shift.archive_coverage.manifest[1].receipt_id++; },
        shift => { shift.archive_coverage.projection.volume_m3 = '999'; },
    ]) {
        const {ledger, controller, disk} = await closed(3);
        await ledger.confirmArchive(await proofFor(ledger));
        await laterShifts(controller);
        const changed = await disk.read();
        change(changed.shifts[0]);
        await disk.write(changed);
        await assert.rejects(ledger.compactArchive('open'), /повторная сверка/);
        assert.deepEqual(await disk.read(), changed);
    }
});

test('unconfirmed source blocks compaction; a duplicate matching ACK does not invalidate the seal', async () => {
    const {ledger, controller, disk} = await closed(4);
    const original = await ledger.getEvent('load-0'), proof = await proofFor(ledger);
    await ledger.confirmArchive(proof);
    await laterShifts(controller);
    const covered = await disk.read(), unconfirmed = copy(covered);
    unconfirmed.shifts[0].events[1].delivery_state = 'awaiting_outbox';
    await disk.write(unconfirmed);
    assert.equal(await ledger.compactArchive('open'), false);
    assert.deepEqual(await disk.read(), unconfirmed);
    await disk.write(covered);
    await ledger.confirm(original, proof.entries[1].result);
    assert.equal(await ledger.compactArchive('open'), true);
});

test('quota failure restores full archive in memory and on disk; retry commits once', async () => {
    const {ledger, controller, disk} = await closed(20);
    await ledger.confirmArchive(await proofFor(ledger));
    await laterShifts(controller);
    const before = await disk.read(), beforeReports = await reports(ledger), write = disk.write;
    disk.write = async () => { throw new Error('quota'); };
    await assert.rejects(ledger.compactArchive('open'), /quota/);
    assert.deepEqual(await ledger.snapshot(), before);
    assert.deepEqual(await disk.read(), before);
    assert.deepEqual(await reports(ledger), beforeReports);
    disk.write = write;
    assert.equal(await ledger.compactArchive('open'), true);
    assert.equal((await disk.read()).revision, before.revision + 1);
});

test('compaction reloads another window’s pending actions and never rewinds the global sequence', async () => {
    const {ledger, controller, disk} = await closed(20);
    await ledger.confirmArchive(await proofFor(ledger));
    await laterShifts(controller);
    const second = setup(disk);
    await second.controller.ready();
    await second.controller.outbox.queue(event('other-window', 1));
    const latest = await disk.read();
    await ledger.compactArchive('open');
    assert.deepEqual((await disk.read()).shifts.slice(1), latest.shifts.slice(1));
    await second.controller.outbox.queue(event('after-compaction', 1));
    const saved = await disk.read();
    assert.ok(saved.shifts[0].archive_compaction);
    assert.equal(saved.shifts.at(-1).events.at(-1).event.sequence, latest.next_sequence);
});

test('archived IDs remain occupied; late receipts and discard cannot resurrect or remove accepted facts', async () => {
    const {ledger, controller, disk} = await closed(20);
    const original = await ledger.getEvent('load-0'), opening = await ledger.getEvent('open');
    const proof = await proofFor(ledger);
    await ledger.confirmArchive(proof);
    await laterShifts(controller);
    await ledger.compactArchive('open');
    const before = await disk.read();
    assert.equal(await ledger.getEvent('load-0'), null);
    assert.equal(await ledger.hasEvent('load-0'), true);
    assert.equal(await controller.outbox.discardUnsent('load-0'), false);
    assert.equal(await ledger.confirm(original, {server_ids: {trip_id: 999}}), false);
    assert.equal(await ledger.confirm(opening, {server_ids: {shift_id: 999}}), false);
    await assert.rejects(ledger.recordAndQueue({...original, local_shift_id: 'current',
        sequence: before.next_sequence, payload: {local_shift_id: 'current'}}), /подтверждённом архиве/);
    await assert.rejects(ledger.confirmArchive(proof), /не подтверждено/);
    assert.deepEqual(await disk.read(), before);
});

test('background worker re-fetches legacy coverage before compacting only the older shift', async () => {
    const {ledger, controller, disk} = await closed(235);
    const proof = await proofFor(ledger);
    await ledger.confirmArchive(proof);
    await laterShifts(controller);
    const legacy = await disk.read();
    delete legacy.shifts[0].archive_coverage.source_digest;
    await disk.write(legacy);
    await ledger.refresh();
    const offsets = [];
    const archive = client(ledger, responder(proof, url => offsets.push(Number(url.searchParams.get('offset')))));
    assert.ok(await archive.refresh());
    assert.deepEqual(offsets, [0, 100, 200]);
    const saved = await disk.read();
    assert.ok(saved.shifts[0].archive_compaction);
    assert.deepEqual(saved.shifts.slice(1), legacy.shifts.slice(1));
    assert.ok(!ledger.archiveCandidates(saved).some(shift => shift.local_shift_id === 'open'));
});

test('an empty shift that would grow is retained and is not repeatedly fetched for compaction', async () => {
    const {ledger, controller, disk} = await closed(0);
    await ledger.confirmArchive(await proofFor(ledger));
    await laterShifts(controller);
    const originals = (await disk.read()).shifts[0].events;
    assert.equal(await ledger.compactArchive('open'), false);
    const saved = await disk.read();
    assert.equal(saved.schema_version, 1);
    assert.deepEqual(saved.shifts[0].events, originals);
    assert.ok(saved.shifts[0].archive_compaction_skipped);
    assert.ok(!ledger.archiveCandidates(saved).some(shift => shift.local_shift_id === 'open'));
});

test('digest deadline rolls back coverage, releases the writer and ignores a late digest', async () => {
    const {ledger, disk} = await closed(4);
    const proof = await proofFor(ledger), before = await disk.read();
    let release;
    const hung = setup(disk, {digestTimeoutMs: 15, crypto: {subtle: {digest: () => new Promise(resolve => { release = resolve; })}}});
    await assert.rejects(hung.ledger.confirmArchive(proof), /archive_digest_deadline/);
    assert.deepEqual(await disk.read(), before);
    await hung.controller.open({excavator_id: 7, fuel: '90', engine_hours: '1201'}, 'next');
    release(new ArrayBuffer(32));
    await new Promise(resolve => setImmediate(resolve));
    assert.equal((await disk.read()).shifts[0].archive_coverage, undefined);
    assert.equal((await disk.read()).current_local_shift_id, 'next');
});

test('malformed compacted state is rejected without overwriting it', async () => {
    const {ledger, controller, disk} = await closed(4);
    await ledger.confirmArchive(await proofFor(ledger));
    await laterShifts(controller);
    await ledger.compactArchive('open');
    const valid = await disk.read();
    for (const change of [state => { state.schema_version = 1; },
        state => { state.shifts[0].archive_compaction.facts.pop(); },
        state => { state.shifts[0].events.pop(); },
        state => { state.shifts[0].archive_coverage.manifest.pop(); },
        state => { state.current_local_shift_id = 'open'; }]) {
        const broken = copy(valid);
        change(broken);
        await disk.write(broken);
        await assert.rejects(setup(disk).ledger.ready(), /повреждён/);
        assert.deepEqual(await disk.read(), broken);
    }
});

test('compaction in one mirror survives restart and an inaccessible newer mirror blocks stale writes', async () => {
    const {ledger, controller, disk} = await closed(20);
    await ledger.confirmArchive(await proofFor(ledger));
    await laterShifts(controller);
    const seed = await disk.read();
    let row = {state: copy(seed)}, mirror = JSON.stringify(seed), unavailable = false;
    const indexedDB = {open() {
        if (unavailable) throw new Error('offline storage');
        const request = {result: {objectStoreNames: {contains: () => true}, close() {}, transaction() {
            const tx = {objectStore() { return {
                get() { const read = {}; queueMicrotask(() => { read.result = copy(row); read.onsuccess(); }); return read; },
                put(next) { queueMicrotask(() => { row = copy(next); tx.oncomplete(); }); },
            }; }};
            return tx;
        }}};
        queueMicrotask(() => request.onsuccess());
        return request;
    }};
    const extra = {adapter: undefined, indexedDB, localStorage: {
        getItem: () => mirror, setItem: () => { throw new Error('quota'); },
    }};
    const first = setup(undefined, extra);
    assert.equal(await first.ledger.compactArchive('open'), true);
    assert.equal(row.state.schema_version, 2);
    assert.equal(JSON.parse(mirror).schema_version, 1);
    const restored = setup(undefined, extra);
    await restored.ledger.ready();
    assert.ok((await restored.ledger.snapshot()).shifts[0].archive_compaction);
    assert.deepEqual(await reports(restored.ledger), await reports(ledger));
    unavailable = true;
    await assert.rejects(setup(undefined, extra).ledger.ready(), {code: 'local_shift_storage_unavailable'});
    await assert.rejects(restored.controller.outbox.queue(event('unsafe-write', 1)), {code: 'local_shift_storage_unavailable'});
    assert.equal(row.state.schema_version, 2);
    unavailable = false;
    await restored.controller.outbox.queue(event('safe-write', 1));
    assert.ok(row.state.shifts[0].archive_compaction);
    assert.equal(row.state.shifts.at(-1).events.at(-1).event.event_id, 'safe-write');
});

test('a hung compaction digest releases the shared lock on deadline without a late destructive commit', async () => {
    const {ledger, controller, disk} = await closed(20);
    await ledger.confirmArchive(await proofFor(ledger));
    await laterShifts(controller);
    let held = false, entered, release;
    const started = new Promise(resolve => { entered = resolve; });
    const locks = {request(name, options, callback) {
        if (held) return Promise.resolve().then(() => callback(null));
        held = true;
        return Promise.resolve().then(() => callback({name})).finally(() => { held = false; });
    }};
    const slow = setup(disk, {locks, digestTimeoutMs: 30, crypto: {subtle: {digest: () => {
        entered();
        return new Promise(resolve => { release = resolve; });
    }}}});
    const other = setup(disk, {locks});
    await other.controller.ready();
    const saving = slow.ledger.compactArchive('open');
    await started;
    await assert.rejects(other.controller.outbox.queue(event('busy', 1)), {code: 'local_shift_busy'});
    await assert.rejects(saving, /archive_digest_deadline/);
    await other.controller.outbox.queue(event('after-timeout', 1));
    const beforeLate = await disk.read();
    release(new ArrayBuffer(32));
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(await disk.read(), beforeLate);
    assert.equal(beforeLate.shifts[0].archive_compaction, undefined);
    assert.equal(held, false);
});
