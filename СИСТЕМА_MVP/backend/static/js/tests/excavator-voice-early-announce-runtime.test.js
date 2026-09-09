const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const backendRoot = path.resolve(__dirname, "..", "..", "..");
const templatePath = path.join(backendRoot, "templates", "trips", "excavator_work.html");
const templateSource = fs.readFileSync(templatePath, "utf8");
const start = templateSource.indexOf("/* EXCAVATOR_VOICE_GUARD_START */");
const end = templateSource.indexOf("function readExcavatorViewportMetrics", start);

assert.notEqual(start, -1, "Excavator voice guard start marker was not found");
assert.notEqual(end, -1, "Excavator assignment snapshot end marker was not found");
const runtimeSource = templateSource.slice(start, end);

function assignmentEvent({
    version,
    assignmentId,
    truckId,
    truckNumber,
    action,
    targetExcavatorId,
    excavatorIds,
}) {
    return {
        type: "assignment_changed",
        object_type: "HaulAssignment",
        object_id: String(assignmentId),
        version,
        payload: {
            action,
            truck_ids: [truckId],
            truck_number: truckNumber,
            target_excavator_id: targetExcavatorId || null,
            excavator_ids: excavatorIds || [],
        },
    };
}

function createRuntime(options = {}) {
    const calls = [];
    const notices = [];
    const storage = new Map();
    const shell = {
        dataset: {
            eoCurrentExcavatorId: String(options.excavatorId || 9),
            eoShiftExcavatorId: "",
        },
        querySelectorAll() {
            return [];
        },
    };
    let bridge = options.bridge || ((details) => Promise.resolve({supported: true, announced: true}));
    const context = {
        console,
        Promise,
        Object,
        Array,
        Number,
        String,
        JSON,
        navigator: {vibrate() {}},
        document: {
            querySelector(selector) {
                return selector === "[data-eo-shell]" ? shell : null;
            },
        },
        showExcavatorNotice(message) {
            notices.push(message);
        },
        playExcavatorEquipmentVoice() {
            throw new Error("Legacy single-item bridge must not be used in the APK runtime");
        },
    };
    context.window = context;
    context.localStorage = {
        getItem(key) {
            return storage.has(key) ? storage.get(key) : null;
        },
        setItem(key, value) {
            storage.set(key, String(value));
        },
    };
    context.MobileOperationalSounds = {
        announceEquipmentBatch(details) {
            calls.push(details);
            return bridge(details);
        },
    };
    vm.runInNewContext(runtimeSource, context, {filename: "excavator_work.html#voice-guard"});
    return {
        context,
        calls,
        notices,
        shell,
        setBridge(nextBridge) {
            bridge = nextBridge;
        },
    };
}

function settlePromises() {
    return new Promise((resolve) => setImmediate(resolve));
}

test("the early hook announces an accepted truck before any DOM refresh", () => {
    const runtime = createRuntime();
    runtime.context.handleOperationalStateSignals({
        events: [assignmentEvent({
            version: 120,
            assignmentId: 501,
            truckId: 41,
            truckNumber: "41",
            action: "assignment_applied",
            targetExcavatorId: 9,
            excavatorIds: [9],
        })],
    });

    assert.equal(runtime.calls.length, 1);
    assert.equal(runtime.calls[0].items[0].eventVersion, 120);
    assert.equal(runtime.calls[0].items[0].operationKey, "excavator-assignment:assign:501:9");
    assert.equal(runtime.calls[0].items[0].equipmentNumber, "41");
});

test("the early hook announces a removed truck to the previous excavator", () => {
    const runtime = createRuntime();
    runtime.context.handleOperationalStateSignals({
        events: [assignmentEvent({
            version: 121,
            assignmentId: 502,
            truckId: 42,
            truckNumber: "42",
            action: "assignment_applied",
            targetExcavatorId: 10,
            excavatorIds: [9, 10],
        })],
    });

    assert.equal(runtime.calls.length, 1);
    assert.equal(runtime.calls[0].items[0].action, "excavator_truck_removed");
    assert.equal(runtime.calls[0].items[0].operationKey, "excavator-assignment:remove:502:9");
});

test("pending and unrelated assignment events stay silent", () => {
    const runtime = createRuntime();
    runtime.context.handleOperationalStateSignals({
        events: [
            assignmentEvent({
                version: 122,
                assignmentId: 503,
                truckId: 43,
                truckNumber: "43",
                action: "assignment_pending",
                targetExcavatorId: 9,
                excavatorIds: [9],
            }),
            assignmentEvent({
                version: 123,
                assignmentId: 504,
                truckId: 44,
                truckNumber: "44",
                action: "assignment_applied",
                targetExcavatorId: 12,
                excavatorIds: [12],
            }),
        ],
    });

    assert.equal(runtime.calls.length, 0);
});

