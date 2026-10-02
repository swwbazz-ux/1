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

function runtime({ dialEmpty = true, downtime = "" } = {}) {
    const corner = element({}, ["mobile-dial-action", "mobile-dial-action--spare", "is-assignment"]);
    const form = element({ driverAssignmentId: "5", driverAssignmentKind: "assign", driverAssignmentTarget: "ЭКС-5" });
    const note = element({});
    const label = element({ driverDialRaw: "ЭКС-1", driverDialFitKey: "k" });
    label.textContent = "ЭКС-1";
    const hold = element({}, [dialEmpty ? "is-empty" : "is-loaded"]);
    const nodes = {
        "[data-driver-assignment-accept]": corner,
        "#driver-assignment-action": form,
        "[data-driver-assignment-note]": note,
        "[data-driver-dial-label]": label,
        "[data-driver-hold-button]": hold,
    };
    const enqueued = [];
    const sounds = [];
    const context = vm.createContext({
        Promise, Date, Number, String,
        window: { driverOfflineEvents: [] },
        shell: { querySelector: (sel) => nodes[sel] || null },
        downtimeCard: { dataset: { driverActiveDowntimeId: downtime } },
        driverOfflineOutbox: {
            enqueue: (event) => { enqueued.push(event); return Promise.resolve(event); },
            flush: () => Promise.resolve(),
        },
        generateClientActionId: (prefix) => prefix + "-1",
        playDriverSound: (name) => sounds.push(name),
    });
    vm.runInContext([
        block(SHIFT, "    function hideDriverAssignmentCorner("),
        block(SHIFT, "    window.syncDriverAssignmentCorner = function") + ";",
        block(SHIFT, "    window.driverAcceptAssignmentLocally = function") + ";",
        block(SHIFT, "    function applyDriverAssignmentDue("),
    ].join("\n"), context);
    return { context, corner, form, note, label, enqueued, sounds };
}

test("a tap on «ПРИНЯТЬ» queues the acceptance and puts the corner out at once, without the server", async () => {
    const r = runtime();
    const saved = await r.context.window.driverAcceptAssignmentLocally(r.form);
    assert.equal(saved, true);
    assert.equal(r.corner.classList.contains("is-assignment"), false);
    assert.equal(r.corner.disabled, true);
    assert.equal(r.enqueued.length, 1);
    assert.equal(r.enqueued[0].event_type, "driver.assignment.accepted");
    assert.equal(r.enqueued[0].payload.assignment_id, 5);
    assert.deepEqual(r.sounds, ["action_ok"]);
});

test("a server screen drawn before the acceptance reached it does not bring the corner back", () => {
    const r = runtime();
    r.context.window.syncDriverAssignmentCorner([
        { event_type: "driver.assignment.accepted", state: "pending", payload: { assignment_id: 5 } },
    ]);
    assert.equal(r.corner.classList.contains("is-assignment"), false);

    const other = runtime();
    other.context.window.syncDriverAssignmentCorner([
        { event_type: "driver.assignment.accepted", state: "pending", payload: { assignment_id: 4 } },
    ]);
    assert.equal(other.corner.classList.contains("is-assignment"), true, "чужое назначение угол не гасит");
});

test("at the deadline the corner goes out and the empty dial names the new excavator at once", () => {
    const r = runtime();
    vm.runInContext("applyDriverAssignmentDue(form)", Object.assign(r.context, { form: r.form }));
    assert.equal(r.corner.classList.contains("is-assignment"), false);
    assert.equal(r.note.hidden, true);
    assert.equal(r.label.textContent, "ЭКС-5");
    assert.equal(r.label.dataset.driverDialRaw, "ЭКС-5");
    assert.equal(r.label.dataset.driverDialFitKey, undefined);

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
