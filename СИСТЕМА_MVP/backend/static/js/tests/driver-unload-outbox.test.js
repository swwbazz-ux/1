const test = require('node:test');
const assert = require('node:assert/strict');
const createOutbox = require('../driver-unload-outbox-v1.js');

function storage() {
    const items = new Map();
    return {getItem: key => items.get(key) || null, setItem: (key, value) => items.set(key, value)};
}
const event = (id = '10') => ({trip_id: id, client_action_id: 'action-' + id, occurred_at: '2026-09-11T10:00:00Z'});
const ack = e => ({ok: true, trip_id: e.trip_id, client_action_id: e.client_action_id});

test('lost response retains original trip, action and fact time across restart', async () => {
    const local = storage();
    const first = createOutbox({storage: local, accessId: 1, send: async () => { throw Error('offline'); }});
    first.queue(event());
    await first.flush();
    let sent;
    const restarted = createOutbox({storage: local, accessId: 1, send: async e => { sent = e; return ack(e); }});
    assert.deepEqual(restarted.queue({...event(), occurred_at: 'changed'}), event());
    await restarted.flush();
    assert.deepEqual(sent, event());
    assert.deepEqual(restarted.pending(), []);
});

test('different trip acknowledgement never deletes a pending confirmation', async () => {
    const box = createOutbox({storage: storage(), accessId: 1, send: async () => ack(event('20'))});
    box.queue(event());
    await box.flush();
    assert.deepEqual(box.pending(), [event()]);
});

test('new trip and another driver retain independent pending events', async () => {
    const local = storage();
    const one = createOutbox({storage: local, accessId: 1, send: async e => ack(e)});
    const two = createOutbox({storage: local, accessId: 2, send: async e => ack(e)});
    one.queue(event()); one.queue(event('20')); two.queue(event('30'));
    await one.flush();
    assert.deepEqual(two.pending(), [event('30')]);
});

test('concurrent retries send an event once', async () => {
    let release, calls = 0;
    const box = createOutbox({storage: storage(), accessId: 1, send: e => {
        calls++; return new Promise(resolve => { release = () => resolve(ack(e)); });
    }});
    box.queue(event());
    const first = box.flush(); const second = box.flush();
    release(); await Promise.all([first, second]);
    assert.equal(calls, 1);
});

test('explicit server conflict remains available for reconciliation and is not retried', async () => {
    let calls = 0;
    const box = createOutbox({storage: storage(), accessId: 1, send: async () => {
        calls++; return {ok: false, conflict: true, error: 'cancelled'};
    }});
    box.queue(event()); await box.flush(); await box.flush();
    assert.equal(calls, 1);
    assert.equal(box.pending()[0].needs_review, true);
    assert.equal(box.pending()[0].occurred_at, event().occurred_at);
});

test('failed persistent write does not claim to have queued an event', () => {
    const box = createOutbox({storage: {getItem: () => null, setItem: () => {throw Error('full');}}, accessId: 1});
    assert.throws(() => box.queue(event()), /full/);
});
