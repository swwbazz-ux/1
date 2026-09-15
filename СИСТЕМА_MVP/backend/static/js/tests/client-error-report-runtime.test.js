const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const script = fs.readFileSync(path.join(__dirname, '../client-error-report.js'), 'utf8');
function runtime({beacon = true, role = 'excavator_operator'} = {}) {
    let now = 1000;
    const handlers = {};
    const payloads = [];
    const window = {
        URL,
        location: {href: 'https://excavator.test/excavator/work/?pin=never-send', pathname: '/excavator/work/'},
        navigator: {sendBeacon: (_, blob) => { if (beacon) payloads.push(JSON.parse(blob.value)); return beacon; }},
        Blob: class { constructor(parts) { this.value = parts.join(''); } },
        addEventListener: (name, callback) => { handlers[name] = callback; },
        fetch: (_, options) => { payloads.push(JSON.parse(options.body)); return Promise.resolve({ok: true}); }
    };
    const document = {body: {dataset: {appRoleCode: role, appShellVersion: 'shell-v250', clientKind: 'android_apk'}}};
    vm.runInNewContext(script, {window, document, Date: {now: () => now}});
    return {window, payloads, send: (name, detail) => handlers[name](detail), advance: ms => { now += ms; }};
}
function diagnostic(runtime, overrides = {}) {
    runtime.send('app:connectiondiagnostic', {detail: {
        cause: 'network_error', owner: 'realtime_poll', generation: 11, failureCount: 1,
        lastSuccessAt: 111, durationMs: 900, nativeHeartbeatFresh: true, pendingOutboxCount: 2,
        fromState: 'ok', toState: 'weak', ...overrides
    }});
}

