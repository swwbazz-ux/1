/* Экран водителя собран из нескольких файлов, которые браузер выполняет по
   очереди. Внутри одного файла функции видны с любого места, между файлами — нет:
   часть, выполняемая при загрузке, не должна обращаться к тому, что определено
   в следующем файле. Этот тест загружает части в том же порядке, что и шаблон,
   и падает, если такая связь появилась. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const {DRIVER_SCREEN_SCRIPTS} = require("./driver-screen-source");

const BACKEND_ROOT = path.resolve(__dirname, "..", "..", "..");
const TEMPLATE = fs.readFileSync(
    path.join(BACKEND_ROOT, "templates", "users", "driver_shift.html"),
    "utf8"
);

function stubElement() {
    return {
        dataset: {},
        classList: {add() {}, remove() {}, contains: () => false, toggle() {}},
        style: {setProperty() {}, removeProperty() {}},
        attributes: [],
        hidden: false,
        textContent: "",
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener() {},
        removeEventListener() {},
        getAttribute: () => null,
        setAttribute() {},
        hasAttribute: () => false,
        removeAttribute() {},
        matches: () => false,
        closest: () => null,
        contains: () => false,
        appendChild: (node) => node,
        getBoundingClientRect: () => ({width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0}),
    };
}

function browserSandbox() {
    const documentStub = {
        hidden: false,
        readyState: "loading",
        activeElement: null,
        body: stubElement(),
        documentElement: stubElement(),
        querySelector: () => stubElement(),
        querySelectorAll: () => [],
        createElement: () => stubElement(),
        createTextNode: () => ({}),
        addEventListener() {},
        removeEventListener() {},
    };
    const sandbox = {
        console,
        document: documentStub,
        navigator: {
            onLine: true,
            serviceWorker: {getRegistration: () => Promise.resolve(null), addEventListener() {}},
        },
        location: {pathname: "/driver/", href: "http://localhost/driver/", origin: "http://localhost"},
        localStorage: {getItem: () => null, setItem() {}, removeItem() {}},
        sessionStorage: {getItem: () => null, setItem() {}, removeItem() {}},
        setTimeout, clearTimeout, setInterval, clearInterval,
        requestAnimationFrame: (fn) => setTimeout(fn, 0),
        cancelAnimationFrame: clearTimeout,
        fetch: () => Promise.resolve({ok: true, json: () => Promise.resolve({})}),
        CustomEvent: class {constructor(type, init) {this.type = type; Object.assign(this, init || {});}},
        Event: class {constructor(type) {this.type = type;}},
        MutationObserver: class {observe() {} disconnect() {}},
        FormData: class {},
        matchMedia: () => ({matches: false, addEventListener() {}, removeEventListener() {}}),
        getComputedStyle: () => ({getPropertyValue: () => ""}),
    };
    sandbox.window = sandbox;
    sandbox.self = sandbox;
    sandbox.addEventListener = () => {};
    sandbox.removeEventListener = () => {};
    sandbox.dispatchEvent = () => true;
    return vm.createContext(sandbox);
}

test("шаблон подключает части экрана водителя в объявленном порядке", () => {
    const positions = DRIVER_SCREEN_SCRIPTS.map((name) => TEMPLATE.indexOf(`js/${name}`));
    positions.forEach((position, index) => {
        assert.notEqual(position, -1, `шаблон не подключает ${DRIVER_SCREEN_SCRIPTS[index]}`);
    });
    const sorted = [...positions].sort((left, right) => left - right);
    assert.deepEqual(positions, sorted, "порядок подключения в шаблоне отличается от объявленного");
});

test("части экрана водителя загружаются по очереди без обращений вперёд", () => {
    const context = browserSandbox();
    for (const name of DRIVER_SCREEN_SCRIPTS) {
        const code = fs.readFileSync(path.join(BACKEND_ROOT, "static", "js", name), "utf8");
        assert.doesNotThrow(
            () => vm.runInContext(code, context, {filename: name}),
            `${name} при загрузке обращается к тому, чего ещё нет`
        );
    }
    for (const name of ["driverFragmentSnapshot", "driverMorphShell", "createDriverRoleHoldGuard",
        "bindDriverUnloadGesture", "DriverVoiceGuard", "applyOperationalStateRefresh",
        "DriverShiftCloseOutbox", "bindDriverMobileShell"]) {
        assert.notEqual(typeof context[name], "undefined", `после загрузки нет ${name}`);
    }
});
