const assert = require('node:assert/strict');
const test = require('node:test');
const createLedger = require('../excavator-local-shift-v1.js');
const createController = require('../excavator-autonomous-shift-v1.js');
const copy = value => JSON.parse(JSON.stringify(value));
const identity = {actor_id: 12, access_id: 7, role_code: 'excavator_operator', device_id: 'long-shift-phone'};

function setup(disk) {
    let saved = null;
    disk ||= {read: async () => saved && copy(saved), write: async value => { saved = copy(value); }};
    const sent = [];
    const transport = {queue: async event => { sent.push(copy(event)); }};
    const ledger = createLedger({adapter: disk, accessId: 7, actorId: 12, deviceId: identity.device_id,
        outbox: transport, locks: {request: (name, options, callback) => Promise.resolve().then(() => callback({name}))}});
    const controller = createController({ledger, transport, identity});
    return {disk, sent, ledger, controller};
}
function event(id, sequence, type = 'excavator.trip.loaded') {
    return {...identity, event_id: id, event_type: type, format_version: 1, sequence,
        local_shift_id: 'open', shift_id: 0, equipment_id: 7, occurred_at: '2026-10-05T01:00:00.000Z',
        depends_on: ['open'], payload: {local_shift_id: 'open', truck_id: 4, dump_point_id: 11, volume_m3: 20}};
}
async function prepared(count) {
    const result = setup();
    await result.controller.open({excavator_id: 7, fuel: '100', engine_hours: '1200'}, 'open');
    // Model an existing long journal, whose immutable independent originals
    // were saved before checkpoints existed. No new causal chain is assumed.
    await result.ledger.recordPreparedBatch(state => Array.from({length: count}, (_, index) =>
        event('load-' + index, state.next_sequence + index)));
    return result;
}
async function covered(ledger, close) {
    const events = await ledger.events();
    const byId = new Map(events.map(item => [item.event_id, item]));
    const found = new Set();
    const stack = [close];
    while (stack.length) {
        const child = stack.pop();
        assert.ok(child.depends_on.length <= 32);
        for (const id of child.depends_on) {
            const parent = byId.get(id);
            assert.ok(parent, 'missing original ' + id);
            assert.ok(parent.sequence < child.sequence);
            if (!found.has(id)) { found.add(id); stack.push(parent); }
        }
    }
    return found;
}

test('235 old independent loads, including acknowledged ones, remain covered after close/restart/next shift', async () => {
    const {controller, ledger, disk} = await prepared(235);
    const originals = await ledger.events();
    await controller.confirm(originals[1], {server_ids: {trip_id: 123}});
    const close = await controller.outbox.queue(event('close', 1, 'excavator.shift.closed'));
    const found = await covered(ledger, close);
    for (const source of originals) {
        assert.ok(found.has(source.event_id));
        assert.deepEqual(await ledger.getEvent(source.event_id), source);
    }
    assert.equal(ledger.currentShift().status, 'closed');
    assert.equal((await ledger.facts()).length, 235);
    const restarted = setup(disk);
    await restarted.controller.ready();
    assert.deepEqual(await restarted.ledger.getEvent('close'), close);
    assert.deepEqual(await restarted.ledger.events(), await ledger.events());
    const next = await restarted.controller.open({excavator_id: 7, fuel: '90', engine_hours: '1201'}, 'next');
    assert.deepEqual(next.depends_on, ['close']);
    assert.ok(next.sequence > close.sequence);
});

test('32 direct parents need no group; 33 and 1025 parents keep every leaf across multiple levels', async () => {
    for (const count of [31, 32, 1024]) {
        const {controller, ledger} = await prepared(count);
        const close = await controller.outbox.queue(event('close', 1, 'excavator.shift.closed'));
        const found = await covered(ledger, close);
        assert.equal([...found].filter(id => id.startsWith('load-')).length, count);
        const groups = (await ledger.events()).filter(item => item.event_type === 'excavator.shift.checkpoint');
        assert.equal(groups.length, count === 31 ? 0 : count === 32 ? 2 : 35);
        assert.ok(groups.every(item => /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(item.event_id)));
    }
});

test('quota failure persists and dispatches neither groups nor close; retry saves the entire batch once', async () => {
    const {controller, ledger, disk, sent} = await prepared(70);
    const before = await disk.read();
    const write = disk.write;
    disk.write = async () => { throw new Error('quota'); };
    await assert.rejects(controller.outbox.queue(event('close', 1, 'excavator.shift.closed')), /quota/);
    assert.deepEqual(await disk.read(), before);
    assert.equal(ledger.currentShift().status, 'open');
    assert.ok(!sent.some(item => ['excavator.shift.closed', 'excavator.shift.checkpoint'].includes(item.event_type)));
    let writes = 0;
    disk.write = async value => { writes++; return write(value); };
    const close = await controller.outbox.queue(event('close', 1, 'excavator.shift.closed'));
    assert.equal(writes, 1);
    assert.equal((await covered(ledger, close)).has('load-69'), true);
});

test('invalid member rolls back the complete prepared batch before storage or dispatch', async () => {
    const {ledger, disk, sent} = await prepared(1);
    const before = await disk.read();
    await assert.rejects(ledger.recordPreparedBatch(state => [
        event('valid-prefix', state.next_sequence),
        {...event('wrong-owner', state.next_sequence + 1), actor_id: 999}
    ]), /не принадлежит/);
    assert.deepEqual(await disk.read(), before);
    assert.equal(await ledger.getEvent('valid-prefix'), null);
    assert.equal(sent.some(item => item.event_id === 'valid-prefix'), false);
});

test('close re-reads another window’s last saved action under the journal lock', async () => {
    const first = await prepared(40);
    const other = setup(first.disk);
    await other.controller.ready();
    await other.controller.outbox.queue(event('other-window-load', 1));
    const close = await first.controller.outbox.queue(event('close', 1, 'excavator.shift.closed'));
    assert.ok((await covered(first.ledger, close)).has('other-window-load'));
    await assert.rejects(other.controller.outbox.queue(event('late-load', 1)), /закрыта/);
});

test('caller-supplied dependencies also remain covered and unchanged', async () => {
    const {controller, ledger} = await prepared(40);
    const raw = event('close', 1, 'excavator.shift.closed');
    raw.depends_on = ['earlier-own-shift-close', 'load-0'];
    const original = copy(raw);
    await controller.outbox.queue(raw);
    assert.deepEqual(raw, original);
    assert.ok((await ledger.events()).some(item => item.depends_on.includes('earlier-own-shift-close')));
});
