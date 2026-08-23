#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const catalog = require("../app-catalog-v1.js");
const installer = require("../role-app-install-v1.js");


test("catalog switches to direct navigation only at the mobile breakpoint", () => {
    assert.equal(catalog.usesDirectMobileNavigation({
        matchMedia: () => ({ matches: true })
    }), true);
    assert.equal(catalog.usesDirectMobileNavigation({
        matchMedia: () => ({ matches: false })
    }), false);
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
    const dialog = { querySelector: (selector) => elements[selector] || null };
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
