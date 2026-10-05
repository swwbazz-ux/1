const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const createLedger = require('../excavator-local-shift-v1.js');
const createController = require('../excavator-autonomous-shift-v1.js');
const copy = value => JSON.parse(JSON.stringify(value));
const identity = {actor_id: 12, access_id: 7, role_code: 'excavator_operator', device_id: 'phone'};
function storage() {
    let saved = null;
    return {read: async () => saved && copy(saved), write: async value => { saved = copy(value); }};
}
function make(adapter = storage(), transport) {
    const sent = [];
    transport ||= {queue: async e => { sent.push(copy(e)); return e; }, pending: async () => [], allocateSequence: async () => 1, discardUnsent: async () => true};
    const ledger = createLedger({adapter, ...{accessId: 7, actorId: 12, deviceId: 'phone'}, outbox: transport,
        locks: {request: (name, options, callback) => Promise.resolve().then(() => callback({name}))}});
    const controller = createController({ledger, transport, identity});
    return {ledger, controller, adapter, sent};
}
const readings = {excavator_id: 7, fuel: '100', engine_hours: '1200'};
function work(id, type = 'excavator.trip.loaded') {
    return {...identity, event_id: id, event_type: type, shift_id: 0, equipment_id: 7,
        sequence: 1, occurred_at: '2026-10-05T00:10:00.000Z', depends_on: [], payload: {truck_id: 4}};
}

test('open, load, close and next opening retain causal references without a server ID', async () => {
    const {ledger, controller} = make();
    const first = await controller.open(readings, 'open-1');
    const load = await controller.outbox.queue(work('load-1'));
    const close = await controller.outbox.queue(work('close-1', 'excavator.shift.closed'));
    const next = await controller.open(readings, 'open-2');
    assert.equal(load.local_shift_id, first.event_id);
    assert.ok(load.depends_on.includes(first.event_id));
    assert.ok(close.depends_on.includes(load.event_id));
    assert.ok(next.depends_on.includes(close.event_id));
    assert.equal(next.shift_id, 0);
    assert.deepEqual((await ledger.events()).map(e => e.event_id), ['open-1', 'load-1', 'close-1', 'open-2']);
    assert.ok(first.sequence < load.sequence && load.sequence < close.sequence && close.sequence < next.sequence);
});

test('restart and new work do not wait for a hung transport or its sequence allocator', async () => {
    const adapter = storage();
    const hung = () => new Promise(() => {});
    const transport = {queue: hung, pending: hung, allocateSequence: hung};
    const first = make(adapter, transport);
    await first.controller.open(readings, 'open-1');
    const second = make(adapter, transport);
    await second.controller.ready();
    const result = await second.controller.outbox.queue(work('load-1'));
    assert.equal(result.local_shift_id, 'open-1');
    assert.equal((await second.controller.outbox.pending()).length, 2);
    assert.ok(await second.controller.outbox.allocateSequence(0));
});

test('metadata on a transport receipt maps the shift without changing its original event', async () => {
    const {ledger, controller} = make();
    const original = await controller.open(readings, 'open-1');
    await controller.confirm({...original, sync_state: 'confirmed', attempt_count: 1}, {server_ids: {shift_id: 99}});
    const child = await controller.outbox.queue(work('load-1'));
    assert.equal(child.shift_id, 99);
    assert.equal(child.local_shift_id, 'open-1');
    assert.deepEqual(await ledger.getEvent('open-1'), original);
});

test('pre-existing server shifts retain the original transport and unchanged envelopes', async () => {
    const {controller, sent} = make();
    await controller.ready();
    const original = {...work('legacy'), shift_id: 123};
    await controller.outbox.queue(original);
    assert.deepEqual(sent, [original]);
    assert.equal(Object.hasOwn(sent[0], 'local_shift_id'), false);
});

test('failed storage does not dispatch opening or show an open local shift', async () => {
    const {controller, ledger, sent} = make({read: async () => null, write: async () => { throw new Error('quota'); }});
    await assert.rejects(controller.open(readings, 'unsaved'), /quota/);
    assert.equal(ledger.currentShift(), null);
    assert.deepEqual(sent, []);
});

