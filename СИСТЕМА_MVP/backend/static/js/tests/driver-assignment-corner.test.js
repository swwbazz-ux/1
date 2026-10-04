"use strict";
/* v377 (02.10.2026): плашка «Вы назначены на ЭКС-N · ПРИНЯТЬ» во всю ширину
   ложилась на барабан простоев — её нельзя было нажать. Назначение теперь в
   подписи круга и в свободном нижнем левом углу «ПРИНЯТЬ»; тап — событие
   очереди, без сети тоже; срок вышел — круг переключается без перезагрузки. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "..", "..", "..");
const SHIFT = fs.readFileSync(path.resolve(__dirname, "../driver-shift-v1.js"), "utf8").replace(/\r\n/g, "\n");
const SHIFT_TEMPLATE = fs.readFileSync(path.join(ROOT, "templates/users/driver_shift.html"), "utf8");
const CORNERS = fs.readFileSync(path.join(ROOT, "templates/includes/mobile_dial_actions.html"), "utf8");
const SHIFT_CSS = fs.readFileSync(path.join(ROOT, "static/css/driver-shift-v1.css"), "utf8");
const OUTBOX = fs.readFileSync(path.resolve(__dirname, "../driver-offline-outbox-v2.js"), "utf8");

function block(text, signature) {
    const start = text.indexOf(signature);
    assert.notEqual(start, -1, signature);
    let depth = 0;
    for (let i = text.indexOf("{", start); i < text.length; i += 1) {
        if (text[i] === "{") depth += 1;
        else if (text[i] === "}") {
            depth -= 1;
            if (depth === 0) return text.slice(start, i + 1);
        }
    }
    throw new Error(signature);
}

test("the assignment has no screen row of its own: no banner, no grid row", () => {
    assert.doesNotMatch(SHIFT_TEMPLATE, /class="driver-work-assignment"/);
    assert.doesNotMatch(SHIFT_CSS, /"assign"/);
    assert.match(SHIFT_TEMPLATE, /data-driver-assignment-note/);
    assert.match(CORNERS, /data-driver-assignment-accept/);
    assert.match(CORNERS, /type="submit" form="driver-assignment-action"/);
    assert.match(OUTBOX, /"driver\.assignment\.accepted"/);
});

function classList(initial) {
    const set = new Set(initial);
    return {
        contains: (n) => set.has(n), add: (n) => set.add(n), remove: (n) => set.delete(n),
        toggle: (n, on) => { if (on) set.add(n); else set.delete(n); },
    };
}

function element(dataset, classes) {
    const attrs = new Map();
    return {
        dataset: Object.assign({}, dataset), classList: classList(classes || []), disabled: false, hidden: false,
        textContent: "",
        setAttribute: (n, v) => attrs.set(n, String(v)), getAttribute: (n) => (attrs.has(n) ? attrs.get(n) : null),
    };
}

const BLOCK = (() => {
    const start = SHIFT.indexOf("    function driverAssignmentOverrideFromForm(");
    const end = SHIFT.indexOf("    bindAssignmentCountdown();\n    function bindDriverShiftControls()");
    assert.ok(start > 0 && end > start, "override block");
    return SHIFT.slice(start, end);
})();

function runtime({ dialEmpty = true, downtime = "", queued = [], enqueue } = {}) {
    const corner = element({}, ["mobile-dial-action", "mobile-dial-action--spare", "is-assignment"]);
    const form = element({
        driverAssignmentId: "5", driverAssignmentKind: "assign", driverAssignmentTarget: "ЭКС-5",
        driverAssignmentExcavatorId: "57",
    });
    const note = element({});
    const label = element({ driverDialRaw: "ЭКС-1", driverDialFitKey: "k" });
    label.textContent = "ЭКС-1";
    const hold = element({}, [dialEmpty ? "is-empty" : "is-loaded"]);
    const workspace = element({ driverManualPrimaryExcavatorLabel: "ЭКС-1", driverManualExcavatorLabel: "ЭКС-1", driverManualAuthorityType: "assignment" });
    const card = element({ driverActiveDowntimeId: downtime });
    const nodes = {
        "[data-driver-assignment-accept]": corner,
        "#driver-assignment-action": form,
        "[data-driver-assignment-note]": note,
        "[data-driver-dial-label]": label,
        "[data-driver-hold-button]": hold,
        "[data-driver-manual-workspace]": workspace,
        "[data-driver-active-downtime-id]": card,
    };
    const enqueued = [];
    const sounds = [];
    const dispatched = [];
    const listeners = {};
    const win = {
        driverOfflineEvents: queued,
        DriverFreeBucket: {
            currentState: () => ({ active: false }),
            currentCatalog: () => ({
                excavators: [{
                    id: 57, label: "ЭКС-5", complex_label: "К-5", rock_type_id: 4, rock_type: "Руда",
                    loading_horizon: "75", loading_block: "52",
                    dump_points: [{ id: 1, name: "ККД", transport_distance_km: "3.5" }, { id: 2, name: "СКДР" }],
                }],
            }),
        },
        dispatchEvent: (event) => dispatched.push(event),
        addEventListener: (type, fn) => { listeners[type] = fn; },
    };
    const context = vm.createContext({
        Promise, Date, Number, String, Array, Object,
        window: win,
        document: { querySelector: (sel) => nodes[sel] || null },
        CustomEvent: function (type, init) { this.type = type; this.detail = init && init.detail; },
        shell: { querySelector: (sel) => nodes[sel] || null },
        driverOfflineOutbox: {
            enqueue: (event) => { enqueued.push(event); return enqueue ? enqueue(event) : Promise.resolve(event); },
            flush: () => Promise.resolve(),
        },
        generateClientActionId: (prefix) => prefix + "-1",
        playDriverSound: (name) => sounds.push(name),
    });
    vm.runInContext(BLOCK, context);
    return { context, win, corner, form, note, label, workspace, enqueued, sounds, dispatched, listeners };
}

const BASE = {
    source: "driver_manual", authority_type: "assignment", truck_id: 10, excavator_id: 54,
    excavator_label: "ЭКС-1", assignment_id: 3, placement_id: 1, rock_type_id: 4,
    dump_points: [{ id: 1, name: "ККД" }, { id: 3, name: "Отвал" }],
};

function plain(value) { return JSON.parse(JSON.stringify(value)); }

test("a tap on «ПРИНЯТЬ» moves the phone to the new excavator at once and queues the acceptance", async () => {
    const r = runtime();
    const saved = await r.win.driverAcceptAssignmentLocally(r.form);
    assert.equal(saved, true);
    assert.equal(r.corner.classList.contains("is-assignment"), false);
    assert.equal(r.note.hidden, true);
    assert.equal(r.label.textContent, "ЭКС-5");
    assert.equal(r.workspace.dataset.driverManualPrimaryExcavatorLabel, "ЭКС-5");
    assert.equal(r.enqueued.length, 1);
    assert.equal(r.enqueued[0].event_type, "driver.assignment.accepted");
    assert.equal(r.enqueued[0].payload.assignment_id, 5);
    assert.equal(r.enqueued[0].context_snapshot.assignment_override.excavator_id, 57);
    assert.deepEqual(r.sounds, ["action_ok"]);
    assert.ok(r.dispatched.some((event) => event.type === "driver-assignment-context-changed"));

    // Ручной рейс и барабан точек — нового экскаватора.
    const context = plain(r.win.driverAssignmentContextOverride(BASE));
    assert.equal(context.excavator_id, 57);
    assert.equal(context.assignment_id, 5);
    assert.equal(context.placement_id, null);
    assert.deepEqual(context.dump_points.map((point) => point.name), ["ККД", "СКДР"]);
    // Сервер уже показал перевод — переопределять нечего.
    assert.equal(r.win.driverAssignmentContextOverride(Object.assign({}, BASE, { excavator_id: 57, assignment_id: 5 })), null);
});

test("after a restart the queued tap keeps the phone on the new excavator", () => {
    const fresh = runtime();
    const override = plain(fresh.context.driverAssignmentOverrideFromForm(fresh.form));
    const r = runtime({
        queued: [{
            event_type: "driver.assignment.accepted", state: "pending", payload: { assignment_id: 5 },
            context_snapshot: { assignment_override: override },
        }],
    });
    r.win.syncDriverAssignmentCorner(r.win.driverOfflineEvents);
    assert.equal(r.corner.classList.contains("is-assignment"), false);
    assert.equal(r.label.textContent, "ЭКС-5");
    assert.equal(r.win.driverAssignmentContextOverride(BASE).excavator_id, 57);
});

test("assignment confirmation waits for durable enqueue; repeated tap shares the same event", async () => {
    let finish;
    const r = runtime({enqueue: (event) => new Promise((resolve) => { finish = () => resolve(event); })});
    const first = r.win.driverAcceptAssignmentLocally(r.form);
    const second = r.win.driverAcceptAssignmentLocally(r.form);
    await Promise.resolve();
    assert.equal(first, second);
    assert.equal(r.enqueued.length, 1);
    assert.equal(r.form.dataset.driverAssignmentTapped, undefined);
    assert.equal(r.corner.classList.contains("is-assignment"), true);
    assert.equal(r.note.hidden, false);
    assert.equal(r.label.textContent, "ЭКС-1");
    assert.equal(r.win.__driverAssignmentOverride, undefined);
    assert.deepEqual(r.sounds, []);
    finish();
    assert.equal(await first, true);
    assert.equal(r.label.textContent, "ЭКС-5");
    assert.deepEqual(r.sounds, ["action_ok"]);
    assert.equal(await r.win.driverAcceptAssignmentLocally(r.form), true);
    assert.equal(r.enqueued.length, 1);
});

test("quota leaves assignment and manual-trip context unchanged and permits a later retry", async () => {
    let fail = true;
    const r = runtime({enqueue: (event) => fail ? Promise.reject(new Error("quota")) : Promise.resolve(event)});
    await assert.rejects(r.win.driverAcceptAssignmentLocally(r.form), /quota/);
    assert.equal(r.form.dataset.driverAssignmentTapped, undefined);
    assert.equal(r.corner.classList.contains("is-assignment"), true);
    assert.equal(r.note.hidden, false);
    assert.equal(r.label.textContent, "ЭКС-1");
    assert.equal(r.win.__driverAssignmentOverride, undefined);
    assert.equal(r.win.driverAssignmentContextOverride(BASE), null);
    assert.deepEqual(r.sounds, []);
    fail = false;
    assert.equal(await r.win.driverAcceptAssignmentLocally(r.form), true);
    assert.equal(r.label.textContent, "ЭКС-5");
});

test("a fresh server screen after the answer drops the phone override; a queued tap keeps it", async () => {
    const r = runtime();
    await r.win.driverAcceptAssignmentLocally(r.form);
    r.win.driverOfflineEvents = [{ event_type: "driver.assignment.accepted", state: "pending", payload: { assignment_id: 5 } }];
    r.listeners["operational-state-refresh-applied"]();
    assert.ok(r.win.__driverAssignmentOverride, "событие ещё в очереди — держим");
    r.win.driverOfflineEvents = [];
    r.listeners["operational-state-refresh-applied"]();
    assert.equal(r.win.__driverAssignmentOverride, null, "сервер ответил — правда его экран");
});

test("at the deadline the phone switches the same way, without an event", () => {
    const r = runtime();
    vm.runInContext("applyDriverAssignmentDue(form)", Object.assign(r.context, { form: r.form }));
    assert.equal(r.corner.classList.contains("is-assignment"), false);
    assert.equal(r.note.hidden, true);
    assert.equal(r.label.textContent, "ЭКС-5");
    assert.equal(r.enqueued.length, 0);

    const loaded = runtime({ dialEmpty: false });
    vm.runInContext("applyDriverAssignmentDue(form)", Object.assign(loaded.context, { form: loaded.form }));
    assert.equal(loaded.label.textContent, "ЭКС-1", "гружёный круг показывает точку, не экскаватор");

    const idle = runtime({ downtime: "116" });
    vm.runInContext("applyDriverAssignmentDue(form)", Object.assign(idle.context, { form: idle.form }));
    assert.equal(idle.label.textContent, "ЭКС-1", "в простое на круге причина");
});

test("the deadline no longer reloads the whole screen", () => {
    const countdown = block(SHIFT, "    function bindAssignmentCountdown(");
    assert.doesNotMatch(countdown, /location\.reload/);
    assert.match(countdown, /applyDriverAssignmentDue\(form\)/);
});

test("manual trips and the point drum follow the excavator the driver accepted on the phone", () => {
    const workspace = fs.readFileSync(path.resolve(__dirname, "../driver-manual-excavator-workspace-v1.js"), "utf8").replace(/\r\n/g, "\n");
    const drum = fs.readFileSync(path.resolve(__dirname, "../driver-point-drum-v1.js"), "utf8");
    const read = block(workspace, "    function readWorkspaceContext(");
    assert.match(read, /var base = readServerWorkspaceContext\(\);/);
    assert.match(read, /root\.driverAssignmentContextOverride\(base\)/);
    assert.match(drum, /root\.addEventListener\("driver-assignment-context-changed", function \(\) \{ refresh\(\); \}\);/);
    assert.match(SHIFT_TEMPLATE, /data-driver-assignment-excavator-id=/);
});

test("after the tap the countdown stops and the note goes out, for a release too", async () => {
    // Координатор 02.10.2026: подпись «→ ЭКС-2 · 02:32» тикала рядом с уже
    // переключённым кругом; у снятия назначения она и вовсе оставалась на виду.
    const r = runtime();
    r.form.dataset.driverAssignmentKind = "release";
    r.form.dataset.driverAssignmentExcavatorId = "";
    await r.win.driverAcceptAssignmentLocally(r.form);
    assert.equal(r.corner.classList.contains("is-assignment"), false);
    assert.equal(r.note.hidden, true);
    assert.equal(r.label.textContent, "ЭКС-1", "снятие не придумывает новый экскаватор");
    assert.equal(vm.runInContext("driverAssignmentTapped(form)", Object.assign(r.context, { form: r.form })), true);

    // После перезапуска тап виден только в очереди — подпись гаснет так же.
    const restarted = runtime({
        queued: [{ event_type: "driver.assignment.accepted", state: "pending", payload: { assignment_id: 5 } }],
    });
    restarted.win.syncDriverAssignmentCorner(restarted.win.driverOfflineEvents);
    assert.equal(restarted.note.hidden, true);
    assert.equal(restarted.corner.classList.contains("is-assignment"), false);

    const countdown = block(SHIFT, "    function bindAssignmentCountdown(");
    assert.match(countdown, /if \(driverAssignmentTapped\(form\)\) \{\s*window\.clearInterval\(timerId\);\s*hideDriverAssignmentNotice\(\);/);
});
