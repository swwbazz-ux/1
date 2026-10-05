'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = process.env.BOARD_INSTALL_TEST_ROOT || path.resolve(__dirname, '../../..');
const origin = 'https://mine.test';
const roles = [
    {code: 'mining_master', file: 'assignments/views.py', constant: 'MINING_MASTER_SERVICE_WORKER_JS'},
    {code: 'dispatcher', file: 'trips/dispatcher_pwa.py', constant: 'DISPATCHER_SERVICE_WORKER_JS'},
];
function raw(file, name) {
    return fs.readFileSync(path.join(root, file), 'utf8')
        .match(new RegExp(name + ' = r"""([\\s\\S]*?)"""'))[1];
}
const helper = raw('users/role_apps.py', 'BOARD_SERVICE_WORKER_JS');
const release = raw('users/role_apps.py', 'RELEASE_STATIC_SERVICE_WORKER_JS')
    .replaceAll('__STATIC_ASSET_RELEASE__', 'test-release')
    .replace('__RELEASE_STATIC_PATHS__', '["/static/js/realtime-client.js","/static/js/connection-indicators-v1.js"]');
function deferred() { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return {promise, resolve, reject}; }
async function flush() { for (let i = 0; i < 4; i++) await new Promise(resolve => setImmediate(resolve)); }
function observe(promise) {
    const state = {status: 'pending'};
    promise.then(value => { state.status = 'resolved'; state.value = value; }, error => { state.status = 'rejected'; state.error = error; });
    return state;
}
function harness(role, options = {}) {
    const handlers = {}, timers = new Map(), calls = [], batches = [], deleted = [];
    let sequence = 0, currentBatch = null;
    const writes = [];
    const worker = raw(role.file, role.constant).replaceAll('__STATIC_ASSET_RELEASE__', 'test-release');
    const cacheName = worker.match(/const CACHE_NAME = "([^"]+)"/)[1];
    const prefix = worker.match(/const CACHE_PREFIX = "([^"]+)"/)[1];
    const version = Number(cacheName.slice(prefix.length + 1));
    const oldName = prefix + 'v' + (version - 1), futureName = prefix + 'v' + (version + 1);
    const cachesPresent = new Map([[oldName, 'working-old-shell'], [cacheName, 'new-shell'],
        [futureName, 'future-shell'], ['driver-mobile-shell-v382', 'other-role'], [prefix + 'unrecognised', 'unknown']]);
    const cache = {
        addAll(requests) { calls.push('addAll'); batches.push(requests); return options.fetch ? options.fetch(requests[0]) : Promise.resolve(); },
        put(request, response) { writes.push(request.url); return options.put ? options.put(request, response) : Promise.resolve(); },
    };
    const context = {
        URL, Response, AbortController, console,
        fetch(request, init) {
            calls.push('fetch');
            if (!currentBatch) { currentBatch = []; batches.push(currentBatch); }
            currentBatch.push(request);
            return options.fetch ? options.fetch(request, init) : Promise.resolve(new Response('asset'));
        },
        Request: class extends Request { constructor(url, init) { super(new URL(url, origin), init); } },
        setTimeout(fn, delay) { const id = ++sequence; timers.set(id, {fn, delay}); return id; },
        clearTimeout(id) { timers.delete(id); },
        self: {
            location: {origin},
            addEventListener(name, fn) { (handlers[name] ||= []).push(fn); },
            skipWaiting() { calls.push('skipWaiting'); return options.skipWaiting ? options.skipWaiting() : Promise.resolve(); },
            clients: {claim() { calls.push('claim'); return options.claim ? options.claim() : Promise.resolve(); }},
        },
        caches: {
            open(name) { calls.push('open:' + name); return options.open ? options.open(name) : Promise.resolve(cache); },
            keys() { calls.push('keys'); return options.keys ? options.keys() : Promise.resolve([...cachesPresent.keys()]); },
            delete(name) { calls.push('delete:' + name); deleted.push(name); return options.delete ? options.delete(name) : Promise.resolve(cachesPresent.delete(name)); },
        },
    };
    vm.createContext(context); vm.runInContext(release + '\n' + helper + '\n' + worker, context);
    return {context, cache, cacheName, oldName, futureName, cachesPresent, calls, batches, deleted, timers, writes,
        fire(delay) { for (const [id, timer] of [...timers]) if (timer.delay === delay) { timers.delete(id); timer.fn(); } },
        event(name, detail = {}) {
            if (name === 'install') currentBatch = null;
            const waits = [];
            for (const fn of handlers[name] || []) fn({...detail, waitUntil(promise) { waits.push(promise); }});
            return Promise.all(waits);
        },
    };
}
for (const role of roles) {
    const check = (name, run) => test(role.code + ': ' + name, run);
    check('hung cache open rejects install and late cache cannot start download', async () => {
        const opened = deferred(); const h = harness(role, {open: () => opened.promise});
        const state = observe(h.event('install')); await flush(); h.fire(2500); await flush();
        assert.equal(state.status, 'rejected');
        opened.resolve(h.cache); await flush();
        assert.equal(h.batches.length, 0); assert.equal(h.calls.includes('skipWaiting'), false);
        assert.equal(h.cachesPresent.get(h.oldName), 'working-old-shell'); assert.deepEqual(h.deleted, []);
        assert.equal(h.timers.size, 0);
    });
    check('hung asset batch rejects install and aborts every request', async () => {
        const batch = deferred(); const h = harness(role, {fetch: () => batch.promise});
        const state = observe(h.event('install')); await flush(); h.fire(30000); await flush();
        assert.equal(state.status, 'rejected'); assert.ok(h.batches[0].every(request => request.signal.aborted));
        batch.resolve(new Response('late asset')); await flush();
        assert.deepEqual(h.writes, []);
        assert.equal(h.calls.includes('skipWaiting'), false); assert.equal(h.calls.includes('claim'), false);
        assert.equal(h.cachesPresent.get(h.oldName), 'working-old-shell'); assert.deepEqual(h.deleted, []);
        assert.equal(h.timers.size, 0);
    });
    check('headers with a hung body cannot finish install or write late', async () => {
        const body = deferred();
        const h = harness(role, {fetch: () => Promise.resolve({ok: true, status: 200,
            clone: () => ({arrayBuffer: () => body.promise})})});
        const state = observe(h.event('install')); await flush(); h.fire(30000); await flush();
        assert.equal(state.status, 'rejected');
        body.resolve(new ArrayBuffer(0)); await flush();
        assert.deepEqual(h.writes, []); assert.equal(h.calls.includes('skipWaiting'), false);
        assert.ok(h.batches[0].every(request => request.signal.aborted)); assert.equal(h.timers.size, 0);
    });
    check('hung candidate writes cannot promote update or erase old cache', async () => {
        const write = deferred(); const h = harness(role, {put: () => write.promise});
        const state = observe(h.event('install')); await flush();
        assert.ok(h.writes.length); h.fire(30000); await flush();
        assert.equal(state.status, 'rejected'); write.resolve(); await flush();
        assert.equal(h.calls.includes('skipWaiting'), false); assert.deepEqual(h.deleted, []);
        assert.equal(h.cachesPresent.get(h.oldName), 'working-old-shell'); assert.equal(h.timers.size, 0);
    });
    check('failed asset batch cannot promote incomplete update', async () => {
        const h = harness(role, {fetch: () => Promise.resolve(new Response('unavailable', {status: 503}))});
        const state = observe(h.event('install')); await flush();
        assert.equal(state.status, 'rejected'); assert.equal(h.calls.includes('skipWaiting'), false);
        assert.ok(h.batches[0].every(request => request.signal.aborted));
        assert.equal(h.cachesPresent.get(h.oldName), 'working-old-shell'); assert.deepEqual(h.deleted, []);
    });
    check('quota failure leaves old shell and a later attempt can install', async () => {
        let failed = true;
        const h = harness(role, {put: () => failed ? Promise.reject(Error('QuotaExceededError')) : Promise.resolve()});
        const first = observe(h.event('install')); await flush(); assert.equal(first.status, 'rejected');
        assert.equal(h.cachesPresent.get(h.oldName), 'working-old-shell'); failed = false;
        const second = observe(h.event('install')); await flush(); assert.equal(second.status, 'resolved');
        assert.equal(h.batches.length, 2); assert.deepEqual(h.deleted, []); assert.equal(h.timers.size, 0);
    });
    check('core and release assets are all required and fetched once per exact URL', async () => {
        const h = harness(role); await h.event('install');
        assert.equal(h.writes.length, h.batches[0].length);
        assert.equal(h.batches.length, 1);
        const urls = h.batches[0].map(request => request.url);
        assert.equal(new Set(urls).size, urls.length);
        for (const file of ['realtime-client.js', 'connection-indicators-v1.js']) {
            assert.ok(urls.includes(origin + '/static/js/' + file + '?v=test-release'));
        }
        assert.ok(h.batches[0].every(request => request.cache === 'reload'));
        assert.equal(h.calls.includes('skipWaiting'), role.code === 'dispatcher');
        assert.deepEqual(h.deleted, []); assert.equal(h.timers.size, 0);
    });
    check('install still has a finite failure without AbortController', async () => {
        const h = harness(role, {fetch: () => new Promise(() => {})}); h.context.AbortController = undefined;
        const state = observe(h.event('install')); await flush(); h.fire(30000); await flush();
        assert.equal(state.status, 'rejected'); assert.equal(h.cachesPresent.get(h.oldName), 'working-old-shell');
        assert.equal(h.timers.size, 0);
    });
    check('activation claims first and removes only older caches of its own role', async () => {
        const h = harness(role); await h.event('activate');
        assert.equal(h.calls[0], 'claim'); assert.deepEqual(h.deleted, [h.oldName]);
        assert.equal(h.cachesPresent.get(h.futureName), 'future-shell');
        assert.equal(h.cachesPresent.get(h.cacheName), 'new-shell');
        assert.equal(h.cachesPresent.get('driver-mobile-shell-v382'), 'other-role'); assert.equal(h.timers.size, 0);
    });
    check('hung claim does not stall activation or start cache cleanup later', async () => {
        const claim = deferred(); const h = harness(role, {claim: () => claim.promise});
        const state = observe(h.event('activate')); await flush(); h.fire(2500); await flush();
        assert.equal(state.status, 'resolved'); claim.resolve(); await flush();
        assert.equal(h.calls.includes('keys'), false); assert.deepEqual(h.deleted, []); assert.equal(h.timers.size, 0);
    });
    check('rejected claim preserves old cache and activation completes', async () => {
        const h = harness(role, {claim: () => Promise.reject(Error('claim failed'))});
        await h.event('activate'); assert.deepEqual(h.deleted, []); assert.equal(h.timers.size, 0);
    });
    check('hung cache listing does not stall activation; late list cannot delete caches', async () => {
        const keys = deferred(); const h = harness(role, {keys: () => keys.promise});
        const state = observe(h.event('activate')); await flush(); h.fire(2500); await flush();
        assert.equal(state.status, 'resolved'); keys.resolve([...h.cachesPresent.keys()]); await flush();
        assert.deepEqual(h.deleted, []); assert.equal(h.timers.size, 0);
    });
    check('hung deletion does not stall activation', async () => {
        const h = harness(role, {delete: () => new Promise(() => {})});
        const state = observe(h.event('activate')); await flush(); h.fire(2500); await flush();
        assert.equal(state.status, 'resolved'); assert.deepEqual(h.deleted, [h.oldName]); assert.equal(h.timers.size, 0);
    });
    check('cleanup errors do not reject a ready worker', async () => {
        const h = harness(role, {keys: () => Promise.reject(Error('storage unavailable'))});
        await h.event('activate'); assert.equal(h.calls.includes('claim'), true); assert.equal(h.timers.size, 0);
    });
}
test('Master stays waiting after install and skips only on the explicit message', async () => {
    const h = harness(roles[0]); await h.event('install');
    assert.equal(h.calls.includes('skipWaiting'), false);
    await h.event('message', {data: {type: 'SKIP_WAITING'}});
    assert.equal(h.calls.filter(call => call === 'skipWaiting').length, 1);
});
test('Dispatcher hung skipWaiting has a finite installation failure', async () => {
    const h = harness(roles[1], {skipWaiting: () => new Promise(() => {})});
    const state = observe(h.event('install')); await flush(); h.fire(2500); await flush();
    assert.equal(state.status, 'rejected'); assert.deepEqual(h.deleted, []); assert.equal(h.timers.size, 0);
});
