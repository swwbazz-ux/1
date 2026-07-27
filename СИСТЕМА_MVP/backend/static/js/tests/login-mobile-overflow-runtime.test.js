"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const BACKEND_ROOT = path.resolve(__dirname, "..", "..", "..");
const LOGIN_TEMPLATE_PATH = path.join(BACKEND_ROOT, "templates", "users", "login.html");
const APP_CSS_PATH = path.join(BACKEND_ROOT, "static", "css", "app.css");
const LOGIN_TEMPLATE = fs.readFileSync(LOGIN_TEMPLATE_PATH, "utf8");
const APP_CSS = fs.readFileSync(APP_CSS_PATH, "utf8");

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
    const start = LOGIN_TEMPLATE.indexOf("document.addEventListener('DOMContentLoaded'");
    const serviceWorkerStart = LOGIN_TEMPLATE.indexOf('    if ("serviceWorker" in navigator)', start);
    assert.notEqual(start, -1, "login DOMContentLoaded runtime must exist");
    assert.notEqual(serviceWorkerStart, -1, "service worker boundary must exist");
    return LOGIN_TEMPLATE.slice(start, serviceWorkerStart) + "\n});";
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
    const timers = createTimers();
    const phoneInput = createInput("login-phone", "data-phone-input");
    const pinInput = createInput("login-pin", "data-pin-input");
    const submitButton = {disabled: false};
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
        querySelector(selector) {
            if (selector === 'button[type="submit"]') return submitButton;
            if (selector === "[data-phone-input]") return phoneInput;
            if (selector === "[data-pin-input]") return pinInput;
            return null;
        },
        querySelectorAll() {
            return [phoneInput, pinInput];
        },
        contains(element) {
            return element === phoneInput || element === pinInput;
        },
    });
    const documentTarget = createEventTarget();
    const document = Object.assign(documentTarget, {
        body,
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
    const window = {
        innerHeight: viewportHeight,
        scrollY: 0,
        visualViewport: Object.assign(viewportTarget, {
            height: viewportHeight,
            offsetTop: 0,
        }),
        setTimeout: timers.setTimeout,
        clearTimeout: timers.clearTimeout,
        scrollTo(optionsValue) {
            scrollCalls.push(optionsValue);
            if (optionsValue && typeof optionsValue.left !== "undefined") {
                document.scrollingElement.scrollLeft = Number(optionsValue.left);
            }
        },
    };
    window.window = window;
    window.document = document;

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

test("natural autofocus and delayed correction keep the mobile login at x=0", () => {
    const runtime = createLoginRuntime({
        autofocus: true,
        mainScrollLeft: 88,
        documentScrollLeft: 88,
    });

    runtime.timers.advance(350);

    assertHorizontalOrigin(runtime, "autofocus after 350ms");
    assert.equal(runtime.body.classList.contains("is-login-keyboard-active"), true);
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
