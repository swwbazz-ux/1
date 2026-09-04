"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const BACKEND_ROOT = path.resolve(__dirname, "..", "..", "..");
const LOGIN_TEMPLATE_PATH = path.join(BACKEND_ROOT, "templates", "users", "login.html");
const APP_CSS_PATH = path.join(BACKEND_ROOT, "static", "css", "app.css");
const MOBILE_ROLE_LOGIN_CSS_PATH = path.join(
    BACKEND_ROOT,
    "static",
    "css",
    "mobile-role-login-v1.css"
);
const LOGIN_TEMPLATE = fs.readFileSync(LOGIN_TEMPLATE_PATH, "utf8");
const APP_CSS = fs.readFileSync(APP_CSS_PATH, "utf8");
const MOBILE_ROLE_LOGIN_CSS = fs.readFileSync(MOBILE_ROLE_LOGIN_CSS_PATH, "utf8");

class FakeClassList {
    constructor(initialValues) {
        this.values = new Set(initialValues || []);
    }

    add(...names) {
        names.forEach((name) => this.values.add(name));
    }

    remove(...names) {
        names.forEach((name) => this.values.delete(name));
    }

    contains(name) {
        return this.values.has(name);
    }

    toggle(name, force) {
        const enabled = typeof force === "boolean" ? force : !this.values.has(name);
        if (enabled) {
            this.values.add(name);
        } else {
            this.values.delete(name);
        }
        return enabled;
    }
}

function createEventTarget() {
    const listeners = new Map();
    return {
        addEventListener(type, listener) {
            const registered = listeners.get(type) || [];
            registered.push(listener);
            listeners.set(type, registered);
        },
        dispatchEvent(event) {
            const registered = (listeners.get(event.type) || []).slice();
            registered.forEach((listener) => listener.call(this, event));
        },
    };
}

function createTimers() {
    let now = 0;
    let nextId = 1;
    const pending = new Map();
    return {
        setTimeout(callback, delay) {
            const id = nextId++;
            pending.set(id, {callback, dueAt: now + Number(delay || 0)});
            return id;
        },
        clearTimeout(id) {
            pending.delete(id);
        },
        advance(milliseconds) {
            const target = now + milliseconds;
            while (true) {
                const due = Array.from(pending.entries())
                    .filter(([, timer]) => timer.dueAt <= target)
                    .sort((left, right) => left[1].dueAt - right[1].dueAt)[0];
                if (!due) {
                    break;
                }
                pending.delete(due[0]);
                now = due[1].dueAt;
                due[1].callback();
            }
            now = target;
        },
    };
}

function extractLoginRuntime() {
    const memoryMarker = 'var LOGIN_MEMORY_KEY = "login-remembered-credentials";';
    const memoryStart = LOGIN_TEMPLATE.indexOf(memoryMarker);
    const start = LOGIN_TEMPLATE.lastIndexOf("(function (window, document) {", memoryStart);
    assert.notEqual(memoryStart, -1, "login memory runtime must exist");
    const serviceWorkerStart = LOGIN_TEMPLATE.indexOf('    {% if not role_app %}', start);
    assert.notEqual(start, -1, "login runtime must exist");
    assert.notEqual(serviceWorkerStart, -1, "service worker boundary must exist");
    return LOGIN_TEMPLATE.slice(start, serviceWorkerStart) + "\n})(window, document);";
}

function createInput(id, attributeName) {
    const target = createEventTarget();
    const attributes = new Set([attributeName]);
    return Object.assign(target, {
        id,
        value: "",
        dataset: {
            hint: attributeName === "data-phone-input"
                ? "Введите номер телефона в формате +7 XXX-XXX-XX-XX"
                : "Код состоит из 6 цифр.",
        },
        classList: new FakeClassList(),
        scrollIntoViewCalls: 0,
        hasAttribute(name) {
            return attributes.has(name);
        },
        getBoundingClientRect() {
            return {top: 300, bottom: 344, height: 44};
        },
        scrollIntoView() {
            this.scrollIntoViewCalls += 1;
        },
    });
}

