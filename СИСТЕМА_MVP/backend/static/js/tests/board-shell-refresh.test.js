'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = process.env.BOARD_REFRESH_TEST_ROOT || path.resolve(__dirname, '../../..');
const origin = 'https://mine.test';
const roles = [
    {code: 'mining_master', file: 'assignments/views.py', constant: 'MINING_MASTER_SERVICE_WORKER_JS', shell: '/mining-master/assignments/'},
    {code: 'dispatcher', file: 'trips/dispatcher_pwa.py', constant: 'DISPATCHER_SERVICE_WORKER_JS', shell: '/dispatcher/control/'},
];
function raw(file, name) {
    return fs.readFileSync(path.join(root, file), 'utf8').match(new RegExp(name + ' = r"""([\\s\\S]*?)"""'))[1];
}
const helper = raw('users/role_apps.py', 'BOARD_SERVICE_WORKER_JS');
const release = raw('users/role_apps.py', 'RELEASE_STATIC_SERVICE_WORKER_JS')
    .replaceAll('__STATIC_ASSET_RELEASE__', 'test-release').replace('__RELEASE_STATIC_PATHS__', '[]');
const key = value => new URL(typeof value === 'string' ? value : value.url, origin).href;
const assets = version => ['/static/js/board.js?v=' + version, '/static/css/board.css?v=' + version];
function html(role, version) {
    const [js, css] = assets(version);
    return `<link rel="stylesheet" href="${css}"><main class="dispatcher-shell" data-dispatcher-command-role="${role.code}">${version}</main><script src="${js}"></script>`;
}
function withURL(value, url) {
    Object.defineProperty(value, 'url', {value: key(url)});
    const clone = value.clone.bind(value);
    value.clone = () => withURL(clone(), url);
    return value;
}
function response(body, url, status = 200, type = 'text/html') {
    return withURL(new Response(body, {status, headers: {'Content-Type': type}}), url);
}
function assetResponse(url, status = 200) {
    return response('asset:' + key(url), url, status, key(url).includes('.css') ? 'text/css' : 'text/javascript');
}
function deferred() { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return {promise, resolve, reject}; }
async function flush() { for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve)); }
function harness(role, options = {}) {
    const entries = options.entries || new Map(), timers = new Map(), waits = [], writes = [], fetches = [];
    const handlers = {}; let nextTimer = 0;
    if (!options.entries) {
        entries.set(key(role.shell), response(html(role, 'old'), role.shell));
        for (const url of assets('old')) entries.set(key(url), assetResponse(url));
    }
    const cache = {
        match(request) {
            return options.match ? options.match(request) : Promise.resolve(entries.get(key(request))?.clone());
        },
        put(request, value) {
            writes.push(key(request));
            return options.put ? options.put(request, value) : save(request, value);
        },
    };
    function save(request, value) { entries.set(key(request), value.clone()); return Promise.resolve(); }
    const context = {
        URL, Request, Response, AbortController, console,
        setTimeout(fn, delay) { const id = ++nextTimer; timers.set(id, {fn, delay}); return id; },
        clearTimeout(id) { timers.delete(id); },
        self: {location: {origin}, navigator: {onLine: true}, addEventListener(name, fn) { handlers[name] = fn; }},
        caches: {open: () => options.open ? options.open() : Promise.resolve(cache)},
        fetch(request, init) {
            fetches.push({url: key(request), init});
            if (options.fetch) return Promise.resolve(options.fetch(request, init));
            return Promise.resolve(new URL(key(request)).pathname === role.shell
                ? response(html(role, 'new'), key(request)) : assetResponse(key(request)));
        },
    };
    const worker = raw(role.file, role.constant).replaceAll('__STATIC_ASSET_RELEASE__', 'test-release');
    vm.createContext(context); vm.runInContext(release + '\n' + helper + '\n' + worker, context);
    return {context, entries, timers, waits, writes, fetches, cache, save,
        start(url = role.shell, headers = {}) {
            let result;
            handlers.fetch({request: new Request(key(url), {headers}), respondWith(value) { result = value; }, waitUntil(value) { waits.push(value); }});
            return result;
        },
        async settled() { await flush(); await Promise.all(waits); await flush(); },
        fire(delay) { for (const [id, timer] of [...timers]) if (timer.delay === delay) { timers.delete(id); timer.fn(); } },
        savedHTML() { return entries.get(key(role.shell)).clone().text(); },
    };
}
for (const role of roles) {
    const check = (name, run) => test(role.code + ': ' + name, run);
    check('fresh network HTML stays responsive; old offline shell remains until every dependency is saved', async () => {
        const asset = deferred();
        const h = harness(role, {fetch: request => new URL(key(request)).pathname === role.shell
            ? response(html(role, 'new'), key(request)) : asset.promise.then(() => assetResponse(key(request)))});
        // Consume the live response before any asynchronous cache preparation.
        assert.equal(await (await h.start(role.shell + '?view=latest')).text(), html(role, 'new'));
        await flush(); assert.equal(await h.savedHTML(), html(role, 'old'));
        asset.resolve(); await h.settled();
        assert.equal(await h.savedHTML(), html(role, 'new'));
        assert.equal(h.writes.at(-1), key(role.shell));
        assert.equal(h.entries.has(key(role.shell + '?view=latest')), false);
        const cold = harness(role, {entries: h.entries, fetch: () => Promise.reject(Error('offline'))});
        assert.equal(await (await cold.start()).text(), html(role, 'new'));
        for (const url of assets('new')) assert.equal(await (await cold.start(url)).text(), 'asset:' + key(url));
    });
    check('valid exact dependencies are reused without downloading or overwriting them', async () => {
        const h = harness(role);
        for (const url of assets('new')) h.entries.set(key(url), assetResponse(url));
        await h.start(); await h.settled();
        assert.equal(await h.savedHTML(), html(role, 'new'));
        assert.equal(h.fetches.length, 1); assert.deepEqual(h.writes, [key(role.shell)]);
    });
    for (const failure of ['404', '206', 'HTML', 'redirect', 'wrong query']) {
        check('invalid refresh dependency (' + failure + ') preserves old HTML and a healthy retry succeeds', async () => {
            let broken = true;
            const h = harness(role, {fetch(request) {
                const url = key(request);
                if (new URL(url).pathname === role.shell) return response(html(role, 'new'), url);
                if (!broken) return assetResponse(url);
                if (failure === '404' || failure === '206') return assetResponse(url, Number(failure));
                if (failure === 'HTML') return response('Login', url);
                return assetResponse(failure === 'redirect' ? '/' : url + '-wrong');
            }});
            await h.start(); await h.settled();
            assert.equal(await h.savedHTML(), html(role, 'old')); assert.equal(h.writes.includes(key(role.shell)), false);
            broken = false; await h.start(); await h.settled();
            assert.equal(await h.savedHTML(), html(role, 'new')); assert.equal(h.timers.size, 0);
        });
    }
    check('HTML cached under a script key is repaired before committing the new shell', async () => {
        const h = harness(role);
        for (const url of assets('new')) h.entries.set(key(url), response('Login', url));
        await h.start(); await h.settled();
        assert.equal(await h.savedHTML(), html(role, 'new'));
        for (const url of assets('new')) assert.equal(await h.entries.get(key(url)).clone().text(), 'asset:' + key(url));
    });
    check('dependency quota failure never replaces old HTML; the next attempt recovers', async () => {
        let broken = true, h;
        h = harness(role, {put: (request, value) => broken ? Promise.reject(Error('QuotaExceededError')) : h.save(request, value)});
        await h.start(); await h.settled();
        assert.equal(await h.savedHTML(), html(role, 'old')); assert.equal(h.writes.includes(key(role.shell)), false);
        broken = false; await h.start(); await h.settled(); assert.equal(await h.savedHTML(), html(role, 'new'));
    });
    for (const phase of ['headers', 'body']) {
        check('hung dependency ' + phase + ' expires without a late HTML replacement', async () => {
            const pending = deferred(); let healthy = false;
            const h = harness(role, {fetch(request) {
                const url = key(request);
                if (new URL(url).pathname === role.shell) return response(html(role, 'new'), url);
                if (healthy) return assetResponse(url);
                if (phase === 'headers') return pending.promise.then(() => assetResponse(url));
                const value = assetResponse(url); value.clone = () => ({arrayBuffer: () => pending.promise}); return value;
            }});
            await h.start(); await flush(); h.fire(8000); await h.settled();
            assert.equal(await h.savedHTML(), html(role, 'old'));
            assert.ok(h.fetches.filter(item => item.url.includes('/static/')).every(item => item.init.signal.aborted));
            pending.resolve(new ArrayBuffer(0)); await flush(); assert.equal(h.writes.includes(key(role.shell)), false);
            healthy = true; await h.start(); await h.settled(); assert.equal(await h.savedHTML(), html(role, 'new'));
        });
    }
    for (const phase of ['open', 'match', 'cached body', 'asset write']) {
        check('hung cache ' + phase + ' is bounded and cannot commit HTML after timeout', async () => {
            const pending = deferred(); let h;
            const options = {};
            if (phase === 'open') options.open = () => pending.promise;
            if (phase === 'match') options.match = () => pending.promise;
            if (phase === 'cached body') options.match = request => {
                const value = assetResponse(key(request)); value.clone = () => ({arrayBuffer: () => pending.promise}); return Promise.resolve(value);
            };
            if (phase === 'asset write') options.put = (request, value) => pending.promise.then(() => h.save(request, value));
            h = harness(role, options); await h.start(); await flush(); h.fire(2500); await h.settled();
            assert.equal(await h.savedHTML(), html(role, 'old'));
            pending.resolve(phase === 'open' ? h.cache : undefined); await flush();
            assert.equal(h.writes.includes(key(role.shell)), false); assert.equal(h.timers.size, 0);
        });
    }
    check('outer refresh deadline also blocks late writes', async () => {
        const pending = deferred();
        const h = harness(role, {fetch: request => new URL(key(request)).pathname === role.shell
            ? response(html(role, 'new'), key(request)) : pending.promise.then(() => assetResponse(key(request)))});
        await h.start(); await flush(); h.fire(30000); await h.settled();
        pending.resolve(); await flush();
        assert.equal(await h.savedHTML(), html(role, 'old')); assert.equal(h.writes.length, 0); assert.equal(h.timers.size, 0);
    });
    check('an older HTTP response arriving after the newer response cannot revert the saved shell', async () => {
        const first = deferred(); let navigations = 0;
        const h = harness(role, {fetch(request) {
            if (new URL(key(request)).pathname !== role.shell) return assetResponse(key(request));
            return ++navigations === 1 ? first.promise : response(html(role, 'newest'), role.shell);
        }});
        const old = h.start(); await flush(); await h.start(); await flush();
        first.resolve(response(html(role, 'older'), role.shell)); await old; await h.settled();
        assert.equal(await h.savedHTML(), html(role, 'newest'));
        assert.equal(h.fetches.some(item => item.url.includes('v=older')), false);
    });
    check('an older dependency finishing late cannot revert a newer complete shell', async () => {
        const firstAssets = deferred(); let navigations = 0;
        const h = harness(role, {fetch(request) {
            const url = key(request);
            if (new URL(url).pathname === role.shell) return response(html(role, ++navigations === 1 ? 'older' : 'newest'), role.shell);
            return url.includes('v=older') ? firstAssets.promise.then(() => assetResponse(url)) : assetResponse(url);
        }});
        await h.start(); await flush(); assert.equal(await h.savedHTML(), html(role, 'old'));
        await h.start(); await flush();
        firstAssets.resolve(); await h.settled(); assert.equal(await h.savedHTML(), html(role, 'newest'));
        assert.equal(h.writes.some(url => url.includes('v=older')), false);
    });
    check('HTML writes never overlap even when a previous Cache.put exceeded its deadline', async () => {
        const firstWrite = deferred(); let htmlWrites = 0, navigation = 0, h;
        h = harness(role, {
            fetch: request => new URL(key(request)).pathname === role.shell
                ? response(html(role, ++navigation === 1 ? 'older' : 'newest'), role.shell) : assetResponse(key(request)),
            put(request, value) {
                if (key(request) === key(role.shell) && ++htmlWrites === 1) return firstWrite.promise.then(() => h.save(request, value));
                return h.save(request, value);
            },
        });
        await h.start(); await flush(); h.fire(2500); await h.settled();
        await h.start(); await flush(); assert.equal(htmlWrites, 1);
        firstWrite.resolve(); await h.settled();
        assert.equal(htmlWrites, 2); assert.equal(await h.savedHTML(), html(role, 'newest')); assert.equal(h.timers.size, 0);
    });
    check('waiting for a stuck HTML write is finite and a later navigation recovers', async () => {
        const pending = deferred(); let first = true, h;
        h = harness(role, {put(request, value) {
            if (key(request) === key(role.shell) && first) { first = false; return pending.promise.then(() => h.save(request, value)); }
            return h.save(request, value);
        }});
        await h.start(); await flush(); h.fire(2500); await h.settled();
        await h.start(); await flush(); h.fire(2500); await h.settled();
        assert.equal(h.writes.filter(url => url === key(role.shell)).length, 1);
        pending.resolve(); await flush();
        await h.start(); await h.settled();
        assert.equal(h.writes.filter(url => url === key(role.shell)).length, 2); assert.equal(h.timers.size, 0);
    });
    check('query navigation uses the canonical ready shell instead of an older query entry', async () => {
        const h = harness(role);
        h.entries.set(key(role.shell + '?view=latest'), response(html(role, 'stale-query'), role.shell + '?view=latest'));
        await h.start(); await h.settled();
        const cold = harness(role, {entries: h.entries, fetch: () => Promise.reject(Error('offline'))});
        assert.equal(await (await cold.start(role.shell + '?view=latest')).text(), html(role, 'new'));
    });
    for (const invalid of ['wrong role', 'login redirect', 'JSON fragment']) {
        check(invalid + ' cannot replace the authenticated offline shell', async () => {
            const h = harness(role, {fetch: () => invalid === 'wrong role'
                ? response(html({code: 'driver'}, 'wrong'), role.shell) : invalid === 'login redirect'
                    ? response('Login', '/') : response('{}', role.shell, 200, 'application/json')});
            await h.start(); await h.settled(); assert.equal(await h.savedHTML(), html(role, 'old'));
            assert.equal(h.writes.length, 0);
        });
    }
    check('XHR bypasses shell preparation and caching', async () => {
        const h = harness(role); await h.start(role.shell, {'X-Requested-With': 'XMLHttpRequest'}); await h.settled();
        assert.equal(h.fetches.length, 1); assert.equal(h.writes.length, 0);
    });
}
