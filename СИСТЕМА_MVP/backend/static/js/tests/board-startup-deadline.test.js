'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = process.env.BOARD_STARTUP_TEST_ROOT || path.resolve(__dirname, '../../..');
const origin = 'https://mine.test';
const roles = [
    {code: 'mining_master', file: 'assignments/views.py', constant: 'MINING_MASTER_SERVICE_WORKER_JS', shell: '/mining-master/assignments/'},
    {code: 'dispatcher', file: 'trips/dispatcher_pwa.py', constant: 'DISPATCHER_SERVICE_WORKER_JS', shell: '/dispatcher/control/'},
];
function raw(file, name) {
    return fs.readFileSync(path.join(root, file), 'utf8')
        .match(new RegExp(name + ' = r"""([\\s\\S]*?)"""'))?.[1] || '';
}
const helper = raw('users/role_apps.py', 'BOARD_SERVICE_WORKER_JS');
const release = raw('users/role_apps.py', 'RELEASE_STATIC_SERVICE_WORKER_JS')
    .replaceAll('__STATIC_ASSET_RELEASE__', 'test-release')
    .replace('__RELEASE_STATIC_PATHS__', '["/static/js/realtime-client.js"]');
const releasePath = '/static/js/realtime-client.js?v=test-release';
function boardHTML(role) {
    return `<main class="dispatcher-shell" data-dispatcher-command-role="${role.code}">Fresh board</main><script src="${releasePath}"></script>`;
}
function response(body = 'Saved board', url = '/', status = 200, type = 'text/html') {
    const r = new Response(body, {status, headers: {'Content-Type': type}});
    Object.defineProperty(r, 'url', {value: origin + url});
    const clone = r.clone.bind(r);
    r.clone = () => { const c = clone(); Object.defineProperty(c, 'url', {value: origin + url}); return c; };
    return r;
}
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; }
async function flush() { for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve)); }
function harness(role, fetcher, options = {}) {
    const timers = new Map(), entries = new Map(), handlers = {}, writes = [], waits = [];
    let next = 0, requests = 0, aborted = false;
    const key = r => typeof r === 'string' ? new URL(r, origin).href : r.url;
    if (options.cached !== false) entries.set(origin + role.shell, response('Saved board', role.shell));
    const cache = {
        match: r => options.hangMatch ? new Promise(() => {}) : Promise.resolve(entries.get(key(r)) || null),
        put: async (r, value) => { writes.push(key(r)); entries.set(key(r), value); },
    };
    const context = {
        Response, Request, URL, AbortController, console,
        setTimeout(fn, delay) { const id = ++next; timers.set(id, {fn, delay}); return id; },
        clearTimeout(id) { timers.delete(id); },
        self: {location: {origin}, navigator: {onLine: options.online !== false}, addEventListener(name, fn) { handlers[name] = fn; }},
        caches: {open: () => options.cacheOpen ? options.cacheOpen() : options.hangCache ? new Promise(() => {}) : Promise.resolve(cache)},
        fetch: async (request, init) => {
            requests++;
            if (init?.signal) init.signal.addEventListener('abort', () => { aborted = true; });
            return fetcher(request, init);
        },
    };
    // Same raw helpers and fetch routing used by add_release_static_cache;
    // Django endpoint tests also check the assembled response uses this route.
    const worker = raw(role.file, role.constant).replace(
        'event.respondWith(networkFirstStatic(request));',
        'event.respondWith(isReleaseStaticRequest(url) ? boardCacheFirstReleaseStatic(request, event) : networkFirstStatic(request));'
    );
    vm.createContext(context);
    vm.runInContext(release + '\n' + helper + '\n' + worker, context);
    return {context, entries, cache, writes, waits, timers,
        get requests() { return requests; }, get aborted() { return aborted; },
        fire(delay) { for (const [id, timer] of [...timers]) if (timer.delay === delay) { timers.delete(id); timer.fn(); } },
        start(url = role.shell, headers = {}) {
            const request = new Request(origin + url, {headers});
            let result;
            handlers.fetch({request, respondWith(value) { result = value; }, waitUntil(value) { waits.push(value); }});
            return result;
        },
    };
}
function observe(promise) {
    const state = {settled: false};
    promise.then(value => { state.settled = true; state.value = value; });
    return state;
}
for (const role of roles) {
    const check = (name, run) => test(role.code + ': ' + name, run);
    check('hung headers finish with saved board and release network timer', async () => {
        const h = harness(role, () => new Promise(() => {}));
        const state = observe(h.start()); await flush(); h.fire(8000); await flush();
        assert.equal(state.settled, true);
        assert.equal(await state.value.text(), 'Saved board');
        assert.equal(h.aborted, true); assert.equal(h.timers.size, 0);
    });
    check('hung response body falls back; late completion never writes cache', async () => {
        const body = deferred();
        const hanging = {ok: true, status: 200, url: origin + role.shell,
            clone: () => ({arrayBuffer: () => body.promise})};
        const h = harness(role, () => hanging);
        const state = observe(h.start()); await flush(); h.fire(8000); await flush();
        assert.equal(state.settled, true); assert.equal(await state.value.text(), 'Saved board');
        body.resolve(new ArrayBuffer(0)); await flush(); assert.deepEqual(h.writes, []);
    });
    check('empty cache returns finite 503 and next healthy navigation succeeds', async () => {
        let healthy = false;
        const h = harness(role, request => healthy ? (new URL(request.url).pathname === role.shell
            ? response(boardHTML(role), role.shell) : response('asset', releasePath, 200, 'text/javascript'))
            : new Promise(() => {}), {cached: false});
        const state = observe(h.start()); await flush(); h.fire(8000); await flush();
        assert.equal(state.settled, true); assert.equal(state.value.status, 503);
        healthy = true; assert.equal(await (await h.start()).text(), boardHTML(role));
        await flush(); assert.ok(h.writes.length); assert.equal(h.timers.size, 0);
    });
    for (const failure of ['hangCache', 'hangMatch']) {
        check(failure + ' cannot stall fallback', async () => {
            const h = harness(role, () => Promise.reject(Error('offline')), {[failure]: true});
            const state = observe(h.start()); await flush(); h.fire(2500); await flush();
            assert.equal(state.settled, true); assert.equal(state.value.status, 503); assert.equal(h.timers.size, 0);
        });
    }
    check('hung cached body is bounded too', async () => {
        const h = harness(role, () => Promise.reject(Error('offline')));
        h.entries.set(origin + role.shell, {ok: true, url: origin + role.shell, clone: () => ({arrayBuffer: () => new Promise(() => {})})});
        const state = observe(h.start()); await flush(); h.fire(2500); await flush();
        assert.equal(state.settled, true); assert.equal(state.value.status, 503);
    });
    check('healthy navigation bypasses stalled cache; expired write cannot start later', async () => {
        const opened = deferred();
        const h = harness(role, () => response(boardHTML(role), role.shell), {cacheOpen: () => opened.promise});
        const state = observe(h.start()); await flush();
        assert.equal(state.settled, true); assert.equal(await state.value.text(), boardHTML(role));
        h.fire(2500); await flush(); opened.resolve(h.cache); await flush();
        assert.deepEqual(h.writes, []); assert.equal(h.timers.size, 0);
    });
    check('server failure uses cache, explicit 403 is returned', async () => {
        let status = 503;
        const h = harness(role, () => response('Server response', role.shell, status));
        assert.equal(await (await h.start()).text(), 'Saved board');
        status = 403; assert.equal((await h.start()).status, 403); assert.deepEqual(h.writes, []);
    });
    check('XHR timeout never receives saved HTML; later fresh JSON works', async () => {
        let healthy = false;
        const h = harness(role, () => healthy ? response('{"version":2}', role.shell, 200, 'application/json') : new Promise(() => {}));
        const headers = {'X-Requested-With': 'XMLHttpRequest'};
        const state = observe(h.start(role.shell, headers)); await flush(); h.fire(8000); await flush();
        assert.equal(state.settled, true); assert.equal(state.value.status, 503);
        healthy = true; assert.deepEqual(await (await h.start(role.shell, headers)).json(), {version: 2});
        assert.deepEqual(h.writes, []);
    });
    check('unversioned script uses saved copy after timeout', async () => {
        const h = harness(role, () => new Promise(() => {}));
        h.entries.set(origin + '/static/app.js', response('saved-script', '/static/app.js', 200, 'text/javascript'));
        const state = observe(h.start('/static/app.js')); await flush(); h.fire(8000); await flush();
        assert.equal(state.settled, true); assert.equal(await state.value.text(), 'saved-script');
    });
    check('release script uses validated cache without network', async () => {
        const h = harness(role, () => new Promise(() => {}));
        h.entries.set(origin + releasePath, response('saved-release', releasePath, 200, 'text/javascript'));
        assert.equal(await (await h.start(releasePath)).text(), 'saved-release'); assert.equal(h.requests, 0);
    });
    check('release script escapes stalled cache and then stalled network', async () => {
        const h = harness(role, () => new Promise(() => {}), {hangCache: true});
        const state = observe(h.start(releasePath)); await flush(); h.fire(2500); await flush();
        assert.equal(h.requests, 1); h.fire(8000); await flush();
        assert.equal(state.settled, true); assert.equal(state.value.status, 503); assert.equal(h.timers.size, 0);
    });
    check('release script succeeds through network when cache is unavailable', async () => {
        const h = harness(role, () => response('fresh-release', releasePath, 200, 'text/javascript'), {hangCache: true});
        const state = observe(h.start(releasePath)); await flush(); h.fire(2500); await flush();
        assert.equal(state.settled, true); assert.equal(await state.value.text(), 'fresh-release');
        h.fire(2500); await flush(); assert.equal(h.timers.size, 0);
    });
    check('redirected login is never saved as script or served from script cache', async () => {
        let offline = false;
        const h = harness(role, () => offline ? Promise.reject(Error('offline')) : response('Login', '/'));
        await h.start('/static/app.js'); await flush(); assert.deepEqual(h.writes, []);
        h.entries.set(origin + '/static/app.js', response('Login', '/'));
        offline = true; assert.equal((await h.start('/static/app.js')).status, 503);
    });
    check('wrong release query cannot be served as cached script', async () => {
        const h = harness(role, () => Promise.reject(Error('offline')));
        h.entries.set(origin + releasePath, response('old', '/static/js/realtime-client.js?v=old', 200, 'text/javascript'));
        assert.equal((await h.start(releasePath)).status, 503);
    });
    check('deadline works without AbortController', async () => {
        const h = harness(role, () => new Promise(() => {})); h.context.AbortController = undefined;
        const state = observe(h.start()); await flush(); h.fire(8000); await flush();
        assert.equal(state.settled, true); assert.equal(await state.value.text(), 'Saved board'); assert.equal(h.timers.size, 0);
    });
}
for (const online of [true, false]) test('mining_master: fast saved shell, online=' + online, async () => {
    const h = harness(roles[0], () => new Promise(() => {}), {online});
    const state = observe(h.start()); await flush(); h.fire(online ? 2500 : 0); await flush();
    assert.equal(state.settled, true); assert.equal(await state.value.text(), 'Saved board');
    assert.equal(h.aborted, false); h.fire(8000); await flush();
    await Promise.all(h.waits); assert.equal(h.aborted, true); assert.equal(h.timers.size, 0);
});