test('a local cancellation must retain the original load instead of discarding it', async () => {
    const {controller, ledger} = make();
    await controller.open(readings, 'open-1');
    await controller.outbox.queue(work('load-1'));
    assert.equal(await controller.outbox.discardUnsent('load-1'), false);
    assert.ok(await ledger.getEvent('load-1'));
});

test('concurrent actions from one screen receive increasing durable sequence numbers', async () => {
    const {controller} = make();
    await controller.open(readings, 'open-1');
    const [a, b] = await Promise.all([controller.outbox.queue(work('load-a')), controller.outbox.queue(work('load-b'))]);
    assert.ok(b.sequence > a.sequence);
});

const face = {rock_type_id: '8', dump_point_ids: ['10', '11'], loading_horizon: '075', loading_block: '52'};
test('saved settings, repeated settings and both loading modes retain a causal chain and committed fields', async () => {
    const {controller, ledger} = make();
    await controller.open(readings, 'open-1');
    const settings = await controller.saveWorkContext(face, 'settings-1');
    assert.ok(settings.depends_on.includes('open-1'));
    const second = await controller.saveWorkContext({...face, loading_block: '53'}, 'settings-2');
    assert.ok(second.depends_on.includes('settings-1'));
    for (const type of ['excavator.trip.loaded', 'excavator.free_bucket.loaded']) {
        const raw = {...work(type, type), payload: {truck_id: 4, dump_point_id: 11, rock_type_id: 'draft', loading_horizon: '999', loading_block: '999'}};
        const saved = await controller.outbox.queue(raw);
        assert.ok(saved.depends_on.includes('settings-2'));
        assert.equal(saved.payload.rock_type_id, '8');
        assert.equal(saved.payload.loading_horizon, '075');
        assert.equal(saved.payload.loading_block, '53');
        assert.equal(raw.payload.rock_type_id, 'draft');
    }
    assert.deepEqual(await ledger.getEvent('settings-1'), settings);
});

test('settings survive restart and a hung network without becoming another shift defaults', async () => {
    const adapter = storage();
    const first = make(adapter, {queue: () => new Promise(() => {})});
    await first.controller.open(readings, 'open-1');
    const source = await first.controller.saveWorkContext(face, 'settings-1');
    const second = make(adapter);
    await second.controller.ready();
    assert.deepEqual(second.controller.workContext(), source);
    await second.controller.outbox.queue(work('close-1', 'excavator.shift.closed'));
    await second.controller.open(readings, 'open-2');
    assert.equal(second.controller.workContext(), null);
});

test('a stale window loading a removed destination fails without recording a load', async () => {
    const adapter = storage();
    const first = make(adapter);
    await first.controller.open(readings, 'open-1');
    await first.controller.saveWorkContext(face, 'settings-1');
    const second = make(adapter);
    await second.controller.ready();
    await first.controller.saveWorkContext({...face, dump_point_ids: ['11']}, 'settings-2');
    await assert.rejects(second.controller.outbox.queue({...work('stale-load'), payload: {dump_point_id: 10}}), /Точка разгрузки изменилась/);
    assert.equal(await second.ledger.getEvent('stale-load'), null);
    const loaded = await second.controller.outbox.queue({...work('valid-load'), payload: {dump_point_id: 11}});
    assert.ok(loaded.depends_on.includes('settings-2'));
});

test('failed settings storage preserves the previous settings and does not deliver the failed event', async () => {
    let fail = false;
    const disk = storage();
    const {controller, sent} = make({read: disk.read, write: state => { if (fail) throw new Error('quota'); return disk.write(state); }});
    await controller.open(readings, 'open-1');
    await controller.saveWorkContext(face, 'settings-1');
    fail = true;
    await assert.rejects(controller.saveWorkContext({...face, rock_type_id: '9'}, 'failed-settings'), /quota/);
    assert.equal(controller.workContext().event_id, 'settings-1');
    assert.equal(sent.some(e => e.event_id === 'failed-settings'), false);
});

