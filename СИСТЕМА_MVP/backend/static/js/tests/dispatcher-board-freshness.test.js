"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const BACKEND = path.resolve(__dirname, "..", "..", "..");
const SOURCE = fs.readFileSync(
    path.join(BACKEND, "static", "js", "dispatcher-transport-v1.js"),
    "utf8"
);
const QUEUE_KEY = "mining-master-mobile-sync-queue-v3";

function createRuntime(options = {}) {
    const storage = new Map(Object.entries(options.storage || {}));
    const timers = new Map();
    const fetchCalls = [];
    let nextTimerId = 1;
    const context = {
        console,
        Date,
        Error,
        JSON,
        Math,
        Object,
        Promise,
        FormData: class FormDataStub {
            constructor() { this.values = []; }
            append(key, value) { this.values.push([key, value]); }
        },
        localStorage: {
            get length() { return storage.size; },
            key(index) { return Array.from(storage.keys())[index] || null; },
            getItem(key) { return storage.has(key) ? storage.get(key) : null; },
            setItem(key, value) {
                if (options.storageFails && options.storageFails(key, value)) throw new Error("QuotaExceededError");
                storage.set(key, String(value));
            },
            removeItem(key) { storage.delete(key); },
        },
        setTimeout(callback, delay) {
            const id = nextTimerId++;
            timers.set(id, {callback, delay});
            return id;
        },
        clearTimeout(id) { timers.delete(id); },
        fetch(url, fetchOptions) {
            fetchCalls.push({url, options: fetchOptions});
            if (typeof options.fetch === "function") {
                return options.fetch(url, fetchOptions);
            }
            return Promise.resolve({
                ok: true,
                json: () => Promise.resolve({ok: true}),
            });
        },
        isAppRoleReadonly: () => Boolean(options.readonly),
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(SOURCE, context, {filename: "dispatcher-transport-v1.js"});
    return {context, storage, timers, fetchCalls};
}

function extractBraceBlock(source, signature, label) {
    const start = source.indexOf(signature);
    assert.notEqual(start, -1, label + ": сигнатура не найдена.");
    const open = source.indexOf("{", start + signature.length);
    assert.notEqual(open, -1, label + ": не найдена открывающая скобка.");
    let depth = 0;
    let quote = "";
    let escaped = false;
    let lineComment = false;
    let blockComment = false;
    for (let index = open; index < source.length; index += 1) {
        const character = source[index];
        const next = source[index + 1] || "";
        if (lineComment) {
            if (character === "\n") lineComment = false;
            continue;
        }
        if (blockComment) {
            if (character === "*" && next === "/") {
                blockComment = false;
                index += 1;
            }
            continue;
        }
        if (quote) {
            if (escaped) escaped = false;
            else if (character === "\\") escaped = true;
            else if (character === quote) quote = "";
            continue;
        }
        if (character === "/" && next === "/") {
            lineComment = true;
            index += 1;
            continue;
        }
        if (character === "/" && next === "*") {
            blockComment = true;
            index += 1;
            continue;
        }
        if (character === "'" || character === '"' || character === "`") {
            quote = character;
            continue;
        }
        if (character === "{") depth += 1;
        else if (character === "}") {
            depth -= 1;
            if (depth === 0) return source.slice(start, index + 1);
        }
    }
    assert.fail(label + ": не найдена закрывающая скобка.");
    return "";
}

const TEMPLATE = fs.readFileSync(path.join(BACKEND, 'templates/trips/dispatcher_control.html'), 'utf8');
const REALTIME = fs.readFileSync(path.join(BACKEND, 'static/js/dispatcher-realtime-v1.js'), 'utf8');
const RECONCILER = fs.readFileSync(path.join(BACKEND, 'static/js/dispatcher-fragment-reconciler-v1.js'), 'utf8');

function boardHarness(role) {
    const r = createRuntime();
    const c = r.context;
    const author = {actor_id: '1', access_id: '2', role, shift_id: '3'};
    const transport = c.createDispatcherTransport({getCommandContext: () => author});
    const selector = role === 'mining_master' ? '.mm-mobile-shell' : '.dispatcher-board';
    const effects = {replace: [], cards: [], versions: [], restored: []};
    const requests = [];
    let active;
    function board(marker) {
        return {marker, dataset: {}, scrollTop: 0, outerHTML: marker, attributes: [],
            classList: {contains: () => false}, getClientRects: () => [{}],
            querySelector: () => null, querySelectorAll: () => [],
            replaceWith(node) { effects.replace.push(node.marker); active = node; }};
    }
    active = board('local');
    c.document = {body: {dataset: {operationalStateVersion: '10'}, classList: {contains: () => false}},
        querySelector: s => s === selector ? active : null};
    c.navigator = {onLine: true};
    c.getComputedStyle = () => ({display: 'block', visibility: 'visible'});
    const session = new Map();
    c.sessionStorage = {getItem: k => session.get(k), setItem: (k,v) => session.set(k,v)};
    c.scrollTo = () => {};
    c.dispatchEvent = () => {};
    c.CustomEvent = function () {};
    c.AppRealtime = {markApplied: v => effects.versions.push(v)};
    c.AppOperationalFragment = {
        request(screen, version) { return new Promise((resolve, reject) => requests.push({screen, version, resolve, reject})); },
        parseRoot: html => board(html)
    };
    let refresh;
    if (role === 'mining_master') {
        Object.assign(c, {
            dispatcherTransport: transport, miningMasterRealtimeLastVersion: 10,
            miningMasterRealtimeStorageKey: 'operational-state-version',
            miningMasterBoardCurrentUpTo: 10, miningMasterBoardStale: false,
            miningMasterMobileRefreshPromise: null, miningMasterMobileRefreshFollowUp: null,
            miningMasterMobileRefreshFollowUpOptions: null,
            miningMasterMobileRefreshRetryTimer: null, MINING_MASTER_REFRESH_RETRY_MAX: 5,
            captureMobileShellState: node => ({scrollTop: node.scrollTop}),
            restoreMobileShellState: (node, state) => effects.restored.push(state.scrollTop),
            bindMiningMasterMobileScreens() {}, refreshMiningMasterUpdateIndicatorFromStorage() {},
            updateDispatcherSyncIndicator() {}, isMobileOperationalRefreshUnsafe: () => transport.boardRefreshToken() === null,
            equipmentCardsNode: {set textContent(value) { effects.cards.push(value); }, get textContent() { return effects.cards.at(-1); }}
        });
        ['markMiningMasterBoardStale()', 'markMiningMasterBoardCurrent(version)',
            'storeMiningMasterRealtimeVersion(version)', 'scheduleMiningMasterRefreshRetry(options, attempt)',
            'refreshMobileBoardFromServer(options)'].forEach(sig => {
            vm.runInContext(extractBraceBlock(TEMPLATE, 'function ' + sig, sig), c);
        });
        refresh = opts => c.refreshMobileBoardFromServer(opts);
    } else {
        vm.runInContext(RECONCILER, c);
        vm.runInContext(REALTIME, c);
        const realtime = c.createDispatcherRealtime({transport,
            setEquipmentCards: data => effects.cards.push(data), getEquipmentCards: () => ({}),
            getDetailLayer: () => null});
        refresh = opts => realtime.refreshBoardFromServer(opts);
    }
    const url = role === 'mining_master' ? '/mining-master/assignments/truck/assign/' : '/dispatcher/control/truck/assign/';
    function command(sender = transport, id = 'next', postOptions) {
        return sender.storePost(url, {client_action_id: id, action: 'assign', truck_id: '7',
            excavator_id: '8', expected_assignment_state_id: '11'}, postOptions);
    }
    function response(index, version = 11, html = 'server') {
        requests[index].resolve({version, html, equipment_cards: {7: {id: 7}}});
    }
    return {...r, author, transport, effects, requests, refresh, command, response, active: () => active,
        version: () => c.document.body.dataset.operationalStateVersion};
}

for (const role of ['mining_master', 'dispatcher']) {
    test(role + ': command and ACK during GET invalidate its response even with an empty queue', async () => {
        const h = boardHarness(role);
        const first = h.refresh({version: 11});
        await h.transport.send(h.command());
        assert.equal(h.transport.readOwnQueue().length, 0);
        h.response(0);
        assert.equal(await first, false);
        assert.equal(h.active().marker, 'local');
        assert.equal(h.version(), '10');
        assert.equal(h.effects.cards.length, 0);
        const next = h.refresh({version: 12});
        h.response(1, 12);
        assert.equal(await next, true);
        assert.equal(h.active().marker, 'server');
        assert.equal(h.version(), '12');
    });

    test(role + ': response arriving with an unsent command cannot replace local state', async () => {
        const h = boardHarness(role);
        const first = h.refresh({version: 11});
        h.command();
        h.response(0);
        assert.equal(await first, false);
        assert.equal(h.effects.replace.length, 0);
        assert.equal(h.version(), '10');
    });

    test(role + ': old structural command still blocks refresh after transport restart', async () => {
        const h = boardHarness(role);
        const command = h.command(h.transport, 'structural', {queueOnNetworkFailure: false});
        const key = h.transport.journalPrefix + encodeURIComponent(command.id);
        const saved = JSON.parse(h.storage.get(key));
        saved.request.createdAt = Date.now() - 60000;
        h.storage.set(key, JSON.stringify(saved));
        const restarted = h.context.createDispatcherTransport({getCommandContext: () => h.author});
        assert.equal(restarted.boardRefreshToken(), null);
        assert.equal(await h.refresh({version: 11}), false);
        assert.equal(h.requests.length, 0);
    });

    test(role + ': another sender completing the command invalidates the in-flight board', async () => {
        const h = boardHarness(role);
        const first = h.refresh({version: 11});
        const other = h.context.createDispatcherTransport({getCommandContext: () => h.author});
        await other.send(h.command(other));
        h.response(0);
        assert.equal(await first, false);
        assert.equal(h.effects.replace.length, 0);
    });

    test(role + ': switching shift or access while GET is in flight rejects the old projection', async () => {
        for (const field of ['actor_id', 'access_id', 'role', 'shift_id']) {
            const h = boardHarness(role);
            const first = h.refresh({version: 11});
            h.author[field] = 'changed';
            h.response(0);
            assert.equal(await first, false, field);
            assert.equal(h.effects.replace.length, 0, field);
        }
    });

    test(role + ': receipt recovery during GET invalidates an unchanged held command snapshot', async () => {
        const h = boardHarness(role);
        const command = h.command();
        const key = h.transport.journalPrefix + encodeURIComponent(command.id);
        const saved = JSON.parse(h.storage.get(key));
        saved.delivery.state = 'held';
        h.storage.set(key, JSON.stringify(saved));
        const first = h.refresh({version: 11});
        h.context.fetch = async () => ({ok: true, json: async () => ({ok: true, status: 'acknowledged',
            receipt: {ok: true, truck_id: 7, assignment_state_id: 12},
            evidence: {client_action_id: 'next', actor_id: '1'}})});
        await h.transport.reconcileReceipt();
        assert.equal(JSON.parse(h.storage.get(key)).delivery.state, 'acknowledged');
        h.response(0);
        assert.equal(await first, false);
        assert.equal(h.effects.cards.length, 0);
    });
}

test('foreign-author pending commands do not block the current board', async () => {
    const h = boardHarness('mining_master');
    const token = h.transport.boardRefreshToken();
    const other = h.context.createDispatcherTransport({getCommandContext: () => ({...h.author, access_id: 'other'})});
    h.command(other);
    assert.equal(h.transport.boardRefreshToken(), token);
    const request = h.refresh({version: 11});
    h.response(0);
    assert.equal(await request, true);
});

test('old-shift commands remain saved without freezing the current shift board', async () => {
    const h = boardHarness('mining_master');
    const token = h.transport.boardRefreshToken();
    const previous = h.context.createDispatcherTransport({getCommandContext: () => ({...h.author, shift_id: 'previous'})});
    const old = h.command(previous);
    assert.equal(h.transport.boardRefreshToken(), token);
    const request = h.refresh({version: 11});
    h.response(0);
    assert.equal(await request, true);
    assert.equal(h.transport.readOwnQueue().length, 1);
    assert.ok(h.storage.has(h.transport.journalPrefix + encodeURIComponent(old.id)));
});

test('unreadable command journal fails closed without fetching or replacing the board', async () => {
    const h = boardHarness('mining_master');
    h.storage.set(h.transport.journalPrefix + 'broken', '{');
    assert.equal(await h.refresh(), false);
    assert.equal(h.requests.length, 0);
    assert.equal(h.transport.getQueueState().storageError, 'storage_unavailable');
});

test('Master rejects regressing, malformed and below-target fragment versions without marking them applied', async () => {
    for (const version of [9, 10, 10.5, -1, 'invalid', null]) {
        const h = boardHarness('mining_master');
        const request = h.refresh({version: 11});
        h.response(0, version);
        assert.equal(await request, false, String(version));
        assert.equal(h.version(), '10');
        assert.equal(h.effects.versions.length, 0);
        assert.equal(h.effects.cards.length, 0);
    }
});

test('Master coalesces follow-ups to the highest target and does not apply an intermediate stale version', async () => {
    const h = boardHarness('mining_master');
    const first = h.refresh({version: 11});
    const second = h.refresh({version: 12});
    const third = h.refresh({version: 14, preserveScreen: true});
    assert.equal(second, third);
    h.response(0, 11, 'old');
    assert.equal(await first, false);
    for (let i = 0; i < 8; i++) await Promise.resolve();
    assert.equal(h.requests[1].version, 14);
    h.active().scrollTop = 67;
    h.response(1, 15, 'new');
    assert.equal(await third, true);
    assert.deepEqual(h.effects.replace, ['new']);
    assert.deepEqual(h.effects.restored, [67]);
    h.context.storeMiningMasterRealtimeVersion(14);
    assert.equal(h.version(), '15');
});

test('Master retries a discarded board after ACK without replaying the original command', async () => {
    const h = boardHarness('mining_master');
    const first = h.refresh({version: 11});
    await h.transport.send(h.command());
    h.response(0);
    assert.equal(await first, false);
    const retry = [...h.timers.values()].find(t => t.delay === 1200);
    assert.ok(retry);
    // The refresh timer has the same delay as the transport timer; execute both.
    for (const [id, timer] of [...h.timers]) {
        if (timer.delay === 1200) { h.timers.delete(id); timer.callback(); }
    }
    for (let i = 0; i < 8; i++) await Promise.resolve();
    assert.equal(h.requests.length, 2);
    h.response(1, 12);
    await h.context.miningMasterMobileRefreshPromise;
    assert.equal(h.active().marker, 'server');
    assert.equal(h.fetchCalls.length, 1);
});

test('Master stops retrying a permanently stale fragment after five follow-ups', async () => {
    const h = boardHarness('mining_master');
    let pending = h.refresh({version: 11});
    for (let attempt = 0; attempt <= 5; attempt++) {
        h.response(attempt, 9);
        assert.equal(await pending, false);
        const retry = [...h.timers].find(([, timer]) => timer.delay === 1200);
        if (attempt === 5) {
            assert.equal(retry, undefined);
        } else {
            assert.ok(retry);
            h.timers.delete(retry[0]);
            retry[1].callback();
            pending = h.context.miningMasterMobileRefreshPromise;
        }
    }
    assert.equal(h.requests.length, 6);
    assert.equal(h.effects.replace.length, 0);
    assert.equal(h.version(), '10');
});
