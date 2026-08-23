#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const catalog = require("../app-catalog-v1.js");
const installer = require("../role-app-install-v1.js");


test("catalog card always opens connection dialog on mobile", () => {
    const cardListeners = {};
    const dialogListeners = {};
    const elements = {
        "[data-dialog-title]": { textContent: "" },
        "[data-dialog-url]": { value: "" },
        "[data-dialog-qr]": { alt: "", src: "" },
        "[data-dialog-qr-target]": { textContent: "" },
        "[data-dialog-open]": { href: "" },
        "[data-copy-status]": { textContent: "" },
        "[data-dialog-close]": null,
        "[data-copy-link]": null,
        "[data-share-link]": null
    };
    const dialog = {
        dataset: {},
        opened: false,
        querySelector: (selector) => elements[selector] || null,
        addEventListener: (name, handler) => { dialogListeners[name] = handler; },
        showModal() { this.opened = true; }
    };
    const card = {
        href: "https://driver.driverform.ru/",
        dataset: {
            appName: "Водитель самосвала",
            appUrl: "https://driver.driverform.ru/",
            appQr: "/static/img/pwa/qr/driver.png",
            appQrTarget: "https://driver.driverform.ru/"
        },
        addEventListener: (name, handler) => { cardListeners[name] = handler; }
    };
    const doc = {
        querySelector: () => dialog,
        querySelectorAll: () => [card]
    };
    const win = {
        navigator: {},
        matchMedia: () => ({ matches: true }),
        document: {}
    };
    let prevented = false;

    catalog.init(win, doc);
    cardListeners.click({ preventDefault: () => { prevented = true; } });

    assert.equal(prevented, true);
    assert.equal(dialog.opened, true);
    assert.equal(dialog.dataset.appName, "Водитель самосвала");
    assert.equal(elements["[data-dialog-open]"].href, "https://driver.driverform.ru/");
});


test("dialog receives the exact server URL and local QR endpoint", () => {
    const values = {};
    const elements = {
        "[data-dialog-title]": { textContent: "" },
        "[data-dialog-url]": { value: "" },
        "[data-dialog-qr]": { alt: "", src: "" },
        "[data-dialog-qr-target]": { textContent: "" },
        "[data-dialog-open]": { href: "" },
        "[data-copy-status]": { textContent: "old" }
    };
    const dialog = {
        dataset: {},
        querySelector: (selector) => elements[selector] || null
    };
    const card = {
        href: "https://driver.driverform.ru/",
        dataset: {
            appName: "Водитель самосвала",
            appUrl: "https://driver.driverform.ru/",
            appQr: "/static/img/pwa/qr/driver.png",
            appQrTarget: "https://driver.driverform.ru/"
        }
    };

    catalog.setDialogDetails(dialog, card);

    assert.equal(elements["[data-dialog-title]"].textContent, "Водитель самосвала");
    assert.equal(dialog.dataset.appName, "Водитель самосвала");
    assert.equal(dialog.dataset.appUrl, "https://driver.driverform.ru/");
    assert.equal(elements["[data-dialog-url]"].value, "https://driver.driverform.ru/");
    assert.equal(elements["[data-dialog-open]"].href, "https://driver.driverform.ru/");
    assert.equal(elements["[data-dialog-qr]"].src, "/static/img/pwa/qr/driver.png");
    assert.equal(
        elements["[data-dialog-qr-target]"].textContent,
        "QR откроет: https://driver.driverform.ru/"
    );
    assert.match(elements["[data-dialog-qr]"].alt, /Водитель самосвала/);
    assert.equal(values.status, undefined);
});


test("share uses Android share sheet and falls back to copying the link", async () => {
    const shared = [];
    const nativeResult = await catalog.shareValue({
        navigator: { share: (payload) => { shared.push(payload); return Promise.resolve(); } }
    }, "Горный мастер", "https://mining-master.driverform.ru/", null);

    assert.equal(nativeResult, "shared");
    assert.equal(shared.length, 1);
    assert.equal(shared[0].url, "https://mining-master.driverform.ru/");
    assert.match(shared[0].text, /установите приложение/);

    const copied = [];
    const input = { value: "https://driver.driverform.ru/" };
    const fallbackResult = await catalog.shareValue({
        navigator: { clipboard: { writeText: (value) => { copied.push(value); return Promise.resolve(); } } }
    }, "Водитель", input.value, input);

    assert.equal(fallbackResult, "copied");
    assert.deepEqual(copied, ["https://driver.driverform.ru/"]);
});


