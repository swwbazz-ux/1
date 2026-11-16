"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const BACKEND = path.resolve(__dirname, "..", "..", "..");
const SCRIPT = fs.readFileSync(
    path.join(BACKEND, "static", "js", "dispatcher-canvas-v1.js"),
    "utf8"
);
const CSS = fs.readFileSync(
    path.join(BACKEND, "static", "css", "dispatcher-canvas-v1.css"),
    "utf8"
);
const TEMPLATE = fs.readFileSync(
    path.join(BACKEND, "templates", "trips", "dispatcher_control.html"),
    "utf8"
);

function makeStyle() {
    const values = new Map();
    return {
        width: "",
        transform: "",
        setProperty(name, value) {
            values.set(name, value);
        },
        removeProperty(name) {
            values.delete(name);
        },
        getPropertyValue(name) {
            return values.get(name) || "";
        },
    };
}

function runCanvas({width, height, phone = false, phoneFit = true}) {
    const listeners = {};
    const viewportListeners = {};
    const shell = {style: makeStyle()};
    const attributes = new Map([["data-dispatcher-canvas", "off"]]);
    if (phoneFit) attributes.set("data-dispatcher-phone-fit", "");
    const canvas = {
        style: makeStyle(),
        hasAttribute(name) {
            return attributes.has(name);
        },
        setAttribute(name, value) {
            attributes.set(name, value);
        },
        getAttribute(name) {
            return attributes.get(name);
        },
        querySelector(selector) {
            return selector === ".dispatcher-shell" ? shell : null;
        },
    };
    const state = {phone};
    const window = {
        innerWidth: width,
        innerHeight: height,
        matchMedia() {
            return {matches: state.phone};
        },
        addEventListener(name, handler) {
            (listeners[name] ||= []).push(handler);
        },
        visualViewport: {
            addEventListener(name, handler) {
                (viewportListeners[name] ||= []).push(handler);
            },
        },
    };
    const document = {
        body: {classList: {contains: () => false}},
        querySelector(selector) {
            return selector === "[data-dispatcher-canvas]" ? canvas : null;
        },
    };
    vm.runInNewContext(SCRIPT, {
        document,
        window,
        setTimeout: (handler) => handler(),
    });
    return {attributes, canvas, listeners, shell, state, window};
}

test("desktop canvas fills the viewport with one proportional transform", () => {
    const runtime = runCanvas({width: 1536, height: 886});
    assert.equal(runtime.attributes.get("data-dispatcher-canvas"), "on");
    assert.equal(runtime.canvas.style.getPropertyValue("--dispatcher-canvas-h"), "1108px");
    assert.equal(runtime.canvas.style.getPropertyValue("--dispatcher-canvas-w"), "1921px");
    assert.ok(
        Math.abs(Number(runtime.canvas.style.getPropertyValue("--dispatcher-canvas-scale")) - 886 / 1108) < 0.001
    );
    assert.equal(runtime.shell.style.width, "");
    assert.equal(runtime.shell.style.transform, "");
});

