"use strict";
/* Экран водителя замирал навсегда, если признак «занят» залипал: обновление
   откладывалось раз в секунду без конца, связь горела синим, круг оставался
   пустым при уже загруженном рейсе. Эти тесты закрепляют аварийный выход и
   его границы — он не должен срабатывать ни на здоровом экране, ни без связи,
   ни поверх того, что водитель вводит руками. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SELF_HEAL_SOURCE = fs.readFileSync(path.resolve(__dirname, "../driver-self-heal-v1.js"), "utf8");
const VOICE_SOURCE = fs.readFileSync(path.resolve(__dirname, "../driver-shift-voice-v1.js"), "utf8");

const MINUTE = 60000;

function screen(options = {}) {
    const calls = {reconciles: [], reloads: 0, toasts: []};
    let clock = 1000000;
    const softBlock = options.softBlock === true;
    const shell = {
        dataset: {},
        contains: () => false,
        querySelector(selector) {
            if (selector === ".driver-shift-opening-form") return options.openingForm || null;
            if (selector === "[data-driver-shift-close-form]") return options.closeForm || null;
            if (selector.startsWith(".is-touch-armed")) return softBlock ? {} : null;
            if (selector.startsWith("[data-driver-point-sheet]")) return options.sheetOpen ? {} : null;
            return null;
        },
    };
    const documentStub = {
        hidden: options.hidden === true,
        readyState: "complete",
        activeElement: options.activeElement || null,
        body: {dataset: {}},
        addEventListener() {},
        querySelector(selector) {
            if (selector === "[data-driver-shell]") return options.shellMissing ? null : shell;
            return null;
        },
    };
    const windowStub = {
        setInterval: () => 1,
        clearInterval: () => {},
        addEventListener(type, handler) {
            (windowStub.listeners[type] = windowStub.listeners[type] || []).push(handler);
        },
        listeners: {},
        sessionStorage: {
            values: new Map(),
            getItem(key) { return this.values.has(key) ? this.values.get(key) : null; },
            setItem(key, value) { this.values.set(key, value); },
        },
        location: {reload() { calls.reloads += 1; }},
        showDriverToast(text) { calls.toasts.push(text); },
        AppRealtime: {
            getDebugState: () => ({
                pendingVersion: options.pendingVersion === undefined ? 44 : options.pendingVersion,
                observedVersion: 44,
                connectionState: options.connectionState || "recovering",
            }),
            requestReconcile(reason) { calls.reconciles.push(reason); },
        },
    };
    const fakeDate = {now: () => clock};
    const context = {
        window: windowStub,
        document: documentStub,
        Date: fakeDate,
        Number,
        String,
        Math,
    };
    context.window.Date = fakeDate;
    vm.runInNewContext(`${VOICE_SOURCE}\n${SELF_HEAL_SOURCE}\ncontext.isUnsafe = isDriverOperationalRefreshUnsafe;`, {
        ...context,
        context,
    });
    return {
        calls,
        shell,
        window: windowStub,
        document: documentStub,
        isUnsafe: () => context.isUnsafe(shell),
        tick: () => windowStub.driverSelfHeal.tick(),
        advance(ms) { clock += ms; },
        emit(type, detail) {
            (windowStub.listeners[type] || []).forEach((handler) => handler({detail}));
        },
    };
}

test("a screen left behind the server is pushed through after a minute, not before", () => {
    const app = screen({softBlock: true});
    app.tick();
    assert.equal(app.isUnsafe(), true, "залипший жест держит обновление, пока отставание свежее");

    app.advance(30000);
    app.tick();
    assert.deepEqual(app.calls.reconciles, [], "полминуты — ещё не повод вмешиваться");

    app.advance(31000);
    app.tick();
    assert.deepEqual(app.calls.reconciles, ["driver_self_heal"]);
    assert.equal(app.isUnsafe(), false, "после минуты мягкая причина обновление больше не держит");
    assert.equal(app.window.driverForceFragmentApply, true);
    assert.deepEqual(app.calls.toasts, ["Экран отстал от сервера — обновляем."]);
    assert.equal(app.calls.reloads, 0, "перезагрузка — только следующая ступень");
});

test("a screen that caught up closes the bypass window and starts counting anew", () => {
    const app = screen({softBlock: true});
    app.tick();
    app.advance(2 * MINUTE);
    app.tick();
    assert.equal(app.isUnsafe(), false);

    app.emit("operational-state-refresh-applied", {});
    assert.equal(app.window.driverRefreshBypassBusyUntil, 0);
    assert.equal(app.isUnsafe(), true, "здоровый экран снова уважает начатый жест");
});

test("three minutes behind reloads the screen once, then holds off", () => {
    const app = screen();
    app.tick();
    app.advance(3 * MINUTE + 1000);
    app.tick();
    assert.equal(app.calls.reloads, 1);

    app.advance(MINUTE);
    app.tick();
    assert.equal(app.calls.reloads, 1, "повторная перезагрузка не чаще раза в три минуты");
});

test("without a live connection nothing is forced and nothing is reloaded", () => {
    for (const connectionState of ["lost", "weak", "unknown", "ok"]) {
        const app = screen({connectionState});
        app.tick();
        app.advance(5 * MINUTE);
        app.tick();
        assert.deepEqual(app.calls.reconciles, [], `состояние ${connectionState}: лечить нечего`);
        assert.equal(app.calls.reloads, 0, `состояние ${connectionState}: телефон в карьере не перезагружаем`);
    }
});

test("a screen that is not behind the server is left alone", () => {
    const app = screen({pendingVersion: null});
    app.tick();
    app.advance(10 * MINUTE);
    app.tick();
    assert.deepEqual(app.calls.reconciles, []);
    assert.equal(app.calls.reloads, 0);
});

test("a hidden screen never accumulates lateness", () => {
    const app = screen({hidden: true});
    app.tick();
    app.advance(10 * MINUTE);
    app.tick();
    assert.deepEqual(app.calls.reconciles, []);
    assert.equal(app.calls.reloads, 0);
});

test("what the driver is typing is never interrupted", () => {
    const dirtyClose = {dataset: {driverShiftDirty: "true"}, querySelector: () => null};
    const app = screen({closeForm: dirtyClose, softBlock: true});
    app.tick();
    app.advance(5 * MINUTE);
    app.tick();
    assert.deepEqual(app.calls.reconciles, []);
    assert.equal(app.calls.reloads, 0);
    assert.equal(app.isUnsafe(), true);
});

test("an open sheet is pushed through but never reloaded from under the finger", () => {
    const app = screen({sheetOpen: true});
    app.tick();
    app.advance(5 * MINUTE);
    app.tick();
    assert.deepEqual(app.calls.reconciles, ["driver_self_heal"], "продавить обновление при открытой шторке можно");
    assert.equal(app.calls.reloads, 0, "а перезагрузить — нет: выбор водителя ещё не отправлен");
});

test("the last deferral reason stays readable on the page", () => {
    const app = screen();
    app.emit("operational-state-refresh-deferred", {role: "driver", reason: "driver_busy"});
    assert.equal(app.document.body.dataset.driverRefreshDeferReason, "driver_busy");
    app.emit("operational-state-refresh-deferred", {role: "dispatcher", reason: "board_busy"});
    assert.equal(app.document.body.dataset.driverRefreshDeferReason, "driver_busy", "чужая роль не перебивает причину водителя");
});