test("installer identifies standalone mode and gives platform instructions", () => {
    assert.equal(installer.isStandalone({
        matchMedia: () => ({ matches: true }),
        navigator: {}
    }), true);
    assert.match(
        installer.manualInstruction({ navigator: { userAgent: "iPhone" } }),
        /Поделиться/
    );
    assert.match(
        installer.manualInstruction({ navigator: { userAgent: "Android Chrome" } }),
        /Установить приложение/
    );
    assert.equal(installer.isAndroidYandex({
        navigator: { userAgent: "Mozilla/5.0 (Linux; Android 14) YaBrowser/25.8.2.100 Mobile" }
    }), true);
    assert.match(
        installer.manualInstruction({
            navigator: { userAgent: "Mozilla/5.0 (Linux; Android 14) YaBrowser/25.8.2.100 Mobile" }
        }),
        /Google Chrome/
    );
});


test("Yandex Android browser mode warning is visible only outside standalone", async () => {
    const closeListeners = {};
    const copyListeners = {};
    const copyStatus = { textContent: "" };
    const closeButton = {
        addEventListener: (name, handler) => { closeListeners[name] = handler; }
    };
    const copyButton = {
        textContent: "Скопировать адрес",
        addEventListener: (name, handler) => { copyListeners[name] = handler; }
    };
    const warning = {
        hidden: true,
        querySelector: (selector) => ({
            "[data-pwa-browser-warning-close]": closeButton,
            "[data-pwa-browser-warning-copy]": copyButton,
            "[data-pwa-browser-warning-copy-status]": copyStatus
        }[selector] || null)
    };
    const copied = [];
    const win = {
        location: { origin: "https://mining-master.driverform.ru" },
        navigator: {
            userAgent: "Mozilla/5.0 (Linux; Android 14) YaBrowser/25.8.2.100 Mobile",
            clipboard: { writeText: (value) => { copied.push(value); return Promise.resolve(); } }
        },
        matchMedia: () => ({ matches: false })
    };
    const doc = {
        querySelector: (selector) => selector === "[data-pwa-browser-mode-warning]" ? warning : null
    };

    assert.equal(installer.initBrowserModeWarning(win, doc), true);
    assert.equal(warning.hidden, false);
    copyListeners.click();
    await Promise.resolve();
    assert.deepEqual(copied, ["https://mining-master.driverform.ru/"]);
    assert.equal(copyButton.textContent, "Адрес скопирован");
    assert.match(copyStatus.textContent, /Google Chrome/);
    closeListeners.click();
    assert.equal(warning.hidden, true);

    const standaloneWin = Object.assign({}, win, {
        matchMedia: () => ({ matches: true })
    });
    warning.hidden = true;
    assert.equal(installer.initBrowserModeWarning(standaloneWin, doc), false);
    assert.equal(warning.hidden, true);
});


test("installer captures the browser prompt and starts it only on button click", async () => {
    const windowListeners = {};
    const buttonListeners = {};
    const classes = new Set();
    const button = {
        disabled: false,
        textContent: "Установить приложение",
        addEventListener: (name, handler) => { buttonListeners[name] = handler; }
    };
    const status = { textContent: "" };
    const panel = {
        classList: {
            add: (name) => classes.add(name),
            remove: (name) => classes.delete(name)
        },
        querySelector: (selector) => selector === "[data-install-button]" ? button : status
    };
    const win = {
        document: {},
        navigator: { userAgent: "Android Chrome" },
        matchMedia: () => ({ matches: false }),
        addEventListener: (name, handler) => { windowListeners[name] = handler; }
    };
    const doc = { querySelector: () => panel };
    let prevented = false;
    let prompted = false;
    const event = {
        preventDefault: () => { prevented = true; },
        prompt: () => { prompted = true; },
        userChoice: Promise.resolve({ outcome: "accepted" })
    };

    installer.init(win, doc);
    windowListeners.beforeinstallprompt(event);
    assert.equal(prevented, true);
    assert.equal(prompted, false);
    assert.equal(classes.has("is-ready"), true);

    buttonListeners.click();
    await event.userChoice;
    await Promise.resolve();
    assert.equal(prompted, true);
    assert.match(status.textContent, /Установка началась/);
});
