'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = process.env.BOARD_INSTALL_TEST_ROOT || path.resolve(__dirname, '../../..');
const origin = 'https://mine.test';
const roles = [
    {code: 'mining_master', file: 'assignments/views.py', constant: 'MINING_MASTER_SERVICE_WORKER_JS', shell: '/mining-master/assignments/'},
    {code: 'dispatcher', file: 'trips/dispatcher_pwa.py', constant: 'DISPATCHER_SERVICE_WORKER_JS', shell: '/dispatcher/control/'},
];
function raw(file, name) {
    return fs.readFileSync(path.join(root, file), 'utf8')
        .match(new RegExp(name + ' = r"""([\\s\\S]*?)"""'))[1];
}
const helper = raw('users/role_apps.py', 'BOARD_SERVICE_WORKER_JS');
const release = raw('users/role_apps.py', 'RELEASE_STATIC_SERVICE_WORKER_JS')
    .replaceAll('__STATIC_ASSET_RELEASE__', 'test-release')
    .replace('__RELEASE_STATIC_PATHS__', '["/static/js/realtime-client.js","/static/js/connection-indicators-v1.js"]');
function response(body, url, status = 200, type = 'application/javascript') {
    const value = new Response(body, {status, headers: {'Content-Type': type}});
    return attachURL(value, new URL(url, origin).href);
}
function attachURL(value, url) {
    Object.defineProperty(value, 'url', {value: url});
    const clone = value.clone.bind(value);
    value.clone = () => attachURL(clone(), url);
    return value;
}
function shellHTML(role, assets = '<script src="/static/js/mobile-shift-unified-v1.js?v=exact-board"></script><link href="/static/css/app.css?v=exact-board" rel="stylesheet">') {
    return `<html><head>${assets}</head><body><main class="dispatcher-shell" data-dispatcher-command-role="${role.code}"></main></body></html>`;
}
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
    const writes = [], fetchWaits = [], entries = options.entries || new Map();
    const key = request => new URL(typeof request === 'string' ? request : request.url, origin).href;
    const worker = raw(role.file, role.constant).replaceAll('__STATIC_ASSET_RELEASE__', 'test-release');
    const cacheName = worker.match(/const CACHE_NAME = "([^"]+)"/)[1];
    const prefix = worker.match(/const CACHE_PREFIX = "([^"]+)"/)[1];
    const version = Number(cacheName.slice(prefix.length + 1));
    const oldName = prefix + 'v' + (version - 1), futureName = prefix + 'v' + (version + 1);
    const cachesPresent = new Map([[oldName, 'working-old-shell'], [cacheName, 'new-shell'],
        [futureName, 'future-shell'], ['driver-mobile-shell-v382', 'other-role'], [prefix + 'unrecognised', 'unknown']]);
    const cache = {
        addAll(requests) { calls.push('addAll'); batches.push(requests); return options.fetch ? options.fetch(requests[0]) : Promise.resolve(); },
        put(request, value) { writes.push(key(request)); if (options.put) return options.put(request, value); entries.set(key(request), value.clone()); return Promise.resolve(); },
        match(request) { return Promise.resolve(entries.get(key(request))?.clone()); },
    };
    const context = {
        URL, Response, AbortController, console,
        fetch(request, init) {
            calls.push('fetch');
            if (!currentBatch) { currentBatch = []; batches.push(currentBatch); }
            currentBatch.push(request);
            return options.fetch ? options.fetch(request, init) : Promise.resolve(defaultResponse(request));
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
    function defaultResponse(request) {
        const url = new URL(key(request));
        if (url.pathname === role.shell) return response(options.html || shellHTML(role), url.href, 200, 'text/html');
        const type = url.pathname.endsWith('.css') ? 'text/css' : 'application/javascript';
        const body = options.realAssets && url.pathname.startsWith('/static/')
            ? fs.readFileSync(path.join(root, url.pathname.slice(1))) : 'asset:' + url.pathname + url.search;
        return response(body, url.href, 200, type);
    }
    vm.createContext(context); vm.runInContext(options.script || release + '\n' + helper + '\n' + worker, context);
    return {context, cache, entries, fetchWaits, defaultResponse, cacheName, oldName, futureName, cachesPresent, calls, batches, deleted, timers, writes,
        fire(delay) { for (const [id, timer] of [...timers]) if (timer.delay === delay) { timers.delete(id); timer.fn(); } },
        start(url) {
            let result;
            for (const fn of handlers.fetch || []) fn({request: new Request(new URL(url, origin)),
                respondWith(value) { result = value; }, waitUntil(value) { fetchWaits.push(value); }});
            return result;
        },
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
    check('fresh install reopens exact HTML dependencies after a worker restart without network', async () => {
        const h = harness(role); await h.event('install');
        const restarted = harness(role, {entries: h.entries, fetch: () => Promise.reject(Error('offline'))});
        assert.equal(await (await restarted.start(role.shell)).text(), shellHTML(role));
        for (const url of ['/static/js/mobile-shift-unified-v1.js?v=exact-board', '/static/css/app.css?v=exact-board']) {
            const saved = await restarted.start(url);
            assert.equal(saved.status, 200); assert.equal(await saved.text(), 'asset:' + url);
        }
        assert.equal((await restarted.start('/static/js/mobile-shift-unified-v1.js?v=other-board')).status, 503);
        assert.equal(restarted.timers.size, 0);
    });
    check('HTML extraction preserves exact queries and ignores inline code, comments and navigation', async () => {
        const html = shellHTML(role, `
            <script defer src='/static/js/mobile-shift-unified-v1.js?v=one&amp;mode=two#fragment'></script>
            <script src='/static/js/mobile-shift-unified-v1.js?v=one&amp;mode=two'></script>
            <script>const template = '<link rel="stylesheet" href="/static/fake.css">';</script>
            <!-- <script src="/static/comment.js"></script> -->
            <link href='/static/css/app.css?v=two' media='all' rel='stylesheet'>
            <link rel='manifest' href='/app.webmanifest'>
            <a href='https://external.test/'>Help</a>`);
        const h = harness(role, {html}); await h.event('install');
        const urls = h.batches.flat().map(request => request.url);
        assert.equal(urls.filter(url => url === origin + '/static/js/mobile-shift-unified-v1.js?v=one&mode=two').length, 1);
        assert.ok(urls.includes(origin + '/static/css/app.css?v=two'));
        assert.equal(urls.some(url => /fake|comment|external/.test(url)), false);
    });
    for (const failure of ['404', '206', 'HTML', 'redirect', 'wrong query']) {
        check('invalid exact dependency (' + failure + ') cannot replace the working installation', async () => {
            const url = '/static/js/mobile-shift-unified-v1.js?v=exact-board';
            let h;
            h = harness(role, {fetch(request) {
                if (new URL(request.url).pathname !== '/static/js/mobile-shift-unified-v1.js') return Promise.resolve(h.defaultResponse(request));
                if (failure === '404' || failure === '206') return Promise.resolve(response('bad', url, Number(failure)));
                if (failure === 'HTML') return Promise.resolve(response('<html>Login</html>', url, 200, 'text/html'));
                return Promise.resolve(response('wrong', failure === 'redirect' ? '/' : url + '-other'));
            }});
            await assert.rejects(h.event('install'));
            assert.deepEqual(h.writes, []); assert.deepEqual(h.deleted, []);
            assert.equal(h.cachesPresent.get(h.oldName), 'working-old-shell');
            assert.equal(h.calls.includes('skipWaiting'), false); assert.equal(h.timers.size, 0);
        });
    }
    for (const failure of ['login redirect', 'wrong role', 'not HTML', 'no dependencies', 'external dependency']) {
        check('invalid shell (' + failure + ') is not an offline-ready installation', async () => {
            let html = shellHTML(role), url = role.shell, type = 'text/html';
            if (failure === 'login redirect') url = '/';
            if (failure === 'wrong role') html = shellHTML({code: 'driver'});
            if (failure === 'not HTML') type = 'application/json';
            if (failure === 'no dependencies') html = shellHTML(role, '');
            if (failure === 'external dependency') html = shellHTML(role, '<script src="https://external.test/app.js"></script>');
            const h = harness(role, {fetch: () => Promise.resolve(response(html, url, 200, type))});
            await assert.rejects(h.event('install'));
            assert.deepEqual(h.writes, []); assert.equal(h.calls.includes('skipWaiting'), false);
            assert.equal(h.cachesPresent.get(h.oldName), 'working-old-shell'); assert.equal(h.timers.size, 0);
        });
    }
    check('hung exact dependency body expires, cannot write late and next install succeeds', async () => {
        const body = deferred(); let hanging = true, h;
        h = harness(role, {fetch(request) {
            if (hanging && new URL(request.url).pathname === '/static/js/mobile-shift-unified-v1.js') {
                const value = response('asset', request.url);
                value.clone = () => ({arrayBuffer: () => body.promise});
                return Promise.resolve(value);
            }
            return Promise.resolve(h.defaultResponse(request));
        }});
        const state = observe(h.event('install')); await flush();
        assert.equal(state.status, 'pending'); h.fire(30000); await flush();
        assert.equal(state.status, 'rejected'); assert.ok(h.batches[0].every(request => request.signal.aborted));
        body.resolve(new ArrayBuffer(0)); await flush();
        assert.deepEqual(h.writes, []); assert.equal(h.calls.includes('skipWaiting'), false);
        hanging = false; await h.event('install');
        assert.ok(h.entries.has(origin + '/static/js/mobile-shift-unified-v1.js?v=exact-board'));
        assert.equal(h.timers.size, 0);
    });
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

if (process.env.BOARD_RENDERED_SHELL_PATH) {
    test('fresh authenticated rendered shell and every script/style reopen offline', async () => {
        // Django supplies real authenticated HTML, its served worker and a list
        // extracted independently with Python's HTMLParser (not the SW regex).
        const fixture = JSON.parse(fs.readFileSync(process.env.BOARD_RENDERED_SHELL_PATH, 'utf8'));
        const role = roles.find(item => item.code === fixture.role);
        assert.ok(role); assert.ok(fixture.assets.length > 5);
        const options = {html: fixture.html, script: fixture.script, realAssets: true};
        const h = harness(role, options);
        await h.event('install');
        // Repeat with another actual Django rendering and one missing exact
        // dependency: network HTML is returned immediately, offline replacement
        // must wait for the real static file to be restored.
        options.html = fixture.refresh_html;
        const missing = fixture.assets.find(url => url.includes('/js/'));
        h.entries.delete(new URL(missing, origin).href);
        const previousWrites = h.writes.length;
        assert.equal(await (await h.start(role.shell + '?offline-refresh=1')).text(), fixture.refresh_html);
        await flush(); await Promise.all(h.fetchWaits);
        assert.deepEqual(h.writes.slice(previousWrites), [new URL(missing, origin).href, origin + role.shell]);
        const cold = harness(role, {entries: h.entries, script: fixture.script, fetch: () => Promise.reject(Error('offline'))});
        assert.equal(await (await cold.start(role.shell)).text(), fixture.refresh_html);
        for (const asset of fixture.assets) {
            const url = new URL(asset, origin);
            const result = await cold.start(url.href);
            assert.equal(result.status, 200, asset);
            assert.deepEqual(Buffer.from(await result.arrayBuffer()), fs.readFileSync(path.join(root, url.pathname.slice(1))), asset);
        }
        assert.equal(cold.timers.size, 0);
        console.log(fixture.role + ': exact offline scripts/styles = ' + fixture.assets.length);
    });
}
