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
    userAgent = "Mozilla/5.0",
    nativeApp = false,
    nativeVersion = "",
    standalone = false,
    iosStandalone = false,
    hidden = false,
} = {}) {
    const requests = [];
    const listeners = new Map();
    const document = {
        hidden,
        cookie: "csrftoken=test-token",
        body: {
            dataset: {
                nativeApp: nativeApp ? "true" : "false",
                nativeAppVersion: nativeVersion,
            },
        },
        querySelector() {
            return null;
        },
        addEventListener(name, handler) {
            listeners.set(name, handler);
        },
    };
    const window = {
        location: {pathname: "/driver/"},
        navigator: {userAgent, standalone: iosStandalone},
        matchMedia(query) {
            return {matches: standalone && query.includes("standalone")};
        },
        fetch(url, options) {
            requests.push({url, options, body: new URLSearchParams(options.body)});
            return Promise.resolve({ok: true});
        },
        addEventListener(name, handler) {
            listeners.set(name, handler);
        },
        clearInterval() {},
        setInterval() {
            return 1;
        },
    };

    vm.runInNewContext(source, {
        window,
        document,
        URLSearchParams,
        decodeURIComponent,
    });
    return {requests, listeners, document};
}

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