test("phone landscape uses the control-only 1400x800 shell fit", () => {
    const runtime = runCanvas({width: 784, height: 336, phone: true});
    assert.equal(runtime.attributes.get("data-dispatcher-canvas"), "off");
    assert.equal(runtime.shell.style.width, "1867px");
    assert.match(runtime.shell.style.transform, /^scale\(0\.4199/);

    runtime.state.phone = false;
    runtime.window.innerWidth = 1536;
    runtime.window.innerHeight = 886;
    runtime.listeners.resize[0]();
    assert.equal(runtime.shell.style.width, "");
    assert.equal(runtime.shell.style.transform, "");
    assert.equal(runtime.attributes.get("data-dispatcher-canvas"), "on");
});

test("report canvas does not inherit the control phone transform", () => {
    const runtime = runCanvas({width: 784, height: 336, phone: true, phoneFit: false});
    assert.equal(runtime.attributes.get("data-dispatcher-canvas"), "off");
    assert.equal(runtime.shell.style.width, "");
    assert.equal(runtime.shell.style.transform, "");
});

test("control template delegates desktop and phone fitting to the canvas module", () => {
    assert.match(TEMPLATE, /data-dispatcher-phone-fit/);
    assert.match(
        TEMPLATE,
        /dispatcher-canvas-v1\.js[^\n]+dispatcher-desktop-shell-v167/
    );
    assert.equal(
        (TEMPLATE.match(/function fitDispatcherShellToScreen/g) || []).length,
        1,
        "в шаблоне должен остаться только отдельный масштабатор Горного мастера"
    );
});

test("canvas stylesheet has no malformed dot-prefixed calc functions", () => {
    assert.doesNotMatch(CSS, /\.calc\(/);
    for (const factor of [".82", ".78", ".8", ".95", ".9"]) {
        assert.match(CSS, new RegExp(`calc\\(${factor.replace(".", "\\.")} \\* var\\(--gd-vw\\)\\)`));
    }
});

/* Планшетная часть файла: принудительное обновление service worker.
   Песочница даёт то, чего у холста нет, — navigator.serviceWorker,
   location и sessionStorage. Промисы заглушек синхронные, чтобы
   проверки оставались без ожиданий. */

function makeStorage(initial = {}, options = {}) {
    const data = new Map(Object.entries(initial));
    return {
        data,
        getItem(name) {
            if (options.unavailable) throw new Error("storage unavailable");
            return data.has(name) ? data.get(name) : null;
        },
        setItem(name, value) {
            if (options.unavailable) throw new Error("storage unavailable");
            data.set(name, String(value));
        },
    };
}

const TABLET_PATH = "/mining-master/tablet/";
const SHELL_V165 = "mining-master-mobile-shell-v165";
const SHELL_V166 = "mining-master-mobile-shell-v166";

function runSwBlock({
    pathname,
    shellVersion = SHELL_V165,
    storage,
    dialog = null,
    guard = null,
}) {
    const reloads = [];
    const updates = [];
    const messages = [];
    const swListeners = {};
    const store = storage || makeStorage();
    const registration = {
        waiting: {
            postMessage(message) {
                messages.push(message);
            },
        },
        update() {
            updates.push(1);
            return {catch() {}};
        },
    };
    const serviceWorker = {
        getRegistration() {
            return {
                then(handler) {
                    handler(registration);
                    return {catch() {}};
                },
            };
        },
        addEventListener(name, handler) {
            (swListeners[name] ||= []).push(handler);
        },
    };
    const body = {
        classList: {contains: () => false},
        dataset: {appShellVersion: shellVersion},
    };
    const document = {
        body,
        querySelector(selector) {
            return selector === "dialog[open]" ? dialog : null;
        },
    };
    const window = {
        innerWidth: 1536,
        innerHeight: 886,
        addEventListener() {},
        matchMedia() {
            return {matches: false};
        },
    };
    if (guard) window.AppPwaContractGuard = guard;
    vm.runInNewContext(SCRIPT, {
        document,
        window,
        navigator: {serviceWorker},
        location: {
            pathname,
            reload() {
                reloads.push(1);
            },
        },
        sessionStorage: store,
        setTimeout: (handler) => handler(),
    });
    return {
        messages,
        reloads,
        storage: store,
        updates,
        controllerListenerCount() {
            return (swListeners.controllerchange || []).length;
        },
        fireControllerChange() {
            for (const handler of swListeners.controllerchange || []) handler();
        },
    };
}

test("service worker force update runs only on the mining-master tablet route", () => {
    for (const pathname of ["/dispatcher/control/", "/mining-master/tablet/dispatcher/", "/mining-master/assignments/"]) {
        const other = runSwBlock({pathname});
        assert.equal(other.controllerListenerCount(), 0, `не должно быть слушателя на ${pathname}`);
        assert.equal(other.updates.length, 0, `не должно быть update() на ${pathname}`);
    }
    const tablet = runSwBlock({pathname: TABLET_PATH});
    assert.equal(tablet.controllerListenerCount(), 1);
    assert.equal(tablet.updates.length, 1);
    assert.equal(tablet.messages.length, 1);
    assert.equal(tablet.messages[0].type, "SKIP_WAITING");
});

test("tablet reload happens once per shell version", () => {
    const storage = makeStorage();
    const first = runSwBlock({pathname: TABLET_PATH, storage});
    first.fireControllerChange();
    assert.equal(first.reloads.length, 1);
    assert.equal(storage.getItem(`mm-sw-forced-reload:${SHELL_V165}`), "1");

    first.fireControllerChange();
    assert.equal(first.reloads.length, 1, "повторный controllerchange той же версии не перезагружает");

    const sameShell = runSwBlock({pathname: TABLET_PATH, storage});
    sameShell.fireControllerChange();
    assert.equal(sameShell.reloads.length, 0, "та же версия оболочки уже перезагружалась");

    const nextShell = runSwBlock({pathname: TABLET_PATH, shellVersion: SHELL_V166, storage});
    nextShell.fireControllerChange();
    assert.equal(nextShell.reloads.length, 1, "новая версия оболочки снова разрешает одну перезагрузку");
    assert.equal(storage.getItem(`mm-sw-forced-reload:${SHELL_V166}`), "1");
});

test("tablet reload waits while the operator has unfinished input", () => {
    const guard = {
        unsafe: true,
        hasUnsafeWorkInProgress() {
            return this.unsafe;
        },
    };
    const storage = makeStorage();
    const runtime = runSwBlock({pathname: TABLET_PATH, storage, guard});
    runtime.fireControllerChange();
    assert.equal(runtime.reloads.length, 0, "незавершённая работа блокирует перезагрузку");
    assert.equal(
        storage.getItem(`mm-sw-forced-reload:${SHELL_V165}`),
        null,
        "заблокированная перезагрузка не расходует ключ"
    );

    guard.unsafe = false;
    runtime.fireControllerChange();
    assert.equal(runtime.reloads.length, 1, "после завершения ввода перезагрузка срабатывает");

    const dialogStorage = makeStorage();
    const withDialog = runSwBlock({pathname: TABLET_PATH, storage: dialogStorage, dialog: {}});
    withDialog.fireControllerChange();
    assert.equal(withDialog.reloads.length, 0, "открытый <dialog open> блокирует перезагрузку");
    assert.equal(dialogStorage.getItem(`mm-sw-forced-reload:${SHELL_V165}`), null);
});

test("tablet reload survives an unavailable sessionStorage and keeps the versioned key", () => {
    const runtime = runSwBlock({
        pathname: TABLET_PATH,
        storage: makeStorage({}, {unavailable: true}),
    });
    runtime.fireControllerChange();
    assert.equal(runtime.reloads.length, 1, "без sessionStorage перезагрузка всё равно выполняется");

    assert.match(SCRIPT, /"mm-sw-forced-reload:" \+ shellVersion/);
    assert.doesNotMatch(SCRIPT, /"mm-sw-forced-reload"(?!\s*\+)/, "ключ не должен остаться без версии");
    assert.match(SCRIPT, /dataset\.appShellVersion/);
    assert.match(SCRIPT, /dialog\[open\]/);
});
