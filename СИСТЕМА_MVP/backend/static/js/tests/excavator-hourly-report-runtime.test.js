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
function harness(fetcher, {cached = payload(), abort = true} = {}) {
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
    const history = {state: null, pushState(state) { this.state = state; }, back() { this.state = null; listeners.popstate(); }};
    vm.runInNewContext(source, {window, document, navigator: {onLine: true}, history, location: {href: '/work/'},
        AbortController: abort ? AbortController : undefined,
        localStorage: {getItem: k => storage.get(k), setItem: (k, value) => storage.set(k, value)},
        fetch: (...args) => { requests++; return fetcher(...args); }});
    const api = window.ExcavatorHourlyReport;
    return {api, modal, content, subtitle, storage, timers, get requests() { return requests; },
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