function createLoginRuntime(options) {
    const runtimeOptions = options || {};
    const viewportHeight = Number(runtimeOptions.viewportHeight || 844);
    const storageValues = new Map();
    if (typeof runtimeOptions.rememberedCredentials === "string") {
        storageValues.set("login-remembered-credentials", runtimeOptions.rememberedCredentials);
    }
    const timers = createTimers();
    const phoneInput = createInput("login-phone", "data-phone-input");
    const pinInput = createInput("login-pin", "data-pin-input");
    const submitButton = {
        disabled: false,
        value: "login",
        classList: new FakeClassList(),
        firstChild: {textContent: "Войти"},
    };
    const main = {
        scrollLeft: Number(runtimeOptions.mainScrollLeft || 0),
        clientWidth: 358,
        scrollWidth: 358,
    };
    const body = {
        classList: new FakeClassList([
            "login-page",
            "unified-login-screen",
        ]),
    };
    const hints = {
        "login-phone": {textContent: "", classList: new FakeClassList()},
        "login-pin": {textContent: "", classList: new FakeClassList()},
    };
    const formTarget = createEventTarget();
    const form = Object.assign(formTarget, {
        dataset: {loginCombined: "true"},
        requestSubmitCalls: 0,
        querySelector(selector) {
            if (selector === 'button[type="submit"]' || selector === ".unified-login-submit") {
                return submitButton;
            }
            if (selector === "[data-phone-input]") return phoneInput;
            if (selector === "[data-pin-input]") return pinInput;
            return null;
        },
        querySelectorAll(selector) {
            if (selector === "[data-phone-input], [data-pin-input]") {
                return [phoneInput, pinInput];
            }
            if (selector === 'button[type="submit"]') return [submitButton];
            return [];
        },
        contains(element) {
            return element === phoneInput || element === pinInput;
        },
        requestSubmit() {
            this.requestSubmitCalls += 1;
        },
    });
    const documentTarget = createEventTarget();
    const document = Object.assign(documentTarget, {
        body,
        documentElement: {dataset: {}},
        activeElement: runtimeOptions.autofocus ? phoneInput : body,
        scrollingElement: {
            scrollLeft: Number(runtimeOptions.documentScrollLeft || 0),
            scrollTop: 0,
        },
        querySelector(selector) {
            if (selector === "[data-validated-login]") return form;
            if (selector === ".unified-login-dialog") return main;
            const hintMatch = selector.match(/^\[data-input-hint-for="([^"]+)"\]$/);
            return hintMatch ? hints[hintMatch[1]] : null;
        },
        querySelectorAll(selector) {
            if (selector === "[data-phone-input], [data-pin-input]") {
                return [phoneInput, pinInput];
            }
            return [];
        },
    });
    const viewportTarget = createEventTarget();
    const scrollCalls = [];
    const window = Object.assign(createEventTarget(), {
        innerHeight: viewportHeight,
        scrollY: 0,
        visualViewport: Object.assign(viewportTarget, {
            height: viewportHeight,
            offsetTop: 0,
        }),
        setTimeout: timers.setTimeout,
        clearTimeout: timers.clearTimeout,
        requestAnimationFrame(callback) {
            callback();
        },
        localStorage: {
            getItem(key) {
                return storageValues.has(key) ? storageValues.get(key) : null;
            },
            setItem(key, value) {
                storageValues.set(key, String(value));
            },
            removeItem(key) {
                storageValues.delete(key);
            },
        },
        scrollTo(optionsValue) {
            scrollCalls.push(optionsValue);
            if (optionsValue && typeof optionsValue.left !== "undefined") {
                document.scrollingElement.scrollLeft = Number(optionsValue.left);
            }
        },
    });
    window.window = window;
    window.document = document;
    phoneInput.focus = function () {
        document.activeElement = phoneInput;
    };
    pinInput.focus = function () {
        document.activeElement = pinInput;
    };

    vm.runInNewContext(extractLoginRuntime(), {
        window,
        document,
        navigator: {},
        Array,
        Boolean,
        Math,
        Number,
        String,
    }, {
        filename: LOGIN_TEMPLATE_PATH,
    });
    document.dispatchEvent({type: "DOMContentLoaded"});

    return {
        body,
        document,
        form,
        main,
        phoneInput,
        pinInput,
        rememberedCredentials() {
            return window.localStorage.getItem("login-remembered-credentials");
        },
        scrollCalls,
        timers,
        focus(input, initialScrollLeft) {
            main.scrollLeft = initialScrollLeft;
            document.scrollingElement.scrollLeft = initialScrollLeft;
            document.activeElement = input;
            input.dispatchEvent({type: "focus"});
        },
        blur(input, initialScrollLeft) {
            main.scrollLeft = initialScrollLeft;
            document.scrollingElement.scrollLeft = initialScrollLeft;
            document.activeElement = body;
            input.dispatchEvent({type: "blur"});
        },
    };
}

function assertHorizontalOrigin(runtime, message) {
    assert.equal(runtime.main.scrollLeft, 0, `${message}: main.scrollLeft`);
    assert.equal(
        runtime.document.scrollingElement.scrollLeft,
        0,
        `${message}: document.scrollLeft`
    );
}

