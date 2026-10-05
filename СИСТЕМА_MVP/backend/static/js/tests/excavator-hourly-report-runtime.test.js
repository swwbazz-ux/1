'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../excavator-hourly-report-v1.js'), 'utf8');

function deferred() {
    let resolve;
    const promise = new Promise(r => { resolve = r; });
    return {promise, resolve};
}
function payload(name = 'Сохранённый') {
    return {ok: true, schema_version: 2, excavator: {name}, hours: ['current', 'previous'].map(code => ({
        code, title: code, period: {start: '2026-10-05T10:00:00Z', label: '10–11'},
        rows: [], totals: {trip_count: 0}, is_empty: true,
    }))};
}
function harness(fetcher, {cached = payload(), abort = true, ledger = null} = {}) {
    const listeners = {};
    const timers = new Map();
    let nextTimer = 0;
    function node() {
        return {children: [], dataset: {}, hidden: false, textContent: '',
            classList: {add() {}, remove() {}},
            appendChild(child) { this.children.push(child); return child; },
            removeChild(child) { this.children.splice(this.children.indexOf(child), 1); },
            get firstChild() { return this.children[0]; }, setAttribute() {}, removeAttribute() {}, focus() {}};
    }
    const modal = node(), content = node(), subtitle = node(), button = node(), shell = node();
    modal.hidden = true;
    modal.dataset.eoHourlyReportUrl = '/report/';
    shell.dataset.eoCurrentExcavatorId = '1';
    modal.querySelector = selector => selector.includes('content') ? content : selector.includes('subtitle') ? subtitle : button;
    const document = {readyState: 'complete', body: node(), createElement: node,
        querySelector: selector => selector === '[data-eo-shell]' ? shell : selector.includes('modal') ? modal : button,
        addEventListener: (name, fn) => { listeners[name] = fn; }};
    const storage = new Map(cached ? [['eo-hourly-report-v2:1', JSON.stringify(cached)]] : []);
    let requests = 0;
    const window = {setTimeout(fn, delay) { const id = ++nextTimer; timers.set(id, {fn, delay}); return id; },
        clearTimeout(id) { timers.delete(id); }, addEventListener(name, fn) { listeners[name] = fn; }, removeEventListener() {}};
    if (ledger) window.eoExcavatorAutonomousShift = {ledger};
    const history = {state: null, pushState(state) { this.state = state; }, back() { this.state = null; listeners.popstate(); }};
    vm.runInNewContext(source, {window, document, navigator: {onLine: true}, history, location: {href: '/work/'},
        AbortController: abort ? AbortController : undefined,
        localStorage: {getItem: k => storage.get(k), setItem: (k, value) => storage.set(k, value)},
        fetch: (...args) => { requests++; return fetcher(...args); }});
    const api = window.ExcavatorHourlyReport;
    return {api, modal, content, subtitle, storage, timers, shell,
        emit(name) { if (listeners[name]) listeners[name](); }, get requests() { return requests; },
        open() { listeners.click({target: {closest: selector => selector.includes('open') ? button : null}, preventDefault() {}}); },
        close() { listeners.keydown({key: 'Escape', target: {}, preventDefault() {}}); },
        fire(delay) { const found = [...timers].find(([, t]) => t.delay === delay); assert.ok(found, `timer ${delay}`); timers.delete(found[0]); found[1].fn(); },
    };
}
const flush = async () => { for (let i = 0; i < 16; i++) await Promise.resolve(); };

for (const phase of ['headers', 'body']) {
    for (const abort of [true, false]) {
        test(`hung ${phase}, AbortController=${abort}: deadline releases refresh and preserves cache`, async () => {
            const hung = deferred();
            const h = harness(() => phase === 'headers' ? hung.promise : Promise.resolve({ok: true, json: () => hung.promise}), {abort});
            h.open();
            assert.match(h.subtitle.textContent, /Сохранённый/);
            const pending = h.api.refresh();
            await flush();
            assert.equal(h.requests, 1);
            h.fire(8000);
            assert.equal(await pending, false);
            assert.equal(h.modal.dataset.eoHourlyLoading, 'false');
            assert.equal(h.modal.dataset.eoHourlyState, 'offline');
            h.fire(250);
            await flush();
            assert.equal(h.requests, 2);
            h.close();
            await flush();
        });
    }
}

