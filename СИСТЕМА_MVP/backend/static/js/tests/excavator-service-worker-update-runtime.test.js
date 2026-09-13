const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const createOutbox = require('../excavator-field-outbox-v1.js');

const viewsSource = fs.readFileSync(path.join(__dirname, '../../../trips/views.py'), 'utf8');
const marker = 'EXCAVATOR_SERVICE_WORKER_JS = r"""';
const start = viewsSource.indexOf(marker) + marker.length;
const workerSource = viewsSource.slice(start, viewsSource.indexOf('"""', start));

class FakeHeaders {
    constructor(type) { this.type = type; }
    get(name) { return name.toLowerCase() === 'content-type' ? this.type : null; }
}

class FakeResponse {
    constructor(body, {url, type = 'text/plain', ok = true}) {
        this.body = body; this.url = url; this.ok = ok; this.headers = new FakeHeaders(type);
    }
    clone() { return new FakeResponse(this.body, {url: this.url, type: this.headers.type, ok: this.ok}); }
    text() { return Promise.resolve(this.body); }
}

class FakeRequest {
    constructor(url) { this.url = new URL(url, 'https://excavator.test').href; this.method = 'GET'; this.mode = 'same-origin'; this.headers = {get: () => null}; }
}

function fakeCache(initial = []) {
    const entries = new Map(initial.map(([key, value]) => [new URL(key, 'https://excavator.test').href, value]));
    const keyOf = request => new URL(typeof request === 'string' ? request : request.url, 'https://excavator.test').href;
    return {
        addAll: () => Promise.resolve(),
        match: request => Promise.resolve(entries.get(keyOf(request))),
        put: (request, response) => { entries.set(keyOf(request), response); return Promise.resolve(); },
        delete: request => Promise.resolve(entries.delete(keyOf(request))),
        keys: () => Promise.resolve([...entries.keys()].map(url => new FakeRequest(url))),
    };
}

function storage() {
    const values = new Map();
    return {getItem: key => values.get(key) || null, setItem: (key, value) => values.set(key, value)};
}