test('settings cannot be recorded into an already closed or superseded shift', async () => {
    const adapter = storage();
    const first = make(adapter);
    await first.controller.open(readings, 'open-1');
    const stale = make(adapter);
    await stale.controller.ready();
    await first.controller.outbox.queue(work('close-1', 'excavator.shift.closed'));
    await assert.rejects(first.controller.saveWorkContext(face, 'closed-settings'), /Сначала начните/);
    await first.controller.open(readings, 'open-2');
    await assert.rejects(stale.controller.saveWorkContext(face, 'stale-settings'), /Смена изменилась/);
});

const template = fs.readFileSync(require.resolve('../../../templates/trips/excavator_work.html'), 'utf8');
test('screen scripts remain syntactically valid after template substitution', () => {
    for (const match of template.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)) {
        const script = match[1].replace(/{%[\s\S]*?%}/g, '').replace(/{{[\s\S]*?}}/g, '0');
        new vm.Script(script);
    }
});
function node() {
    const classes = new Set();
    return {dataset: {}, disabled: true, textContent: '', setAttribute() {},
        classList: {toggle(k,v) { if(v) classes.add(k); else classes.delete(k); }, remove(...ks) {ks.forEach(k => classes.delete(k));}, contains: k => classes.has(k)}};
}
function projection() {
    const button = node(), label = node(), card = node(), shiftScreen = node();
    card.dataset.eoPreparedCanLoad = '1';
    const shell = {dataset: {}, querySelector: selector => ({'[data-eo-shift-button]':button,'[data-eo-shift-label]':label,'[data-eo-screen="shift"]':shiftScreen}[selector] || null),
        querySelectorAll: selector => selector === '[data-eo-truck-card]' ? [card] : []};
    const context = vm.createContext({});
    vm.runInContext(template.slice(template.indexOf('function projectExcavatorLocalShift('), template.indexOf('function ensureExcavatorAutonomousShift(')), context);
    return {shell, button, label, card, render: shift => context.projectExcavatorLocalShift(shell, {currentShift: () => shift})};
}
test('local projection enables prepared cards only after opening and does not reset a pending load', () => {
    const ui = projection();
    ui.render(null);
    assert.equal(ui.card.disabled, true);
    const shift = {local_shift_id: 'open', open_event_id: 'open', status: 'open', equipment_id: 7};
    ui.render(shift);
    assert.equal(ui.card.disabled, false);
    assert.equal(ui.button.dataset.eoShiftAction, 'close');
    ui.card.disabled = true;
    ui.render({...shift, server_shift_id: 99});
    assert.equal(ui.card.disabled, true);
    assert.equal(ui.shell.dataset.nativeShiftId, '99');
    ui.render({...shift, status: 'closed'});
    assert.equal(ui.card.disabled, true);
    assert.equal(ui.button.dataset.eoShiftAction, 'open');
});

test('local shift modules are packaged in the prepared service worker shell', () => {
    const views = fs.readFileSync(require.resolve('../../../trips/views.py'), 'utf8');
    for (const script of ['excavator-local-shift-v1.js', 'excavator-autonomous-shift-v1.js', 'excavator-shift-archive-v1.js']) {
        assert.ok(views.includes('/static/js/' + script + '?v=excavator-mobile-shell-v276'));
        assert.ok(template.includes("js/" + script));
    }
});

test('an old closed local shift does not intercept a newer server shift', async () => {
    const {controller, sent} = make();
    const open = await controller.open(readings, 'open-1');
    await controller.confirm(open, {server_ids: {shift_id: 99}});
    await controller.outbox.queue(work('close-1', 'excavator.shift.closed'));
    controller.setServerShift(100);
    const legacy = {...work('new-server-load'), shift_id: 100};
    await controller.outbox.queue(legacy);
    assert.deepEqual(sent.at(-1), legacy);
    const ui = projection();
    ui.shell.dataset.eoServerRenderedShiftId = '100';
    ui.button.dataset.eoShiftAction = 'close';
    ui.render({local_shift_id:'open-1', open_event_id:'open-1', status:'closed', server_shift_id:99});
    assert.equal(ui.button.dataset.eoShiftAction, 'close');
});