test('close/reopen starts a new request; late old body cannot overwrite it or clear its loading flag', async () => {
    const old = deferred(), fresh = deferred();
    let count = 0;
    const h = harness(() => Promise.resolve({ok: true, json: () => (++count === 1 ? old.promise : fresh.promise)}));
    h.open(); await flush();
    h.close(); h.open(); await flush();
    assert.equal(h.requests, 2);
    assert.equal(h.modal.dataset.eoHourlyLoading, 'true');
    old.resolve(payload('Старый')); await flush();
    assert.match(h.subtitle.textContent, /Сохранённый/);
    assert.equal(h.modal.dataset.eoHourlyLoading, 'true');
    fresh.resolve(payload('Новый')); await flush();
    assert.match(h.subtitle.textContent, /Новый/);
    assert.equal(h.modal.dataset.eoHourlyLoading, 'false');
    assert.equal([...h.timers.values()].filter(t => t.delay === 8000).length, 0);
});

for (const bad of [{ok: true, schema_version: 2, hours: []}, {...payload(), hours: [null, null]}, {...payload(), hours: [{code: 'current'}, {code: 'previous'}]}]) {
    test('partial response cannot replace the last complete cached report: ' + JSON.stringify(bad.hours), async () => {
        const h = harness(() => Promise.resolve({ok: true, json: () => Promise.resolve(bad)}));
        const before = h.storage.get('eo-hourly-report-v2:1');
        h.open(); await flush();
        assert.equal(h.storage.get('eo-hourly-report-v2:1'), before);
        assert.match(h.subtitle.textContent, /Сохранённый/);
        assert.equal(h.modal.dataset.eoHourlyState, 'offline');
        assert.equal(h.modal.dataset.eoHourlyLoading, 'false');
    });
}

test('invalid cache and hanging network settle to an honest error, not endless loading', async () => {
    const h = harness(() => new Promise(() => {}), {cached: {schema_version: 2, hours: [null]}});
    h.open(); await flush(); h.fire(8000); await flush();
    assert.equal(h.modal.dataset.eoHourlyState, 'error');
    assert.equal(h.modal.dataset.eoHourlyLoading, 'false');
});

test('synchronous fetch failure also releases request state', async () => {
    const h = harness(() => { throw new Error('unavailable'); });
    h.open(); await flush();
    assert.equal(h.modal.dataset.eoHourlyState, 'offline');
    assert.equal(h.modal.dataset.eoHourlyLoading, 'false');
});

test('timeout retries automatically after a bounded pause without another user action', async () => {
    let count = 0;
    const h = harness(() => ++count === 1 ? new Promise(() => {}) : Promise.resolve({ok: true, json: () => Promise.resolve(payload('Обновлённый'))}));
    h.open(); await flush(); h.fire(8000); await flush();
    assert.equal(h.requests, 1);
    h.fire(15000); await flush();
    assert.equal(h.requests, 2);
    assert.match(h.subtitle.textContent, /Обновлённый/);
    assert.equal(h.modal.dataset.eoHourlyState, 'ready');
});

test('closing the report cancels the scheduled retry', async () => {
    const h = harness(() => new Promise(() => {}));
    h.open(); await flush(); h.fire(8000); await flush();
    assert.ok([...h.timers.values()].some(t => t.delay === 15000));
    h.close(); await flush();
    assert.equal(h.timers.size, 0);
});

const createLedger = require('../excavator-local-shift-v1.js');
function reportLedger() {
    let state = null;
    return createLedger({accessId: 7, actorId: 12, deviceId: 'phone',
        adapter: {read: async () => state, write: async value => { state = structuredClone(value); }},
        locks: {request: (name, opts, callback) => Promise.resolve().then(() => callback({name}))},
    });
}
function localEvent(id, sequence, type = 'excavator.trip.loaded') {
    return {event_id: id, event_type: type, sequence, actor_id: 12, access_id: 7,
        role_code: 'excavator_operator', device_id: 'phone', local_shift_id: 'open', equipment_id: 1,
        occurred_at: new Date(Date.now() - 1000).toISOString(), local_trip_id: 'trip-' + id,
        payload: {local_shift_id: 'open', dump_point_id: 4, dump_point_name: 'ККД', local_fleet_code: 'belaz'}};
}
function textOf(node) { return node.textContent + (node.children || []).map(textOf).join(' '); }
async function seededLedger() {
    const ledger = reportLedger();
    await ledger.recordAndQueue(localEvent('open', 1, 'excavator.shift.opened'));
    await ledger.recordAndQueue(localEvent('load', 2));
    return ledger;
}
test('uncached local report renders before hung HTTP and refreshes new loads while HTTP remains pending', async () => {
    const ledger = await seededLedger();
    const h = harness(() => new Promise(() => {}), {cached: null, ledger});
    h.open(); await flush();
    assert.match(textOf(h.content), /1 рейс/);
    assert.match(textOf(h.content), /ККД/);
    assert.equal(h.requests, 1);
    await ledger.recordAndQueue(localEvent('load-2', 3));
    h.emit('excavator-local-shift-changed'); await flush();
    assert.match(textOf(h.content), /2 рейса/);
    assert.equal(h.requests, 1);
    assert.equal(h.storage.size, 0, 'local projection must never overwrite the raw server cache');
    h.fire(8000); await flush();
    assert.match(textOf(h.content), /2 рейса/);
    h.close();
});