test("mobile login CSS constrains every grid child and wraps long role headings", () => {
    assert.match(
        APP_CSS,
        /\.login-page\.unified-login-screen \.unified-login-dialog,\s*\.login-page\.unified-login-screen \.unified-login-form,\s*\.login-page\.unified-login-screen \.unified-login-form > \* \{[^}]*min-width:\s*0;[^}]*max-width:\s*100%;/s
    );
    assert.match(
        APP_CSS,
        /\.login-page\.unified-login-screen \.unified-login-dialog \.app-confirm-content h2 \{[^}]*white-space:\s*normal;[^}]*overflow-wrap:\s*anywhere;/s
    );
    assert.match(
        APP_CSS,
        /\.is-login-keyboard-active \.unified-login-dialog \.app-confirm-content h2 \{[^}]*white-space:\s*normal;/s
    );
    assert.doesNotMatch(
        APP_CSS,
        /\.is-login-keyboard-active \.unified-login-dialog \.app-confirm-content h2 \{[^}]*white-space:\s*nowrap;/s
    );
});

test("natural autofocus without viewport shrink keeps the mobile login at x=0", () => {
    const runtime = createLoginRuntime({
        autofocus: true,
        mainScrollLeft: 88,
        documentScrollLeft: 88,
    });

    runtime.timers.advance(350);

    assertHorizontalOrigin(runtime, "autofocus after 350ms");
    assert.equal(runtime.body.classList.contains("is-login-keyboard-active"), false);
    assert.equal(runtime.phoneInput.scrollIntoViewCalls, 0);
});

test("focus and blur of both login fields never horizontally scroll the dialog", () => {
    const runtime = createLoginRuntime();

    for (const [name, input, displacedBy] of [
        ["phone", runtime.phoneInput, 44],
        ["pin", runtime.pinInput, 89],
    ]) {
        runtime.focus(input, displacedBy);
        runtime.timers.advance(350);
        assertHorizontalOrigin(runtime, `${name} focus after 350ms`);
        assert.equal(input.scrollIntoViewCalls, 0, `${name} focus must use vertical-only scrolling`);

        runtime.blur(input, displacedBy);
        runtime.timers.advance(350);
        assertHorizontalOrigin(runtime, `${name} blur after 350ms`);
    }
});

test("desktop focus remains stable when the field is already visible", () => {
    const runtime = createLoginRuntime({
        autofocus: true,
        viewportHeight: 720,
    });

    runtime.timers.advance(350);

    assertHorizontalOrigin(runtime, "desktop autofocus");
    assert.deepEqual(runtime.scrollCalls, []);
    assert.equal(runtime.phoneInput.scrollIntoViewCalls, 0);
});

test("shared combined login keeps one geometry and role-specific accent tokens", () => {
    assert.match(
        MOBILE_ROLE_LOGIN_CSS,
        /\.unified-login-dialog\.mobile-role-login\s*\{[^}]*position:\s*fixed;[^}]*height:\s*var\(--login-vv-height, 100dvh\);/s
    );
    assert.match(
        MOBILE_ROLE_LOGIN_CSS,
        /\.login-combined\.login-role-driver\s*\{[^}]*--mobile-login-button-top:\s*#60e3d6;[^}]*--mobile-login-button-bottom:\s*#16998e;/s
    );
    assert.match(
        MOBILE_ROLE_LOGIN_CSS,
        /--mobile-login-accent:\s*var\(--login-accent, #ffd200\);/
    );
    assert.doesNotMatch(MOBILE_ROLE_LOGIN_CSS, /excavator-login/);
});

test("legacy remembered credentials are migrated to phone-only storage", () => {
    const runtime = createLoginRuntime({
        rememberedCredentials: JSON.stringify({phone: "9000000003", pin: "654321"}),
    });

    assert.equal(runtime.phoneInput.value, "900-000-00-03");
    assert.equal(runtime.pinInput.value, "");
    assert.deepEqual(
        JSON.parse(runtime.rememberedCredentials()),
        {phone: "9000000003"}
    );
});

test("malformed remembered credentials are removed instead of retained", () => {
    const runtime = createLoginRuntime({rememberedCredentials: '{"phone":'});

    assert.equal(runtime.phoneInput.value, "");
    assert.equal(runtime.pinInput.value, "");
    assert.equal(runtime.rememberedCredentials(), null);
});

test("combined login Enter moves phone to PIN and submits from PIN", () => {
    const runtime = createLoginRuntime();
    let phonePrevented = false;
    let pinPrevented = false;

    runtime.phoneInput.value = "9990000001";
    runtime.phoneInput.dispatchEvent({type: "input"});
    runtime.phoneInput.dispatchEvent({
        type: "keydown",
        key: "Enter",
        isComposing: false,
        preventDefault() { phonePrevented = true; },
    });

    assert.equal(phonePrevented, true);
    assert.equal(runtime.document.activeElement, runtime.pinInput);

    runtime.pinInput.value = "123456";
    runtime.pinInput.dispatchEvent({type: "input"});
    runtime.pinInput.dispatchEvent({
        type: "keydown",
        key: "Enter",
        isComposing: false,
        preventDefault() { pinPrevented = true; },
    });

    assert.equal(pinPrevented, true);
    assert.equal(runtime.form.requestSubmitCalls, 1);
});