test("several different trucks are delivered to one native batch", () => {
    const runtime = createRuntime();
    runtime.context.handleOperationalStateSignals({
        events: [
            assignmentEvent({
                version: 130,
                assignmentId: 510,
                truckId: 45,
                truckNumber: "45",
                action: "assignment_applied",
                targetExcavatorId: 9,
                excavatorIds: [9],
            }),
            assignmentEvent({
                version: 131,
                assignmentId: 511,
                truckId: 46,
                truckNumber: "46",
                action: "assignment_applied",
                targetExcavatorId: 9,
                excavatorIds: [9],
            }),
        ],
    });

    assert.equal(runtime.calls.length, 1);
    assert.deepEqual(
        Array.from(runtime.calls[0].items, (item) => item.equipmentNumber),
        ["45", "46"]
    );
});

test("only the latest operation for one truck is announced", () => {
    const runtime = createRuntime();
    runtime.context.handleOperationalStateSignals({
        events: [
            assignmentEvent({
                version: 140,
                assignmentId: 520,
                truckId: 47,
                truckNumber: "47",
                action: "assignment_applied",
                targetExcavatorId: 9,
                excavatorIds: [9],
            }),
            assignmentEvent({
                version: 141,
                assignmentId: 521,
                truckId: 47,
                truckNumber: "47",
                action: "release_applied",
                excavatorIds: [9],
            }),
        ],
    });

    assert.equal(runtime.calls.length, 1);
    assert.equal(runtime.calls[0].items.length, 1);
    assert.equal(runtime.calls[0].items[0].action, "excavator_truck_removed");
    assert.equal(runtime.calls[0].items[0].eventVersion, 141);
});

test("the DOM fallback stays silent after the early hook claimed the assignment", () => {
    const runtime = createRuntime({bridge: () => new Promise(() => {})});
    runtime.context.handleOperationalStateSignals({
        events: [assignmentEvent({
            version: 150,
            assignmentId: 530,
            truckId: 48,
            truckNumber: "48",
            action: "assignment_applied",
            targetExcavatorId: 9,
            excavatorIds: [9],
        })],
    });
    runtime.context.announceExcavatorAssignmentChanges({}, {
        "48": {assignmentId: "530", truckNumber: "48"},
    }, 155);

    assert.equal(runtime.calls.length, 1);
    assert.equal(runtime.context.ExcavatorVoiceGuard.state("excavator-assignment:assign:530:9"), "claimed");
});

test("already_announced finalizes the JS claim and blocks the DOM fallback", async () => {
    const runtime = createRuntime({
        bridge: () => Promise.resolve({supported: true, announced: false, reason: "already_announced"}),
    });
    const event = assignmentEvent({
        version: 160,
        assignmentId: 540,
        truckId: 49,
        truckNumber: "49",
        action: "assignment_applied",
        targetExcavatorId: 9,
        excavatorIds: [9],
    });
    runtime.context.handleOperationalStateSignals({events: [event]});
    await settlePromises();
    runtime.context.announceExcavatorAssignmentChanges({}, {
        "49": {assignmentId: "540", truckNumber: "49"},
    }, 165);

    assert.equal(runtime.calls.length, 1);
    assert.equal(runtime.context.ExcavatorVoiceGuard.state("excavator-assignment:assign:540:9"), "announced");
});

test("a rejected bridge releases the operation for the DOM fallback", async () => {
    const runtime = createRuntime({bridge: () => Promise.reject(new Error("bridge rejected"))});
    runtime.context.handleOperationalStateSignals({
        events: [assignmentEvent({
            version: 170,
            assignmentId: 550,
            truckId: 50,
            truckNumber: "50",
            action: "assignment_applied",
            targetExcavatorId: 9,
            excavatorIds: [9],
        })],
    });
    await settlePromises();
    runtime.setBridge(() => Promise.resolve({supported: true, announced: true}));
    runtime.context.announceExcavatorAssignmentChanges({}, {
        "50": {assignmentId: "550", truckNumber: "50"},
    }, 175);

    assert.equal(runtime.calls.length, 2);
});

test("a synchronous bridge throw releases the operation for the next path", () => {
    const runtime = createRuntime({bridge: () => { throw new Error("sync bridge failure"); }});
    const operation = {
        action: "excavator_truck_assigned",
        truckNumber: "51",
        fallbackVoice: "voice_truck_assigned",
        eventVersion: 180,
        operationKey: "excavator-assignment:assign:560:9",
    };
    runtime.context.playExcavatorAssignmentAlerts([operation]);

    assert.equal(runtime.context.ExcavatorVoiceGuard.state(operation.operationKey), "");
});

test("a removed DOM card shows a notice but never invents a second voice key", () => {
    const runtime = createRuntime();
    runtime.context.announceExcavatorAssignmentChanges({
        "52": {assignmentId: "570", truckNumber: "52"},
    }, {}, 190);

    assert.equal(runtime.calls.length, 0);
    assert.deepEqual(runtime.notices, ["Снят самосвал 52."]);
});

test("legacy string snapshots remain readable after the schema upgrade", () => {
    const runtime = createRuntime();
    const normalized = runtime.context.normalizeExcavatorAssignmentSnapshotItem("41", "41");
    assert.equal(normalized.assignmentId, "");
    assert.equal(normalized.truckNumber, "41");
});

test("the rendered snapshot exposes the exact HaulAssignment id", () => {
    assert.match(
        templateSource,
        /data-eo-assignment-snapshot[\s\S]*?data-assignment-id="\{\{ card\.assignment\.id \}\}"/
    );
});