test('expired-session update migrates v233 shell with exact safe assets and preserves queue', async () => {
    const css = '/static/css/excavator-work-v55.css?v=excavator-mobile-shell-v233';
    const js = '/static/js/mobile-shift-unified-v1.js?v=excavator-mobile-shell-v233';
    const shellHtml = `<main data-eo-shell data-eo-role-code="excavator_operator"><link href="${css}"><script src="${js}"></script></main>`;
    const oldCache = fakeCache([
        ['/excavator/work/', new FakeResponse(shellHtml, {url: 'https://excavator.test/excavator/work/', type: 'text/html'})],
        [css, new FakeResponse('/* v233 */', {url: 'https://excavator.test' + css, type: 'text/css'})],
        [js, new FakeResponse('// v233', {url: 'https://excavator.test' + js, type: 'text/javascript'})],
        ['/static/js/stale.js?v=v233', new FakeResponse('// v232', {url: 'https://excavator.test/static/js/stale.js?v=v232', type: 'text/javascript'})],
        ['/static/js/poison.js', new FakeResponse('<form>login</form>', {url: 'https://excavator.test/login/', type: 'text/html'})],
    ]);
    const currentCache = fakeCache();
    const cacheMap = new Map([
        ['excavator-mobile-shell-v233', oldCache],
        ['excavator-mobile-shell-v234', currentCache],
    ]);
    const listeners = {};
    let skippedWaiting = false;
    let claimedClients = false;
    const context = {
        URL, Set, Promise, Request: FakeRequest, Response: FakeResponse,
        fetch: async () => new FakeResponse('<form>expired</form>', {url: 'https://excavator.test/login/', type: 'text/html'}),
        caches: {open: async name => cacheMap.get(name), keys: async () => [...cacheMap.keys()], delete: async name => cacheMap.delete(name)},
        self: {
            location: {origin: 'https://excavator.test'},
            addEventListener: (name, fn) => { listeners[name] = fn; },
            clients: {claim: async () => { claimedClients = true; }},
            skipWaiting: async () => { skippedWaiting = true; },
        },
        setTimeout, clearTimeout,
    };
    vm.createContext(context);
    vm.runInContext(workerSource, context);

    const local = storage();
    const before = createOutbox({localStorage: local, queueKey: 'access-7', send: async () => ({})});
    await before.queue({event_id: 'pending', event_type: 'excavator.trip.loaded', format_version: 1, sequence: 1, depends_on: [], payload: {truck_id: 63}});
    let installWork;
    listeners.install({waitUntil: promise => { installWork = promise; }});
    await installWork;
    assert.equal(skippedWaiting, true);

    let activateWork;
    listeners.activate({waitUntil: promise => { activateWork = promise; }});
    await activateWork;
    assert.equal(claimedClients, true);
    assert.deepEqual([...cacheMap.keys()], ['excavator-mobile-shell-v234']);

    context.fetch = async () => { throw new Error('offline'); };
    let navigationResponse;
    listeners.fetch({
        request: new FakeRequest('/excavator/work/'),
        respondWith: promise => { navigationResponse = promise; },
    });
    const reopenedShell = await navigationResponse;
    const fetchOfflineAsset = async path => {
        let responseWork;
        listeners.fetch({
            request: new FakeRequest(path),
            respondWith: promise => { responseWork = promise; },
        });
        return responseWork;
    };
    const reopenedCss = await fetchOfflineAsset(css);
    const reopenedJs = await fetchOfflineAsset(js);
    const after = createOutbox({localStorage: local, queueKey: 'access-7', send: async () => ({})});

    assert.equal(await reopenedShell.text(), shellHtml);
    assert.equal(await reopenedCss.text(), '/* v233 */');
    assert.equal(await reopenedJs.text(), '// v233');
    assert.ok(await currentCache.match('/excavator/work/'));
    assert.ok(await currentCache.match(css));
    assert.ok(await currentCache.match(js));
    assert.equal(await currentCache.match('/static/js/stale.js?v=v233'), undefined);
    assert.equal(await currentCache.match('/static/js/poison.js'), undefined);
    assert.equal((await after.ready()).length, 1);
});

test('incomplete previous shell rejects install before old cache deletion or activation', async () => {
    const missing = '/static/js/missing.js?v=excavator-mobile-shell-v233';
    const shellHtml = `<main data-eo-shell data-eo-role-code="excavator_operator"><script src="${missing}"></script></main>`;
    const oldCache = fakeCache([
        ['/excavator/work/', new FakeResponse(shellHtml, {url: 'https://excavator.test/excavator/work/', type: 'text/html'})],
    ]);
    const currentCache = fakeCache();
    const cacheMap = new Map([
        ['excavator-mobile-shell-v233', oldCache],
        ['excavator-mobile-shell-v234', currentCache],
    ]);
    const listeners = {};
    let skippedWaiting = false;
    let claimedClients = false;
    const context = {
        URL, Set, Promise, Request: FakeRequest, Response: FakeResponse,
        fetch: async () => new FakeResponse('<form>expired</form>', {url: 'https://excavator.test/login/', type: 'text/html'}),
        caches: {open: async name => cacheMap.get(name), keys: async () => [...cacheMap.keys()], delete: async name => cacheMap.delete(name)},
        self: {
            location: {origin: 'https://excavator.test'},
            addEventListener: (name, fn) => { listeners[name] = fn; },
            clients: {claim: async () => { claimedClients = true; }},
            skipWaiting: async () => { skippedWaiting = true; },
        },
        setTimeout, clearTimeout,
    };
    vm.createContext(context);
    vm.runInContext(workerSource, context);

    let installWork;
    listeners.install({waitUntil: promise => { installWork = promise; }});
    await assert.rejects(installWork, /Authenticated excavator shell is unavailable/);

    assert.equal(cacheMap.has('excavator-mobile-shell-v233'), true);
    assert.equal(await currentCache.match('/excavator/work/'), undefined);
    assert.equal(skippedWaiting, false);
    assert.equal(claimedClients, false);
});
