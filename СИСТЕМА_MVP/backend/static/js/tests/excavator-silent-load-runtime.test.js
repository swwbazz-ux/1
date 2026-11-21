"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");


const templatePath = path.resolve(
    __dirname,
    "..",
    "..",
    "..",
    "templates",
    "trips",
    "excavator_work.html"
);
const template = fs.readFileSync(templatePath, "utf8");


function extractBraceBlock(source, signature, label) {
    const start = source.indexOf(signature);
    assert.notEqual(start, -1, `${label} signature was not found.`);
    const openBrace = source.indexOf("{", start + signature.length);
    assert.notEqual(openBrace, -1, `${label} opening brace was not found.`);

    let depth = 0;
    for (let index = openBrace; index < source.length; index += 1) {
        if (source[index] === "{") depth += 1;
        if (source[index] === "}") depth -= 1;
        if (depth === 0) return source.slice(start, index + 1);
    }
    assert.fail(`${label} closing brace was not found.`);
}


function createOutbox(queued) {
    return {
        pending: () => Promise.resolve([]),
        allocateSequence: () => Promise.resolve(1),
        queue(event) {
            queued.push(event);
            return Promise.resolve(event);
        },
        flush: () => Promise.resolve([]),
    };
}


function fixture({withOutbox = true} = {}) {
    const notices = [], sounds = [], voices = [], statuses = [], queued = [], confirmed = [];
    const classNames = new Set();
    const card = {
        dataset: {truckId: "7", assignmentId: "8", eoCanLoad: "1", eoFreeBucket: "0"},
        className: "eo-truck-card",
        hidden: false,
        classList: {
            add: (...names) => names.forEach(name => classNames.add(name)),
            remove: (...names) => names.forEach(name => classNames.delete(name)),
            contains: name => classNames.has(name),
        },
        setAttribute() {},
        querySelector: () => null,
        appendChild() {},
    };
    const dumpTarget = {
        dataset: {eoDumpTarget: "9", eoDumpName: "ККД"},
        querySelector: () => null,
    };
    const shell = {
        dataset: {
            eoCurrentExcavatorId: "3", eoAccessId: "7", eoEmployeeId: "17",
            nativeShiftId: "11", eoRoleCode: "excavator_operator",
        },
        querySelector: () => null,
        querySelectorAll: () => [],
    };
    let idCount = 0;
    const context = {
        window: {isAppRoleReadonly: () => false},
        document: {
            hidden: false,
            querySelector: () => null,
            querySelectorAll: () => [],
            createElement: () => ({dataset: {}, append() {}, setAttribute() {}}),
        },
        navigator: {onLine: false},
        Promise, Math, Number, String, Boolean, Array, Object, Date,
        shell, card, dumpTarget, freeBucketController: null,
        downtimeInput: {value: ""}, transportDistanceInput: {value: "3.5"},
        fieldOutbox: withOutbox ? createOutbox(queued) : null,
        canTruckLoad: () => true,
        truckLoadBlockReason: () => "",
        snapshotTruckCard: () => ({className: card.className}),
        applyTruckPending(node) {node.dataset.eoEquipmentState = "loaded_waiting_unload";},
        addPendingTruckBadge: () => null,
        confirmTruckLoaded(node) {confirmed.push(node);},
        restoreTruckCard() {},
        playExcavatorSound: name => sounds.push(name),
        playExcavatorVoice: (cue, voice) => voices.push({cue, voice}),
        playExcavatorEquipmentVoice() {},
        showExcavatorNotice: message => notices.push(message),
        renderExcavatorOfflineStatus: summary => statuses.push(summary),
        markLastDumpTarget() {},
        invalidateExcavatorWorkRefresh() {},
        newExcavatorFieldId: prefix => `${prefix}-${++idCount}`,
        excavatorInstallDeviceId: () => "device-A",
        legacyExcavatorFieldSequence: () => 0,
        cardByTruckId: () => card,
    };
    context.globalThis = context;
    vm.createContext(context);
    vm.runInContext(
        extractBraceBlock(template, "    function postTruckLoaded(card, dumpTarget)", "postTruckLoaded"),
        context
    );
    return {
        context, card, dumpTarget, notices, sounds, voices, statuses, queued, confirmed, classNames,
        restoreOutbox() {
            context.fieldOutbox = createOutbox(queued);
        },
    };
}


function flushPromises() {
    return new Promise(resolve => setImmediate(resolve));
}


test("a missing local outbox reports an error instead of silently dropping the load", async () => {
    const f = fixture({withOutbox: false});
    f.context.postTruckLoaded(f.card, f.dumpTarget);
    await flushPromises();

    assert.equal(f.notices.length, 1, "The worker must see a notice, not silence.");
    assert.match(f.notices[0], /Локальное сохранение недоступно/);
    assert.match(f.notices[0], /не сохранена/);
    assert.deepEqual(f.voices, [{cue: "action_error", voice: "voice_truck_send_failed"}]);
    assert.equal(f.statuses.length, 1, "The offline indicator must show a storage failure.");
    assert.equal(f.statuses[0].storage_failed, true);
    assert.deepEqual(f.queued, [], "Nothing may be treated as queued while the outbox is unavailable.");

    assert.equal(f.classNames.has("is-pending"), false);
    assert.equal(f.classNames.has("is-saved-on-device"), false);
    assert.equal(f.card.dataset.eoOfflineEventId, undefined);
    assert.equal(f.card.dataset.eoLocalOpenTripId, undefined);
    assert.equal(f.card.dataset.eoEquipmentState, undefined);
    assert.deepEqual(f.confirmed, [], "The load must not be confirmed on the card.");
});

test("a failed load leaves the action repeatable once the outbox is available again", async () => {
    const f = fixture({withOutbox: false});
    f.context.postTruckLoaded(f.card, f.dumpTarget);
    await flushPromises();
    assert.equal(f.queued.length, 0);

    f.restoreOutbox();
    f.context.postTruckLoaded(f.card, f.dumpTarget);
    await flushPromises();

    assert.equal(f.queued.length, 1, "The same truck and dump point must still be loadable.");
    assert.equal(f.queued[0].event_type, "excavator.trip.loaded");
    assert.equal(f.queued[0].payload.truck_id, "7");
    assert.equal(f.queued[0].payload.dump_point_id, "9");
});

test("a missing truck card or dump point explains what is missing", async () => {
    const missingCard = fixture();
    missingCard.context.postTruckLoaded(null, missingCard.dumpTarget);
    await flushPromises();
    assert.deepEqual(missingCard.sounds, ["action_error"]);
    assert.equal(missingCard.notices.length, 1);
    assert.match(missingCard.notices[0], /Самосвал не определён/);
    assert.deepEqual(missingCard.statuses, [], "Storage is fine, so no storage failure is reported.");
    assert.deepEqual(missingCard.queued, []);

    const missingTarget = fixture();
    missingTarget.context.postTruckLoaded(missingTarget.card, null);
    await flushPromises();
    assert.deepEqual(missingTarget.sounds, ["action_error"]);
    assert.equal(missingTarget.notices.length, 1);
    assert.match(missingTarget.notices[0], /Точка разгрузки не определена/);
    assert.deepEqual(missingTarget.statuses, []);
    assert.deepEqual(missingTarget.queued, []);
});

test("the silent combined early return is gone from the template", () => {
    assert.doesNotMatch(template, /if \(!card \|\| !dumpTarget \|\| !fieldOutbox\) return;/);
    assert.match(template, /if \(!fieldOutbox\) \{[\s\S]{0,400}showExcavatorNotice\(/);
});
