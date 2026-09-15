"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const base = fs.readFileSync(path.resolve(__dirname, "../../../templates/base.html"), "utf8");
const fragmentSource = base.split("/* OPERATIONAL_FRAGMENT_CLIENT_START */")[1].split("/* OPERATIONAL_FRAGMENT_CLIENT_END */")[0];
const indicatorSource = fs.readFileSync(path.resolve(__dirname, "../connection-indicators-v1.js"), "utf8");

function fragment(response) {
    const window = {
        location: {href: "https://fixture.invalid/driver/?tab=work"},
        fetch: async () => response,
        setTimeout, clearTimeout, AbortController,
        scrollX: 3, scrollY: 90,
        scrollTo(x, y) { this.restoredScroll = [x, y]; }
    };
    vm.runInNewContext(fragmentSource, {window, document: {}, URL, Promise, Error});
    return window;
}
function response(payload = {contract: "operational-fragment-v1", screen: "driver", version: 7, html: "<main></main>"}, extra = {}) {
    return {ok: true, status: 200, headers: {get: () => "application/json; charset=utf-8"}, json: async () => payload, ...extra};
}

test("fragment rejects redirected login JSON and HTML content type before accepting a version", async () => {
    for (const extra of [{redirected: true}, {headers: {get: () => "text/html"}}, {headers: {get: () => null}}]) {
        await assert.rejects(fragment(response(undefined, extra)).AppOperationalFragment.request("driver", 7), {code: "OPERATIONAL_FRAGMENT_INVALID_RESPONSE"});
    }
});

test("fragment requires a finite integer server version and rejects missing or old versions", async () => {
    for (const version of [undefined, null, "7", -1, 0.5, NaN, Infinity, 6]) {
        const payload = {contract: "operational-fragment-v1", screen: "driver", version, html: "<main></main>"};
        await assert.rejects(fragment(response(payload)).AppOperationalFragment.request("driver", 7), {code: "OPERATIONAL_FRAGMENT_INVALID_PAYLOAD"});
    }
    const received = await fragment(response()).AppOperationalFragment.request("driver", 7);
    assert.equal(received.version, 7);
});

test("view restoration follows panel keys across reordered fragment DOM", () => {
    const window = fragment(response());
    const panel = (key, top, left) => ({getAttribute: () => key, scrollTop: top, scrollLeft: left});
    const oldPanels = [panel("work", 32, 0), panel("shift", 340, 9)];
    const oldRoot = {scrollTop: 17, scrollLeft: 1, querySelectorAll: () => oldPanels};
    const snapshot = window.AppOperationalFragment.captureView(oldRoot, "[data-tab]", "data-tab");
    const newPanels = [panel("shift", 0, 0), panel("work", 0, 0), panel("new", 0, 0)];
    const newRoot = {scrollTop: 0, scrollLeft: 0, querySelectorAll: () => newPanels};
    window.AppOperationalFragment.restoreView(newRoot, snapshot);
    assert.deepEqual(newPanels.map(p => [p.scrollTop, p.scrollLeft]), [[340, 9], [32, 0], [0, 0]]);
    assert.equal(newRoot.scrollTop, 17);
    assert.deepEqual(window.restoredScroll, [3, 90]);
});

test("both role indicators use the same labels and recovering never says connection lost", () => {
    const listeners = new Map();
    const node = () => ({attrs: {"aria-hidden": "true"}, setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; }});
    const indicators = [node(), node()];
    const banner = {textContent: ""};
    const document = {body: {dataset: {}}, readyState: "complete", querySelectorAll: () => indicators, querySelector: () => banner};
    const window = {addEventListener: (name, fn) => listeners.set(name, fn)};
    vm.runInNewContext(indicatorSource, {window, document});
    assert.equal(banner.textContent, "Проверяем связь…");
    for (const [state, text] of [["weak", "Переподключение…"], ["lost", "Нет связи с сервером"], ["recovering", "Восстанавливаем данные"], ["ok", "Связь есть"]]) {
        document.body.dataset.connectionState = state;
        listeners.get("operational-state-connection")();
        assert.equal(banner.textContent, text);
        for (const indicator of indicators) {
            assert.equal(indicator.attrs["aria-label"], text);
            assert.equal(indicator.attrs["aria-hidden"], undefined);
        }
    }
    const replacement = node(); indicators[0] = replacement;
    listeners.get("operational-state-refresh-applied")();
    assert.equal(replacement.attrs.title, "Связь есть");
});

test("connection renderer preserves the authentication-ended banner", () => {
    const listeners = new Map();
    const banner = {textContent: ""};
    const document = {body: {dataset: {connectionState: "weak"}}, readyState: "complete", querySelectorAll: () => [], querySelector: () => banner};
    vm.runInNewContext(indicatorSource, {document, window: {addEventListener: (name, fn) => listeners.set(name, fn)}});
    listeners.get("app-authentication-ended")();
    banner.textContent = "Сессия завершена. Открываем экран входа…";
    listeners.get("operational-state-connection")();
    assert.equal(banner.textContent, "Сессия завершена. Открываем экран входа…");
});
