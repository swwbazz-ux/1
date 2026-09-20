const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const BACKEND = path.resolve(__dirname, "..", "..", "..");
const roleAppsSource = fs.readFileSync(
    path.join(BACKEND, "users", "role_apps.py"),
    "utf8"
);
const match = roleAppsSource.match(/PUSH_SERVICE_WORKER_JS\s*=\s*r"""([\s\S]*?)"""\.strip\(\)/);
if (!match) throw new Error("PUSH_SERVICE_WORKER_JS not found");
const serviceWorkerSource = match[1];

function createRuntime(options = {}) {
    const listeners = new Map();
    const notifications = [];
    const fetches = [];
    const navigations = [];
    const focused = [];
    const visible = options.visible === true;
    const client = {
        visibilityState: visible ? "visible" : "hidden",
        navigate(url) {
            navigations.push(url);
            return Promise.resolve();
        },
        focus() {
            focused.push(true);
            return Promise.resolve(this);
        }
    };
    const self = {
        addEventListener(type, handler) {
            listeners.set(type, handler);
        },
        clients: {
            matchAll() {
                return Promise.resolve([client]);
            },
            openWindow(url) {
                navigations.push(url);
                return Promise.resolve();
            }
        },
        registration: {
            showNotification(title, config) {
                notifications.push({title, config});
                return Promise.resolve();
            }
        }
    };
    async function fetch(url, init = {}) {
        fetches.push({url, init});
        if (url === "/push/pending/") {
            return {
                ok: true,
                json: async () => ({
                    ok: true,
                    badge: 1,
                    csrf_token: "csrf-test",
                    notifications: [{
                        id: 17,
                        title: "Самосвал загружен",
                        body: "Пульт обновлён.",
                        tag: "dispatcher-trip",
                        url: "/dispatcher/control/"
                    }]
                })
            };
        }
        return {ok: true, json: async () => ({ok: true})};
    }
    const context = vm.createContext({
        self,
        fetch,
        ROLE_CODE: "dispatcher",
        ROLE_ICON_SLUG: "dispatcher",
        START_URL: "/dispatcher/control/",
        JSON,
        Array,
        Promise,
        console
    });
    vm.runInContext(serviceWorkerSource, context, {filename: "dispatcher-push-sw.js"});
    return {listeners, notifications, fetches, navigations, focused};
}

async function dispatch(runtime, type, event) {
    let task;
    event.waitUntil = promise => { task = promise; };
    runtime.listeners.get(type)(event);
    await task;
}

test("visible Dispatcher window suppresses duplicate Windows banner and marks the item shown", async () => {
    const runtime = createRuntime({visible: true});

    await dispatch(runtime, "push", {});

    assert.equal(runtime.notifications.length, 0);
    assert.deepEqual(runtime.fetches.map(item => item.url), ["/push/pending/", "/push/shown/"]);
    assert.match(runtime.fetches[1].init.body, /17/);
});

test("closed or hidden Dispatcher window shows one persistent system notification", async () => {
    const runtime = createRuntime({visible: false});

    await dispatch(runtime, "push", {});

    assert.equal(runtime.notifications.length, 1);
    assert.equal(runtime.notifications[0].title, "Самосвал загружен");
    assert.equal(runtime.notifications[0].config.requireInteraction, true);
    assert.equal(runtime.notifications[0].config.renotify, true);
    assert.equal(runtime.notifications[0].config.data.url, "/dispatcher/control/");
    assert.deepEqual(runtime.fetches.map(item => item.url), ["/push/pending/", "/push/shown/"]);
});

test("notification click focuses the existing PWA window and navigates to the Dispatcher board", async () => {
    const runtime = createRuntime({visible: false});
    let closed = false;

    await dispatch(runtime, "notificationclick", {
        notification: {
            data: {url: "/dispatcher/control/"},
            close() { closed = true; }
        }
    });

    assert.equal(closed, true);
    assert.deepEqual(runtime.navigations, ["/dispatcher/control/"]);
    assert.equal(runtime.focused.length, 1);
});
