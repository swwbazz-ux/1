const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(
    path.resolve(__dirname, "../application-session-heartbeat.js"),
    "utf8"
);

function runHeartbeat({
    userAgent = "Mozilla/5.0", nativeApp = false, nativeVersion = "",
    standalone = false, iosStandalone = false, hidden = false, deferred = false,
} = {}) {
    const requests = [], events = [];
    const listeners = new Map(), timers = new Map();
    let now = 1000000, timerId = 0;
    const document = {
        hidden, cookie: "csrftoken=test-token",
        body: {dataset: {nativeApp: nativeApp ? "true" : "false", nativeClientVersion: nativeVersion}},
        querySelector() {return null;},
        addEventListener(name, handler) {listeners.set(name, handler);},
    };
    const window = {
        location: {pathname: "/driver/"},
        navigator: {userAgent, standalone: iosStandalone, onLine: true},
        AbortController,
        CustomEvent: function (type, options) {this.type = type; this.detail = options.detail;},
        dispatchEvent(event) {events.push(event);},
        matchMedia(query) {return {matches: standalone && query.includes("standalone")};},
        fetch(url, options) {
            let resolve, reject;
            const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
            requests.push({url, options, body: new URLSearchParams(options.body), resolve, reject});
            if (!deferred) resolve({status: 204, ok: true, redirected: false});
            return promise;
        },
        addEventListener(name, handler) {listeners.set(name, handler);},
        clearTimeout(id) {timers.delete(id);},
        setTimeout(callback, delay) {timers.set(++timerId, {callback, delay}); return timerId;},
    };
    class FakeDate extends Date {static now() {return now;}}
    const context = {window, document, URLSearchParams, decodeURIComponent, Date: FakeDate};
    const load = () => vm.runInNewContext(source, context);
    load();
    return {
        requests, listeners, document, window, events, load,
        advance(ms) {now += ms;},
        runTimers(delay) {
            for (const [id, timer] of [...timers]) {
                if (timer.delay === delay) {timers.delete(id); timer.callback();}
            }
        },
        emit(type) {const handler = listeners.get(type); if (handler) handler({type});},
        success(index, status = 204, redirected = false) {requests[index].resolve({status, ok: status >= 200 && status < 300, redirected});},
        pendingTimers(delay) {return [...timers.values()].filter(timer => timer.delay === delay).length;},
        successes() {return events.filter(event => event.type === "web-heartbeat-success");},
        diagnostics() {return events.filter(event => event.type === "app:connectiondiagnostic").map(event => event.detail.cause);},
    };
}
async function settle() {for (let i = 0; i < 12; i++) await Promise.resolve();}

test("native APK heartbeat reports Android APK and binary version", () => {
    const {requests} = runHeartbeat({
        userAgent: "Mozilla/5.0 (Linux; Android 14)",
        nativeApp: true,
        nativeVersion: "0.1.18",
    });
    assert.equal(requests.length, 1);
    assert.equal(requests[0].body.get("client_kind"), "android_apk");
    assert.equal(requests[0].body.get("client_version"), "0.1.18");
});

test("installed Android PWA is distinct from an ordinary browser", () => {
    const pwa = runHeartbeat({
        userAgent: "Mozilla/5.0 (Linux; Android 14) Chrome/140 Mobile",
        standalone: true,
    });
    const browser = runHeartbeat({
        userAgent: "Mozilla/5.0 (Linux; Android 14) Chrome/140 Mobile",
    });
    assert.equal(pwa.requests[0].body.get("client_kind"), "android_pwa");
    assert.equal(browser.requests[0].body.get("client_kind"), "browser");
});

test("iPhone home-screen PWA is distinct from Safari", () => {
    const userAgent = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1";
    const pwa = runHeartbeat({userAgent, iosStandalone: true});
    const safari = runHeartbeat({userAgent});
    assert.equal(pwa.requests[0].body.get("client_kind"), "ios_pwa");
    assert.equal(safari.requests[0].body.get("client_kind"), "safari");
});

test("hidden web page does not claim foreground presence", () => {
    const {requests} = runHeartbeat({hidden: true});
    assert.equal(requests.length, 0);
});


test("only a fresh authenticated 204 publishes transport success", async () => {
    const r = runHeartbeat({deferred: true}); r.success(0); await settle();
    assert.equal(r.successes().length, 1);
    assert.equal(r.successes()[0].detail.source, "application-session-heartbeat");
    assert.equal(r.successes()[0].detail.status, 204);
    assert.equal(r.successes()[0].detail.occurredAtMs, 1000000);
    assert.equal(r.window.AppSessionHeartbeat.lastSuccessAtMs, 1000000);
    assert.equal(r.pendingTimers(30000), 1);
});

