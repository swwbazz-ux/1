"use strict";
/* Бой 03.10.2026, самосвал 22: «несколько раз жал разгрузку, и только с 5-го
   раза высветилось ехать к экскаватору». Связь «recovering» дольше минуты —
   самовосстановление экрана открывает окно, в котором признаки начатого жеста
   обновление больше не держат. Свежий экран приходил посреди удержания круга,
   оболочка подменялась вместе с кнопкой, удержание снималось (destroy в
   bindDriverMobileShell) — разгрузка молча не засчитывалась, в очереди пусто.
   Палец на круге — жёсткая причина: ни обход самовосстановления, ни его
   перезагрузка через три минуты удержание не обрывают. Залипший признак
   старше 5 с обновление не держит. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SELF_HEAL_SOURCE = fs.readFileSync(path.resolve(__dirname, "../driver-self-heal-v1.js"), "utf8");
const VOICE_SOURCE = fs.readFileSync(path.resolve(__dirname, "../driver-shift-voice-v1.js"), "utf8");
const SHIFT_SOURCE = fs.readFileSync(path.resolve(__dirname, "../driver-shift-v1.js"), "utf8").replace(/\r\n/g, "\n");

const MINUTE = 60000;

function screen() {
    const calls = {reconciles: [], reloads: 0};
    let clock = 1000000;
    const shell = {
        dataset: {},
        contains: () => false,
        querySelector(selector) {
            // Кольцо набирается: на кнопке круга класс is-holding.
            if (selector.startsWith(".is-touch-armed")) return {tagName: "BUTTON", className: "driver-work-dial-button is-holding"};
            return null;
        },
    };
    const documentStub = {
        hidden: false,
        readyState: "complete",
        activeElement: null,
        body: {dataset: {}},
        addEventListener() {},
        querySelector(selector) {
            return selector === "[data-driver-shell]" ? shell : null;
        },
    };
    const windowStub = {
        setInterval: () => 1,
        clearInterval: () => {},
        addEventListener() {},
        sessionStorage: {
            values: new Map(),
            getItem(key) { return this.values.has(key) ? this.values.get(key) : null; },
            setItem(key, value) { this.values.set(key, value); },
        },
        location: {reload() { calls.reloads += 1; }},
        showDriverToast() {},
        AppRealtime: {
            getDebugState: () => ({pendingVersion: 44, observedVersion: 44, connectionState: "recovering"}),
            requestReconcile(reason) { calls.reconciles.push(reason); },
        },
    };
    const fakeDate = {now: () => clock};
    const context = {window: windowStub, document: documentStub, Date: fakeDate, Number, String, Math};
    windowStub.Date = fakeDate;
    vm.runInNewContext(`${VOICE_SOURCE}\n${SELF_HEAL_SOURCE}\ncontext.isUnsafe = isDriverOperationalRefreshUnsafe;`, {
        ...context,
        context,
    });
    return {
        calls,
        window: windowStub,
        isUnsafe: () => context.isUnsafe(shell),
        tick: () => windowStub.driverSelfHeal.tick(),
        advance(ms) { clock += ms; },
        now: () => clock,
    };
}

function behindForAMinute(app) {
    app.tick();
    app.advance(MINUTE + 1000);
    app.tick();
    assert.deepEqual(app.calls.reconciles, ["driver_self_heal"], "окно обхода открыто");
}

test("self-heal bypass does not swap the screen under a finger holding the dial", () => {
    const app = screen();
    behindForAMinute(app);
    app.window.driverDialHoldStartedAt = app.now();
    app.advance(200);
    assert.equal(app.isUnsafe(), true, "удержание идёт — подмена экрана сняла бы его");
    assert.equal(app.window.driverRefreshBusyReason, "dial_hold");
});

test("without a finger on the dial the bypass still pushes a stuck screen through", () => {
    const app = screen();
    behindForAMinute(app);
    app.window.driverDialHoldStartedAt = 0;
    assert.equal(app.isUnsafe(), false, "залипший is-holding без удержания — мягкая причина");
});

test("a stale hold mark cannot freeze the screen", () => {
    const app = screen();
    behindForAMinute(app);
    app.window.driverDialHoldStartedAt = app.now();
    app.advance(5000);
    assert.equal(app.isUnsafe(), false);
});

test("the three-minute reload waits for the finger to leave the dial", () => {
    const app = screen();
    app.tick();
    app.advance(3 * MINUTE + 1000);
    app.window.driverDialHoldStartedAt = app.now();
    app.tick();
    assert.equal(app.calls.reloads, 0, "перезагрузка посреди удержания съела бы разгрузку");
    app.window.driverDialHoldStartedAt = 0;
    app.advance(5000);
    app.tick();
    assert.equal(app.calls.reloads, 1);
});

test("the unload hold marks the finger on start and clears it on reset and on completion", () => {
    const start = SHIFT_SOURCE.indexOf("unloadHoldGuard = window.createDriverRoleHoldGuard({");
    assert.notEqual(start, -1);
    const options = SHIFT_SOURCE.slice(start, SHIFT_SOURCE.indexOf("window.driverUnloadHoldGuard = unloadHoldGuard;", start));
    const section = (name) => {
        const from = options.indexOf(name + ": function () {");
        assert.notEqual(from, -1, name);
        return options.slice(from, from + 400);
    };
    assert.match(section("onStart"), /window\.driverDialHoldStartedAt = Date\.now\(\);/);
    assert.match(section("onReset"), /window\.driverDialHoldStartedAt = 0;/);
    assert.match(section("onComplete"), /window\.driverDialHoldStartedAt = 0;/);
});