test('server source IDs prevent double counting before a lost receipt arrives', async () => {
    const ledger = await seededLedger();
    const response = deferred();
    const h = harness(() => response.promise, {cached: null, ledger});
    h.open(); await flush();
    const server = await ledger.hourlyReport(null, Date.now());
    server.ok = true;
    server.hours[0].source_trip_ids = [42];
    server.hours[0].source_event_ids = ['load'];
    response.resolve({ok: true, json: async () => server}); await flush(); await flush();
    assert.match(textOf(h.content), /1 рейс/);
    assert.doesNotMatch(textOf(h.content), /2 рейса/);
    assert.equal(await ledger.confirm(await ledger.getEvent('load'), {server_ids: {trip_id: 42}}), true);
    h.emit('excavator-local-shift-changed'); await flush();
    assert.doesNotMatch(textOf(h.content), /2 рейса/);
    h.close();
});

test('a late local projection cannot repaint a closed modal', async () => {
    const ready = deferred();
    const ledger = {ready: () => ready.promise, currentShift: () => ({equipment_id: 1}), hourlyReport: async () => payload('Поздний')};
    const h = harness(() => new Promise(() => {}), {cached: null, ledger});
    h.open(); h.close();
    const before = textOf(h.content);
    ready.resolve(); await flush();
    assert.equal(textOf(h.content), before);
    assert.equal(h.modal.hidden, true);
});

test('a response for the previous excavator cannot enter the new excavator cache', async () => {
    const response = deferred();
    const h = harness(() => response.promise, {cached: null});
    h.open(); await flush();
    h.shell.dataset.eoCurrentExcavatorId = '2';
    response.resolve({ok:true, json:async () => ({...payload('Первая техника'), excavator:{id:1,name:'Первая техника'}})});
    await flush();
    assert.equal(h.storage.size, 0);
    assert.doesNotMatch(h.subtitle.textContent, /Первая техника/);
    h.close();
});


test('whole-shift section shows older loads and cancellations while the network hangs', async () => {
    const ledger=await seededLedger();
    const old=localEvent('old',3);old.occurred_at=new Date(Date.now()-5*3600000).toISOString();
    await ledger.recordAndQueue(old);
    await ledger.recordAndQueue({...localEvent('unknown',4),payload:{local_shift_id:'open',local_fleet_code:'unknown'}});
    await ledger.recordAndQueue({...localEvent('cancel',5),event_type:'excavator.trip.loaded.cancelled',
        payload:{local_shift_id:'open',source_load_event_id:'load'}});
    const h=harness(()=>new Promise(()=>{}),{cached:null,ledger});
    h.open();await flush();
    const section=h.content.children.find(n=>n.className?.includes('is-shift-report'));
    assert.ok(section);
    assert.match(textOf(section),/За смену/);
    assert.match(textOf(section),/2 рейса/);
    assert.match(textOf(section),/Отменено: 1/);
    assert.match(textOf(section),/1 рейс требует уточнения/);
    assert.equal(h.storage.size,0);
    h.close();
});

test('previous local shift is not labelled as the total for a newer server shift', async () => {
    const ledger=await seededLedger();
    await ledger.recordAndQueue({...localEvent('close',3),event_type:'excavator.shift.closed'});
    const h=harness(()=>new Promise(()=>{}),{cached:null,ledger});
    h.shell.dataset.eoServerRenderedShiftId='999';
    h.open();await flush();
    assert.equal(h.content.children.some(n=>n.className?.includes('is-shift-report')),false);
    h.close();
});