test('resource failures retain DOM tag and pathname, never URL credentials/query/hash', () => {
    const r = runtime();
    r.send('error', {target: {tagName: 'IFRAME', src: 'https://user:pass@excavator.test/excavator/work/?token=private#pin'}});
    assert.equal(r.payloads.length, 1);
    assert.equal(r.payloads[0].source, 'resource:IFRAME');
    assert.deepEqual(JSON.parse(r.payloads[0].stack), {tag: 'IFRAME', path: '/excavator/work/'});
    assert.equal(r.payloads[0].message, 'Не загрузился ресурс: IFRAME /excavator/work/');
    assert.doesNotMatch(JSON.stringify(r.payloads), /private|user:pass|#pin|\?token/);
});

test('actual resource errors remain visible during background, planned abort marker is not guessed', () => {
    const r = runtime();
    diagnostic(r, {cause: 'background_pause', fromState: 'ok', toState: 'ok'});
    r.send('error', {target: {tagName: 'SCRIPT', src: '/static/main.js?pin=secret'}});
    assert.equal(r.payloads.length, 1);
    assert.equal(r.payloads[0].source, 'resource:SCRIPT');
});

test('only explicitly planned AbortError is neutral; timeout abort stays diagnosable', () => {
    const r = runtime();
    r.send('unhandledrejection', {reason: {name: 'AbortError', message: 'superseded', __appPlannedAbort: true}});
    r.send('error', {error: {name: 'AbortError', __appPlannedAbort: true}});
    diagnostic(r, {cause: 'planned_abort', fromState: 'ok', toState: 'weak'});
    assert.equal(r.payloads.length, 0);
    assert.equal(r.window.AppClientDiagnostics.getRecentConnections()[0].cause, 'planned_abort');
    r.send('unhandledrejection', {reason: {name: 'AbortError', message: 'request timeout'}});
    assert.equal(r.payloads.length, 1);
    assert.match(r.payloads[0].message, /request timeout/);
});

test('connection telemetry contains numeric/enum whitelist and excludes attached action/secrets', () => {
    const r = runtime();
    diagnostic(r, {payload: {pin: '765432'}, token: 'sensitive', url: '/x?cookie=private',
        owner: '/private?token=dontsend'});
    const item = JSON.parse(r.payloads[0].stack);
    assert.equal(item.generation, 11);
    assert.equal(item.failureCount, 1);
    assert.equal(item.pendingOutboxCount, 2);
    assert.equal(item.nativeHeartbeatFresh, true);
    assert.equal(item.owner, 'realtime_poll');
    assert.equal(item.role, 'excavator_operator');
    assert.equal(item.clientKind, 'android_apk');
    assert.doesNotMatch(JSON.stringify(r.payloads), /765432|sensitive|private|dontsend/);
    assert.equal(Object.keys(item).length, 15);
});

test('connection diagnostics are bounded and do not exhaust the script-error allowance', () => {
    const r = runtime();
    for (let i = 0; i < 60; i++) diagnostic(r, {generation: i, cause: 'planned_abort'});
    assert.equal(r.window.AppClientDiagnostics.getRecentConnections().length, 24);
    assert.equal(r.payloads.length, 0);
    for (let i = 0; i < 8; i++) diagnostic(r, {fromState: 'weak', toState: 'lost'});
    assert.equal(r.payloads.length, 1);
    r.send('error', {message: 'Actual JS failure', filename: '/static/main.js?cookie=private'});
    assert.equal(r.payloads.length, 2);
    r.advance(60001);
    diagnostic(r, {fromState: 'weak', toState: 'lost'});
    assert.equal(r.payloads.length, 3);
});

test('a heartbeat with unchanged state produces no error report and returned memory is copied', () => {
    const r = runtime();
    diagnostic(r, {cause: 'heartbeat_success', fromState: 'ok', toState: 'ok'});
    assert.equal(r.payloads.length, 0);
    const first = r.window.AppClientDiagnostics.getRecentConnections();
    first[0].cause = 'private-data';
    assert.equal(r.window.AppClientDiagnostics.getRecentConnections()[0].cause, 'heartbeat_success');
});

test('exception and fetch-fallback reports redact query, PIN, cookie and bearer value', () => {
    const r = runtime({beacon: false, role: 'driver'});
    r.send('unhandledrejection', {reason: {
        message: 'pin=4444 cookie=abcdef token="top-secret" Bearer abcXYZ',
        stack: 'at https://driver.test/static/a.js?token=more-secret:10:5'
    }});
    assert.equal(r.payloads.length, 1);
    assert.doesNotMatch(JSON.stringify(r.payloads), /4444|abcdef|top-secret|abcXYZ|more-secret/);
    assert.match(r.payloads[0].message, /redacted/);
    assert.equal(r.payloads[0].role, 'driver');
});

test('real runtime cause codes retain diagnostic meaning and HTTP status is numeric', () => {
    const r = runtime();
    diagnostic(r, {cause: 'http_503'});
    let item = JSON.parse(r.payloads[0].stack);
    assert.equal(item.cause, 'http_error');
    assert.equal(item.httpStatus, 503);
    diagnostic(r, {cause: 'transport_recovered', fromState: 'lost', toState: 'recovering'});
    item = JSON.parse(r.payloads[1].stack);
    assert.equal(item.cause, 'transport_recovered');
    diagnostic(r, {cause: 'http_503?pin=secret', fromState: 'recovering', toState: 'weak'});
    assert.equal(JSON.parse(r.payloads[2].stack).cause, 'state_change');
    assert.doesNotMatch(JSON.stringify(r.payloads), /secret/);
});

test('native app uses actual server body marker even without a clientKind attribute', () => {
    const handlers = {};
    const payloads = [];
    const window = {URL, location: {href: 'https://driver.test/driver/'}, navigator: {},
        addEventListener: (name, callback) => { handlers[name] = callback; },
        fetch: (_, options) => { payloads.push(JSON.parse(options.body)); return Promise.resolve(); }};
    const document = {body: {dataset: {nativeApp: 'true', nativeClientVersion: '0.1.28',
        appRoleCode: 'driver', appShellVersion: 'driver-mobile-shell-v237'}}};
    vm.runInNewContext(script, {window, document});
    handlers['app:connectiondiagnostic']({detail: {cause: 'timeout', fromState: 'ok', toState: 'weak'}});
    const item = JSON.parse(payloads[0].stack);
    assert.equal(item.clientKind, 'android_apk');
    assert.equal(item.nativeVersion, '0.1.28');
    assert.equal(item.appVersion, 'driver-mobile-shell-v237');
});

test('healthy periodic and outbox reconciliation do not consume an incident report budget', () => {
    const r = runtime();
    for (let i = 0; i < 12; i++) {
        diagnostic(r, {cause: 'periodic_server_truth', fromState: 'ok', toState: 'recovering'});
        diagnostic(r, {cause: 'dom_applied', fromState: 'recovering', toState: 'ok'});
        diagnostic(r, {cause: 'outbox_state', fromState: 'ok', toState: 'recovering'});
        diagnostic(r, {cause: 'outbox_drained', fromState: 'recovering', toState: 'ok'});
        r.advance(20000);
    }
    assert.equal(r.payloads.length, 0);
    diagnostic(r, {cause: 'network_error', fromState: 'ok', toState: 'weak'});
    diagnostic(r, {cause: 'timeout', fromState: 'weak', toState: 'lost'});
    diagnostic(r, {cause: 'transport_recovered', fromState: 'lost', toState: 'recovering'});
    diagnostic(r, {cause: 'dom_applied', fromState: 'recovering', toState: 'ok'});
    assert.equal(r.payloads.length, 4);
    assert.deepEqual(r.payloads.map(item => JSON.parse(item.stack).toState), ['weak', 'lost', 'recovering', 'ok']);
    diagnostic(r, {cause: 'periodic_server_truth', fromState: 'ok', toState: 'recovering'});
    diagnostic(r, {cause: 'dom_applied', fromState: 'recovering', toState: 'ok'});
    assert.equal(r.payloads.length, 4);
});