for (const [status, redirected] of [[200, false], [204, true], [401, false], [403, false], [503, false]]) {
    test(`heartbeat ${status} redirect=${redirected} cannot prove authenticated transport`, async () => {
        const r = runHeartbeat({deferred: true}); r.success(0, status, redirected); await settle();
        assert.equal(r.successes().length, 0);
        assert.equal(r.window.AppSessionHeartbeat.lastSuccessAtMs, 0);
        assert.equal(r.pendingTimers(30000), 1);
    });
}

test("one physical wake burst shares one request even after a fast response", async () => {
    const r = runHeartbeat({deferred: true});
    for (const type of ["focus", "pageshow", "resume", "native-connectivity-resume"]) r.emit(type);
    assert.equal(r.requests.length, 1);
    r.success(0); await settle();
    for (const type of ["focus", "pageshow", "resume"]) {r.emit(type); await settle();}
    assert.equal(r.requests.length, 1);
    assert.equal(r.pendingTimers(30000), 1);
    r.advance(30000); r.runTimers(30000);
    assert.equal(r.requests.length, 2);
    assert.equal(r.pendingTimers(8000), 1);
});

test("background cancels one owner neutrally and hidden time starts no requests", async () => {
    const r = runHeartbeat({deferred: true});
    r.document.hidden = true; r.emit("visibilitychange");
    assert.equal(r.requests[0].options.signal.aborted, true);
    r.advance(60000); r.runTimers(30000); r.runTimers(8000);
    r.success(0); await settle();
    assert.equal(r.requests.length, 1); assert.equal(r.successes().length, 0);
    assert.deepEqual(r.diagnostics(), ["planned_abort"]);
    r.document.hidden = false; r.emit("visibilitychange");
    assert.equal(r.requests.length, 2);
});

test("timeout releases a hung owner once and a late old finally cannot clear the next request", async () => {
    const r = runHeartbeat({deferred: true});
    r.advance(8000); r.runTimers(8000);
    assert.equal(r.requests[0].options.signal.aborted, true);
    assert.deepEqual(r.diagnostics(), ["timeout"]);
    assert.equal(r.pendingTimers(22000), 1);
    r.advance(22000); r.runTimers(22000);
    assert.equal(r.requests.length, 2);
    r.success(0); await settle(); r.emit("focus");
    assert.equal(r.successes().length, 0);
    assert.equal(r.requests.length, 2);
    assert.equal(r.pendingTimers(8000), 1);
    r.success(1); await settle();
    assert.equal(r.successes().length, 1);
    assert.equal(r.pendingTimers(30000), 1);
});

test("late rejection of a planned abort neither reports network error nor takes new ownership", async () => {
    const r = runHeartbeat({deferred: true});
    r.emit("pagehide"); r.emit("pageshow");
    assert.equal(r.requests.length, 2);
    r.requests[0].reject(Object.assign(new Error("planned"), {name: "AbortError"}));
    await settle();
    assert.deepEqual(r.diagnostics(), ["planned_abort"]);
    assert.equal(r.pendingTimers(8000), 1);
    r.success(1); await settle(); assert.equal(r.successes().length, 1);
});

test("network error is diagnostic only and keeps the thirty-second presence interval", async () => {
    const r = runHeartbeat({deferred: true}); r.requests[0].reject(new TypeError("network")); await settle();
    assert.deepEqual(r.diagnostics(), ["network_error"]);
    assert.equal(r.successes().length, 0);
    assert.equal(r.pendingTimers(30000), 1);
    assert.equal(r.events.some(event => event.type === "operational-state-connection"), false);
});

test("loading the script twice cannot install a second heartbeat cycle", async () => {
    const r = runHeartbeat(); r.load(); await settle();
    assert.equal(r.requests.length, 1); assert.equal(r.pendingTimers(30000), 1);
});


test("repeated visible focus events cannot accelerate the thirty-second heartbeat", async () => {
    const r = runHeartbeat(); await settle();
    for (let i = 0; i < 14; i++) {r.advance(2000); r.emit("focus"); await settle();}
    assert.equal(r.requests.length, 1);
    assert.equal(r.pendingTimers(30000), 1);
    r.advance(2000); r.runTimers(30000); await settle();
    assert.equal(r.requests.length, 2);
});
