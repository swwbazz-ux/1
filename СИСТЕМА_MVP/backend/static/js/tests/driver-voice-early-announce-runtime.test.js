"use strict";

/* Ранняя озвучка рабочего события.

   Голос водителя не должен ждать ни занятого интерфейса, ни загрузки
   фрагмента экрана. Здесь проверяется, что сигнал уходит сразу после ответа
   сервера, что он молчит там, где данных недостаточно, и что три источника
   одного события озвучивают его ровно один раз. */

const assert = require("node:assert/strict");
const {driverScreenSource} = require("./driver-screen-source");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const BACKEND_ROOT = path.resolve(__dirname, "..", "..", "..");
const REALTIME_CLIENT_PATH = path.join(BACKEND_ROOT, "static", "js", "realtime-client.js");
const REALTIME_CLIENT_SOURCE = fs.readFileSync(REALTIME_CLIENT_PATH, "utf8");
const DRIVER_TEMPLATE_PATH = path.join(BACKEND_ROOT, "templates", "users", "driver_shift.html");
const DRIVER_TEMPLATE_SOURCE = driverScreenSource();
const MOBILE_QUEUE_KEY = "driver-voice-early-announce-queue";

/* ------------------------------------------------------------------ */
/* Общая часть: разбор шаблона Водителя                                */
/* ------------------------------------------------------------------ */

function extractMarkedBlock(source, startMarker, endMarker) {
    const start = source.indexOf(startMarker);
    assert.notEqual(start, -1, `${startMarker} was not found in the Driver template.`);
    const end = source.indexOf(endMarker, start);
    assert.notEqual(end, -1, `${endMarker} was not found in the Driver template.`);
    return source.slice(start + startMarker.length, end);
}

function extractBraceBlock(source, signature, label) {
    const start = source.indexOf(signature);
    assert.notEqual(start, -1, `${label} signature was not found.`);
    const open = source.indexOf("{", start + signature.length);
    assert.notEqual(open, -1, `${label} opening brace was not found.`);

    let depth = 0;
    let quote = "";
    let escaped = false;
    let lineComment = false;
    let blockComment = false;

    for (let index = open; index < source.length; index += 1) {
        const character = source[index];
        const next = source[index + 1] || "";

        if (lineComment) {
            if (character === "\n") lineComment = false;
            continue;
        }
        if (blockComment) {
            if (character === "*" && next === "/") {
                blockComment = false;
                index += 1;
            }
            continue;
        }
        if (quote) {
            if (escaped) {
                escaped = false;
            } else if (character === "\\") {
                escaped = true;
            } else if (character === quote) {
                quote = "";
            }
            continue;
        }
        if (character === "/" && next === "/") {
            lineComment = true;
            index += 1;
            continue;
        }
        if (character === "/" && next === "*") {
            blockComment = true;
            index += 1;
            continue;
        }
        if (character === '"' || character === "'" || character === "`") {
            quote = character;
            continue;
        }
        if (character === "{") depth += 1;
        if (character === "}") {
            depth -= 1;
            if (depth === 0) return source.slice(start, index + 1);
        }
    }
    throw new Error(`${label} block was not closed.`);
}

const GUARD_BLOCK = extractMarkedBlock(
    DRIVER_TEMPLATE_SOURCE,
    "/* DRIVER_VOICE_GUARD_START */",
    "/* DRIVER_VOICE_GUARD_END */"
);
const DUMP_POINT_SELECTOR_SOURCE = extractBraceBlock(
    DRIVER_TEMPLATE_SOURCE,
    "function latestDriverDumpPointEvent(context)",
    "latestDriverDumpPointEvent"
);
const ASSIGNMENT_ALERT_SOURCE = extractBraceBlock(
    DRIVER_TEMPLATE_SOURCE,
    "function playDriverAssignmentAlert(eventVersion, assignmentKind, excavatorNumber, options)",
    "playDriverAssignmentAlert"
);

/* Резервный путь по DOM вызывает те же функции, но с другими аргументами:
   версией состояния вместо версии события и ключом операции из разметки.
   Тест повторяет именно этот вызов, а не второй запуск раннего сигнала. */
function domFallbackAnnounceAssignment(runtime, targetVersion, assignmentDataset) {
    const assignmentId = String(assignmentDataset.driverAssignmentId || "").trim();
    if (String(assignmentDataset.driverAssignmentKind || "") === "assign") {
        runtime.window.playDriverAssignmentAlert(
            targetVersion,
            "assign",
            String(assignmentDataset.driverExcavatorNumber || ""),
            {opKey: assignmentId ? "assign:" + assignmentId : ""}
        );
        return;
    }
    runtime.window.playDriverReleaseOfferCue(assignmentId);
}

function domFallbackAnnounceDumpPoint(runtime, targetVersion, shellDataset) {
    runtime.window.playDriverDumpPointAlert({events: [{
        version: targetVersion,
        type: "trip_changed",
        payload: {
            action: "truck_loaded",
            trip_id: Number(shellDataset.driverActiveTripId || 0),
            assigned_dump_point_id: Number(shellDataset.driverAssignedDumpPointId || 0),
            dump_point_name: String(shellDataset.driverAssignedDumpPointName || ""),
        },
    }]});
}
const DUMP_POINT_ALERT_SOURCE = extractBraceBlock(
    DRIVER_TEMPLATE_SOURCE,
    "function playDriverDumpPointAlert(context)",
    "playDriverDumpPointAlert"
);
const RELEASE_OFFER_CUE_SOURCE = extractBraceBlock(
    DRIVER_TEMPLATE_SOURCE,
    "function playDriverReleaseOfferCue(assignmentId)",
    "playDriverReleaseOfferCue"
);

function createDriverVoiceRuntime(options) {
    const runtimeOptions = options || {};
    const bridgeCalls = [];
    const soundCalls = [];
    const vibrations = [];
    const timeouts = [];

    const announceResult = runtimeOptions.announceResult || {supported: true, announced: true};
    const soundResult = runtimeOptions.soundResult !== false;

    const sandbox = {
        console,
        Promise,
        Object,
        Array,
        Number,
        String,
        Boolean,
        JSON,
        Error,
        Math,
        window: {},
        document: {
            body: {dataset: {nativeApp: runtimeOptions.nativeApp === false ? "false" : "true"}},
        },
        navigator: {
            vibrate(pattern) {
                vibrations.push(pattern);
                return true;
            },
        },
    };
    sandbox.window.window = sandbox.window;
    sandbox.window.document = sandbox.document;
    sandbox.window.setTimeout = function (callback, delay) {
        timeouts.push({callback, delay});
        return timeouts.length;
    };
    sandbox.window.clearTimeout = function (timerId) {
        if (timerId > 0 && timerId <= timeouts.length) timeouts[timerId - 1] = null;
    };
    sandbox.window.MobileOperationalSounds = {
        announceDumpPoint(details) {
            bridgeCalls.push({method: "announceDumpPoint", details});
            return Promise.resolve(
                typeof announceResult === "function"
                    ? announceResult("announceDumpPoint", details)
                    : announceResult
            );
        },
        announceEquipment(details) {
            bridgeCalls.push({method: "announceEquipment", details});
            return Promise.resolve(
                typeof announceResult === "function"
                    ? announceResult("announceEquipment", details)
                    : announceResult
            );
        },
        announceOperational(details) {
            bridgeCalls.push({method: "announceOperational", details});
            return Promise.resolve(
                typeof announceResult === "function"
                    ? announceResult("announceOperational", details)
                    : announceResult
            );
        },
        play(name) {
            soundCalls.push(name);
            return Promise.resolve(soundResult);
        },
    };

    const context = vm.createContext(sandbox);
    /* Диагностика и мостовые обёртки заменяются заглушками: проверяется
       поведение guard'а и раннего сигнала, а не отчётность. */
    vm.runInContext(
        [
            "function reportDriverAudioDiagnostic() {}",
            "function playDriverSound(name) {",
            "    return window.MobileOperationalSounds.play(name);",
            "}",
            "function playDriverVoice(cue, voice, options) {",
            "    return window.MobileOperationalSounds.announceOperational({",
            "        cue: cue, voice: voice,",
            "        eventVersion: Number((options || {}).eventVersion || 0),",
            "        eventKey: String((options || {}).eventKey || '')",
            "    });",
            "}",
            GUARD_BLOCK,
            DUMP_POINT_SELECTOR_SOURCE,
            ASSIGNMENT_ALERT_SOURCE,
            RELEASE_OFFER_CUE_SOURCE,
            DUMP_POINT_ALERT_SOURCE,
            /* Резервный путь по DOM вызывает эти же функции напрямую. */
            "window.playDriverAssignmentAlert = playDriverAssignmentAlert;",
            "window.playDriverDumpPointAlert = playDriverDumpPointAlert;",
            "window.playDriverReleaseOfferCue = playDriverReleaseOfferCue;",
        ].join("\n"),
        context,
        {filename: "driver_shift.voice.js"}
    );

    return {
        window: sandbox.window,
        bridgeCalls,
        soundCalls,
        vibrations,
        timeouts,
        signal(signalContext) {
            return sandbox.window.handleOperationalStateSignals(signalContext);
        },
    };
}

function assignmentEvent(version, assignmentId, action, excavatorNumber) {
    return {
        version,
        type: "assignment_changed",
        object_type: "HaulAssignment",
        object_id: String(assignmentId),
        payload: {
            action,
            target_excavator_number: String(excavatorNumber || ""),
        },
    };
}

function dumpPointEvent(version, tripId, dumpPointId, dumpPointName) {
    return {
        version,
        type: "trip_changed",
        payload: {
            action: "truck_loaded",
            trip_id: tripId,
            assigned_dump_point_id: dumpPointId,
            dump_point_name: dumpPointName,
        },
    };
}

async function settlePromises() {
    for (let round = 0; round < 12; round += 1) {
        await Promise.resolve();
    }
}

/* ------------------------------------------------------------------ */
/* Guard и разбор события                                              */
/* ------------------------------------------------------------------ */

test("synchronous double entry claims the operation once", async () => {
    const runtime = createDriverVoiceRuntime();
    const events = [assignmentEvent(120, 77, "assignment_pending", "12")];

    runtime.signal({events});
    runtime.signal({events});
    await settlePromises();

    assert.equal(
        runtime.bridgeCalls.length,
        1,
        "two synchronous signals for one assignment must reach the bridge once"
    );
    assert.equal(runtime.vibrations.length, 1, "vibration must not repeat either");
});

test("early hook sends the exact event version and the assignment identifier", async () => {
    const runtime = createDriverVoiceRuntime();
    runtime.signal({
        events: [assignmentEvent(1301, 88, "assignment_pending", "07")],
        stateVersion: 1499,
    });
    await settlePromises();

    const call = runtime.bridgeCalls[0];
    assert.equal(call.method, "announceEquipment");
    assert.equal(
        call.details.eventVersion,
        1301,
        "the event version must be sent, not the global state version"
    );
    assert.equal(call.details.eventKey, "driver_assignment");
    assert.equal(call.details.equipmentNumber, "07");
});

test("the newest assignment inside one delta wins", async () => {
    const runtime = createDriverVoiceRuntime();
    runtime.signal({
        events: [
            assignmentEvent(400, 11, "assignment_pending", "03"),
            assignmentEvent(407, 12, "assignment_pending", "09"),
        ],
    });
    await settlePromises();

    assert.equal(runtime.bridgeCalls.length, 1);
    assert.equal(
        runtime.bridgeCalls[0].details.equipmentNumber,
        "09",
        "a reissued assignment must be announced by its latest version, not its first"
    );
});

test("release_applied is announced and assignment_applied is not", async () => {
    const released = createDriverVoiceRuntime();
    released.signal({events: [assignmentEvent(510, 21, "release_applied", "")]});
    await settlePromises();
    assert.equal(released.bridgeCalls[0].method, "announceOperational");
    assert.equal(released.bridgeCalls[0].details.voice, "voice_assignment_removed");

    const applied = createDriverVoiceRuntime();
    applied.signal({events: [assignmentEvent(511, 22, "assignment_applied", "05")]});
    await settlePromises();
    assert.equal(
        applied.bridgeCalls.length,
        0,
        "assignment_applied is not part of the Driver voice contract"
    );
});

test("an assignment event without an identifier is left to the DOM fallback", async () => {
    const runtime = createDriverVoiceRuntime();
    const event = assignmentEvent(600, 33, "assignment_pending", "04");
    event.object_id = "";
    runtime.signal({events: [event]});
    await settlePromises();

    assert.equal(
        runtime.bridgeCalls.length,
        0,
        "without an operation key the early hook cannot deduplicate and must stay silent"
    );
});

/* ------------------------------------------------------------------ */
/* Закрытие и освобождение заявки                                      */
/* ------------------------------------------------------------------ */

test("already_announced finalizes the claim and blocks the DOM fallback", async () => {
    const runtime = createDriverVoiceRuntime({
        announceResult: {supported: true, announced: false, reason: "already_announced"},
    });
    runtime.signal({events: [assignmentEvent(700, 44, "assignment_pending", "02")]});
    await settlePromises();

    assert.equal(runtime.bridgeCalls.length, 1);
    assert.equal(
        runtime.window.DriverVoiceGuard.state("assign:44"),
        "announced",
        "an event owned by the heartbeat must stay closed for the other paths"
    );

    domFallbackAnnounceAssignment(runtime, 760, {
        driverAssignmentId: "44",
        driverAssignmentKind: "assign",
        driverExcavatorNumber: "02",
    });
    await settlePromises();
    assert.equal(runtime.bridgeCalls.length, 1, "the DOM fallback must not repeat it");
});

test("a web fallback that actually played finalizes the claim", async () => {
    const runtime = createDriverVoiceRuntime({
        announceResult: {supported: false, announced: true},
    });
    runtime.signal({events: [assignmentEvent(800, 55, "assignment_pending", "06")]});
    await settlePromises();

    assert.equal(
        runtime.window.DriverVoiceGuard.state("assign:55"),
        "announced",
        "supported:false with announced:true means a sound was produced"
    );
});

test("a completely silent failure releases the claim", async () => {
    const runtime = createDriverVoiceRuntime({
        announceResult: {supported: true, announced: false, reason: "resource_unavailable"},
        soundResult: false,
    });
    runtime.signal({events: [assignmentEvent(900, 66, "assignment_pending", "08")]});
    await settlePromises();

    assert.equal(
        runtime.window.DriverVoiceGuard.state("assign:66"),
        "",
        "if nothing was heard the operation must stay available to the next path"
    );
});

test("the dump-point cue fallback finalizes the claim", async () => {
    const runtime = createDriverVoiceRuntime({
        announceResult: {supported: true, announced: false, reason: "invalid_event"},
        soundResult: true,
    });
    runtime.signal({events: [dumpPointEvent(1000, 4321, 9, "Отвал 3")]});
    await settlePromises();

    assert.deepEqual(runtime.soundCalls, ["truck_assigned"]);
    assert.equal(
        runtime.window.DriverVoiceGuard.state("dump:4321:id-9"),
        "announced",
        "the cue already sounded, so the DOM fallback must not repeat it"
    );
});

/* ------------------------------------------------------------------ */
/* Настоящая цепочка: ранний сигнал, затем резервный путь по DOM        */
/* ------------------------------------------------------------------ */

test("the DOM fallback stays silent after the early hook announced the assignment", async () => {
    const runtime = createDriverVoiceRuntime();
    runtime.signal({
        events: [assignmentEvent(2100, 909, "assignment_pending", "21")],
        stateVersion: 2140,
    });
    await settlePromises();
    assert.equal(runtime.bridgeCalls.length, 1);
    assert.equal(runtime.bridgeCalls[0].details.eventVersion, 2100);

    /* Экран обновился, разметка сменилась — резервный путь пробует то же
       назначение уже с версией состояния. */
    domFallbackAnnounceAssignment(runtime, 2140, {
        driverAssignmentId: "909",
        driverAssignmentKind: "assign",
        driverExcavatorNumber: "21",
    });
    await settlePromises();

    assert.equal(
        runtime.bridgeCalls.length,
        1,
        "the DOM fallback must recognise the operation the early hook already announced"
    );
});

test("the DOM fallback announces an assignment the early hook could not see", async () => {
    const runtime = createDriverVoiceRuntime();
    /* Усечённая дельта: ранний обработчик до экрана не доходит вовсе. */
    domFallbackAnnounceAssignment(runtime, 3050, {
        driverAssignmentId: "910",
        driverAssignmentKind: "assign",
        driverExcavatorNumber: "31",
    });
    await settlePromises();

    assert.equal(runtime.bridgeCalls.length, 1, "the safety net must still announce");
    assert.equal(
        runtime.bridgeCalls[0].details.eventVersion,
        3050,
        "the fallback keeps the positive state version"
    );
    assert.ok(
        runtime.bridgeCalls[0].details.eventVersion > 0,
        "a non-positive version would be rejected natively"
    );
});

test("the DOM fallback stays silent after the early hook announced the dump point", async () => {
    const runtime = createDriverVoiceRuntime();
    runtime.signal({events: [dumpPointEvent(4100, 5150, 3, "Отвал 1")]});
    await settlePromises();
    assert.equal(runtime.bridgeCalls.length, 1);

    domFallbackAnnounceDumpPoint(runtime, 4180, {
        driverActiveTripId: "5150",
        driverAssignedDumpPointId: "3",
        driverAssignedDumpPointName: "Отвал 1",
    });
    await settlePromises();

    assert.equal(
        runtime.bridgeCalls.length,
        1,
        "one trip must reach the player once, whichever path saw it first"
    );
});

/* ------------------------------------------------------------------ */
/* Предложение снять назначение: пик сейчас, голос по факту снятия      */
/* ------------------------------------------------------------------ */

test("release_pending plays one cue and no voice", async () => {
    const runtime = createDriverVoiceRuntime();
    domFallbackAnnounceAssignment(runtime, 5200, {
        driverAssignmentId: "911",
        driverAssignmentKind: "release",
        driverExcavatorNumber: "",
    });
    await settlePromises();

    assert.deepEqual(
        runtime.soundCalls,
        ["assignment_removed_notice"],
        "the offer must draw attention with a single cue"
    );
    assert.equal(
        runtime.bridgeCalls.length,
        0,
        "the phrase belongs to release_applied; announcing it here would write the "
        + "native marker at a lower version and the heartbeat would repeat it later"
    );
    assert.equal(runtime.vibrations.length, 1);
});

test("a repeated DOM refresh does not repeat the release cue", async () => {
    const runtime = createDriverVoiceRuntime();
    const dataset = {
        driverAssignmentId: "912",
        driverAssignmentKind: "release",
        driverExcavatorNumber: "",
    };

    domFallbackAnnounceAssignment(runtime, 5300, dataset);
    await settlePromises();
    domFallbackAnnounceAssignment(runtime, 5340, dataset);
    domFallbackAnnounceAssignment(runtime, 5390, dataset);
    await settlePromises();

    assert.deepEqual(
        runtime.soundCalls,
        ["assignment_removed_notice"],
        "the cue is claimed once per release offer, however often the screen refreshes"
    );
    assert.equal(runtime.vibrations.length, 1);
});

test("release_applied still produces one full announcement after the pending cue", async () => {
    const runtime = createDriverVoiceRuntime();

    /* Предложение снять: только пик. */
    domFallbackAnnounceAssignment(runtime, 5400, {
        driverAssignmentId: "913",
        driverAssignmentKind: "release",
        driverExcavatorNumber: "",
    });
    await settlePromises();
    assert.deepEqual(runtime.soundCalls, ["assignment_removed_notice"]);
    assert.equal(runtime.bridgeCalls.length, 0);

    /* Водитель подтвердил: приходит release_applied с тем же номером записи. */
    runtime.signal({events: [assignmentEvent(5460, 913, "release_applied", "")]});
    await settlePromises();

    assert.equal(runtime.bridgeCalls.length, 1, "the removal must be announced exactly once");
    assert.equal(runtime.bridgeCalls[0].method, "announceOperational");
    assert.equal(runtime.bridgeCalls[0].details.voice, "voice_assignment_removed");
    assert.equal(
        runtime.bridgeCalls[0].details.eventVersion,
        5460,
        "the exact event version keeps the heartbeat deduplicated against this call"
    );

    /* Повторный DOM-refresh после применения ничего не добавляет. */
    domFallbackAnnounceAssignment(runtime, 5480, {
        driverAssignmentId: "913",
        driverAssignmentKind: "release",
        driverExcavatorNumber: "",
    });
    await settlePromises();
    assert.deepEqual(runtime.soundCalls, ["assignment_removed_notice"]);
    assert.equal(runtime.bridgeCalls.length, 1);
});

test("the release cue and the release phrase use separate claim keys", async () => {
    const runtime = createDriverVoiceRuntime();
    domFallbackAnnounceAssignment(runtime, 5500, {
        driverAssignmentId: "914",
        driverAssignmentKind: "release",
        driverExcavatorNumber: "",
    });
    await settlePromises();

    assert.equal(runtime.window.DriverVoiceGuard.state("release-cue:914"), "announced");
    assert.equal(
        runtime.window.DriverVoiceGuard.state("assign:914"),
        "",
        "the cue must not close the key that belongs to the removal phrase itself"
    );

    runtime.signal({events: [assignmentEvent(5560, 914, "release_applied", "")]});
    await settlePromises();
    assert.equal(runtime.window.DriverVoiceGuard.state("assign:914"), "announced");
    assert.equal(runtime.bridgeCalls.length, 1);
});

test("the release cue never reaches the native announcer", () => {
    assert.match(
        RELEASE_OFFER_CUE_SOURCE,
        /playDriverSound\("assignment_removed_notice"\)/,
        "the offer uses the plain signal player"
    );
    assert.doesNotMatch(
        RELEASE_OFFER_CUE_SOURCE,
        /announceEquipment|announceOperational|announceDumpPoint|playDriverVoice/,
        "no announcer call means no native dedupe marker is written for the offer"
    );
});

/* ------------------------------------------------------------------ */
/* Заявка не должна оставаться запертой при сбое моста                 */
/* ------------------------------------------------------------------ */

test("a rejected bridge promise releases the assignment claim", async () => {
    const runtime = createDriverVoiceRuntime();
    runtime.window.MobileOperationalSounds.announceEquipment = function () {
        return Promise.reject(new Error("bridge is gone"));
    };
    runtime.signal({events: [assignmentEvent(6100, 920, "assignment_pending", "41")]});
    await settlePromises();

    assert.equal(
        runtime.window.DriverVoiceGuard.state("assign:920"),
        "",
        "a rejected promise must not lock the operation until the page reloads"
    );
});

test("a synchronously thrown bridge call releases the assignment claim", async () => {
    const runtime = createDriverVoiceRuntime();
    runtime.window.MobileOperationalSounds.announceEquipment = function () {
        throw new Error("bridge threw");
    };
    runtime.signal({events: [assignmentEvent(6200, 921, "assignment_pending", "42")]});
    await settlePromises();

    assert.equal(runtime.window.DriverVoiceGuard.state("assign:921"), "");
});

test("a released claim lets the next path announce the same assignment", async () => {
    const runtime = createDriverVoiceRuntime();
    const original = runtime.window.MobileOperationalSounds.announceEquipment;
    runtime.window.MobileOperationalSounds.announceEquipment = function () {
        return Promise.reject(new Error("bridge is gone"));
    };
    runtime.signal({events: [assignmentEvent(6300, 922, "assignment_pending", "43")]});
    await settlePromises();

    runtime.window.MobileOperationalSounds.announceEquipment = original;
    domFallbackAnnounceAssignment(runtime, 6350, {
        driverAssignmentId: "922",
        driverAssignmentKind: "assign",
        driverExcavatorNumber: "43",
    });
    await settlePromises();

    assert.equal(
        runtime.window.DriverVoiceGuard.state("assign:922"),
        "announced",
        "after a failure the operation must remain available to the safety net"
    );
});

test("a rejected dump-point bridge promise releases the point claim", async () => {
    const runtime = createDriverVoiceRuntime();
    runtime.window.MobileOperationalSounds.announceDumpPoint = function () {
        return Promise.reject(new Error("bridge is gone"));
    };
    runtime.signal({events: [dumpPointEvent(6400, 5160, 4, "Отвал 2")]});
    await settlePromises();

    assert.equal(runtime.window.DriverVoiceGuard.state("dump:5160:id-4"), "");
});

test("a synchronously thrown dump-point bridge call releases the point claim", async () => {
    const runtime = createDriverVoiceRuntime();
    runtime.window.MobileOperationalSounds.announceDumpPoint = function () {
        throw new Error("bridge threw");
    };
    runtime.signal({events: [dumpPointEvent(6500, 5170, 5, "Отвал 4")]});
    await settlePromises();

    assert.equal(runtime.window.DriverVoiceGuard.state("dump:5170:id-5"), "");
});

test("the dump-point signal carries the exact event version and trip", async () => {
    const runtime = createDriverVoiceRuntime();
    runtime.signal({
        events: [dumpPointEvent(1100, 777, 5, "Склад руды")],
        stateVersion: 1180,
    });
    await settlePromises();

    const call = runtime.bridgeCalls[0];
    assert.equal(call.method, "announceDumpPoint");
    assert.equal(call.details.eventVersion, 1100);
    assert.equal(call.details.tripId, 777);
    assert.ok(
        call.details.eventVersion > 0,
        "DriverDumpPointAnnouncer rejects a non-positive version as invalid_event"
    );
});

/* ------------------------------------------------------------------ */
/* Ключ точки разгрузки: нативный формат и границы guard'а              */
/* ------------------------------------------------------------------ */

test("the dump-point claim key keeps the native trip-and-point form", async () => {
    const runtime = createDriverVoiceRuntime();
    runtime.signal({events: [dumpPointEvent(1200, 4321, 9, "Отвал 3")]});
    await settlePromises();

    assert.equal(runtime.bridgeCalls.length, 1);
    assert.equal(
        runtime.window.DriverVoiceGuard.state("dump:4321:id-9"),
        "announced",
        "core/voice_state.py and ConnectivityForegroundService use dump:<trip>:id-<point>"
    );
    assert.equal(
        runtime.window.DriverVoiceGuard.state("dump:4321"),
        "",
        "a whole-trip key would swallow every later point of the same trip"
    );
});

test("a repeated delivery of one dump point is announced exactly once", async () => {
    const runtime = createDriverVoiceRuntime();
    const event = dumpPointEvent(1400, 6100, 21, "Отвал C");

    runtime.signal({events: [event]});
    await settlePromises();
    /* Тот же кадр приходит вторым путём. */
    runtime.signal({events: [event]});
    await settlePromises();
    /* Тот же кадр повторно доставлен уже с более высокой версией состояния. */
    runtime.signal({events: [dumpPointEvent(1409, 6100, 21, "Отвал C")]});
    await settlePromises();

    assert.equal(
        runtime.bridgeCalls.length,
        1,
        "one operation must reach the player once, however often it is delivered"
    );
    assert.equal(runtime.window.DriverVoiceGuard.state("dump:6100:id-21"), "announced");
});

test("another dump point inside the same trip is announced again", async () => {
    const runtime = createDriverVoiceRuntime();

    runtime.signal({events: [dumpPointEvent(1300, 6001, 11, "Отвал A")]});
    await settlePromises();
    runtime.signal({events: [dumpPointEvent(1307, 6001, 12, "Отвал B")]});
    await settlePromises();

    assert.equal(
        runtime.bridgeCalls.length,
        2,
        "a changed point of the same trip is a new working event, not a repeat"
    );
    assert.deepEqual(
        runtime.bridgeCalls.map((call) => call.details.dumpPointId),
        [11, 12]
    );
    assert.equal(runtime.window.DriverVoiceGuard.state("dump:6001:id-11"), "announced");
    assert.equal(runtime.window.DriverVoiceGuard.state("dump:6001:id-12"), "announced");
});

test("a dump point without an identifier is announced and keyed by its name", async () => {
    const runtime = createDriverVoiceRuntime();

    runtime.signal({events: [dumpPointEvent(1500, 7001, 0, "Отвал 5")]});
    await settlePromises();
    assert.equal(runtime.bridgeCalls.length, 1, "a name-only point must still be announced");
    assert.equal(
        runtime.window.DriverVoiceGuard.state("dump:7001:name-отвал_5"),
        "announced",
        "without an identifier the key repeats the native name form"
    );
    assert.equal(
        runtime.window.DriverVoiceGuard.state("dump:7001:id-0"),
        "",
        "an id-0 key is what the native truth rejects as invalidKnownOperation"
    );
    assert.equal(
        runtime.window.DriverVoiceGuard.state("dump:7001"),
        "",
        "one trip must not be closed by its first point"
    );

    runtime.signal({events: [dumpPointEvent(1504, 7001, 0, "Отвал 5")]});
    await settlePromises();
    assert.equal(runtime.bridgeCalls.length, 1, "the same name-only point does not repeat");

    runtime.signal({events: [dumpPointEvent(1508, 7001, 0, "Отвал 6")]});
    await settlePromises();
    assert.equal(
        runtime.bridgeCalls.length,
        2,
        "another point name inside the same trip is another event"
    );
});

test("an assignment and a dump point inside one delta are both announced", async () => {
    const runtime = createDriverVoiceRuntime();
    runtime.signal({
        events: [
            assignmentEvent(1600, 71, "assignment_pending", "05"),
            dumpPointEvent(1601, 8001, 31, "Отвал D"),
        ],
    });
    await settlePromises();

    assert.deepEqual(
        runtime.bridgeCalls.map((call) => call.method),
        ["announceDumpPoint", "announceEquipment"],
        "each event owns a separate operation key, so neither suppresses the other"
    );
});

test("a failed dump-point announcement leaves the same point open for the retry", async () => {
    const runtime = createDriverVoiceRuntime();
    const original = runtime.window.MobileOperationalSounds.announceDumpPoint;
    runtime.window.MobileOperationalSounds.announceDumpPoint = function () {
        return Promise.reject(new Error("bridge is gone"));
    };

    runtime.signal({events: [dumpPointEvent(1700, 8100, 41, "Отвал E")]});
    await settlePromises();
    assert.equal(
        runtime.window.DriverVoiceGuard.state("dump:8100:id-41"),
        "",
        "a failed bridge call must not lock the point until the page reloads"
    );

    runtime.window.MobileOperationalSounds.announceDumpPoint = original;
    runtime.signal({events: [dumpPointEvent(1701, 8100, 41, "Отвал E")]});
    await settlePromises();

    assert.equal(
        runtime.bridgeCalls.length,
        1,
        "the retry after the failure must reach the player"
    );
    assert.equal(runtime.window.DriverVoiceGuard.state("dump:8100:id-41"), "announced");
});

/* ------------------------------------------------------------------ */
/* Срок ожидания голоса: зависший звук не держит экран                  */
/* ------------------------------------------------------------------ */

const VOICE_DEADLINE_MS = 2500;

function fireVoiceDeadline(runtime) {
    const pending = runtime.timeouts
        .filter(Boolean)
        .filter((timer) => timer.delay === VOICE_DEADLINE_MS);
    assert.ok(pending.length >= 1, "the voice handoff must be bounded by a deadline");
    const timer = pending[pending.length - 1];
    runtime.timeouts[runtime.timeouts.indexOf(timer)] = null;
    timer.callback();
}

test("a hung dump-point bridge releases the claim and does not hold the cursor", async () => {
    const runtime = createDriverVoiceRuntime({announceResult: () => new Promise(() => {})});
    const outcome = runtime.signal({events: [dumpPointEvent(1800, 9001, 51, "Отвал F")]});
    await settlePromises();

    assert.equal(runtime.bridgeCalls.length, 1);
    assert.equal(
        runtime.window.DriverVoiceGuard.state("dump:9001:id-51"),
        "claimed",
        "until the deadline the operation belongs to the voice path"
    );

    fireVoiceDeadline(runtime);
    const result = await outcome;

    assert.equal(result.accepted, true, "unresponsive audio must not stall the shared cursor");
    assert.equal(
        runtime.window.DriverVoiceGuard.state("dump:9001:id-51"),
        "",
        "the claim is released, so the next delivery may try again"
    );
});

test("the same event is tried again after the voice deadline", async () => {
    let attempts = 0;
    const runtime = createDriverVoiceRuntime({
        announceResult: () => {
            attempts += 1;
            return attempts === 1 ? new Promise(() => {}) : {supported: true, announced: true};
        },
    });
    const first = runtime.signal({events: [dumpPointEvent(1810, 9002, 52, "Отвал F")]});
    await settlePromises();
    fireVoiceDeadline(runtime);
    assert.equal((await first).accepted, true);

    const second = runtime.signal({events: [dumpPointEvent(1811, 9002, 52, "Отвал F")]});
    assert.equal((await second).accepted, true);
    assert.equal(runtime.bridgeCalls.length, 2, "the released claim gives the retry a chance");
    assert.equal(runtime.window.DriverVoiceGuard.state("dump:9002:id-52"), "announced");
});

test("a late bridge success after the deadline is not announced twice", async () => {
    const late = {};
    late.promise = new Promise((resolve) => { late.resolve = resolve; });
    let attempts = 0;
    const runtime = createDriverVoiceRuntime({
        announceResult: () => {
            attempts += 1;
            return attempts === 1
                ? late.promise
                : {supported: true, announced: false, reason: "already_announced"};
        },
        soundResult: true,
    });
    const first = runtime.signal({events: [dumpPointEvent(1820, 9003, 53, "Отвал H")]});
    await settlePromises();
    fireVoiceDeadline(runtime);
    assert.equal((await first).accepted, true);

    /* Поздний ответ моста: звук всё-таки прозвучал уже после срока. */
    late.resolve({supported: true, announced: true, processed: true});
    await settlePromises();

    const second = runtime.signal({events: [dumpPointEvent(1821, 9003, 53, "Отвал H")]});
    assert.equal((await second).accepted, true);
    assert.equal(runtime.bridgeCalls.length, 2, "the retry asks the bridge and the ledger answers");
    assert.deepEqual(runtime.soundCalls, [], "already_announced must not add a fallback cue");
    assert.equal(runtime.window.DriverVoiceGuard.state("dump:9003:id-53"), "announced");
});

test("a bridge timeout result adds no web cue and does not hold the cursor", async () => {
    const runtime = createDriverVoiceRuntime({
        announceResult: {supported: true, announced: false, reason: "bridge_timeout"},
        soundResult: true,
    });
    const result = runtime.signal({events: [dumpPointEvent(1830, 9004, 54, "Отвал K")]});

    assert.equal((await result).accepted, true);
    assert.deepEqual(runtime.soundCalls, [], "the bridge may still speak: no second sound");
    assert.equal(runtime.window.DriverVoiceGuard.state("dump:9004:id-54"), "");
});

test("a hung assignment bridge resolves the handoff within the deadline", async () => {
    const runtime = createDriverVoiceRuntime({announceResult: () => new Promise(() => {})});
    const outcome = runtime.signal({events: [assignmentEvent(1840, 741, "assignment_pending", "19")]});
    await settlePromises();
    assert.equal(runtime.window.DriverVoiceGuard.state("assign:741"), "claimed");

    fireVoiceDeadline(runtime);
    assert.equal((await outcome).accepted, true);
    assert.equal(runtime.window.DriverVoiceGuard.state("assign:741"), "");
});

test("two hung handoffs still resolve the combined promise", async () => {
    const runtime = createDriverVoiceRuntime({announceResult: () => new Promise(() => {})});
    let settled = null;
    runtime.signal({
        events: [
            assignmentEvent(1870, 743, "assignment_pending", "21"),
            dumpPointEvent(1871, 9006, 56, "Отвал M"),
        ],
    }).then((value) => { settled = value; });
    await settlePromises();

    assert.equal(settled, null, "both handoffs are still awaiting their deadline");
    let rounds = 0;
    while (runtime.timeouts.filter(Boolean).some((timer) => timer.delay === VOICE_DEADLINE_MS)) {
        fireVoiceDeadline(runtime);
        rounds += 1;
        assert.ok(rounds <= 4, "deadlines must not multiply");
    }
    await settlePromises();

    assert.equal(settled && settled.accepted, true, "Promise.all must always settle");
});

test("an explicit device refusal still keeps the cursor pending for a retry", async () => {
    const runtime = createDriverVoiceRuntime({
        announceResult: {supported: true, announced: false, reason: "resource_unavailable"},
        soundResult: false,
    });
    const result = runtime.signal({events: [assignmentEvent(1850, 742, "assignment_pending", "20")]});

    assert.equal((await result).accepted, false, "a device that said no must be retried");
    assert.equal(runtime.window.DriverVoiceGuard.state("assign:742"), "");
});

test("a browser without any voice channel does not hold the cursor", async () => {
    const runtime = createDriverVoiceRuntime({
        nativeApp: false,
        announceResult: {supported: false, announced: false},
        soundResult: false,
    });
    const result = runtime.signal({events: [dumpPointEvent(1860, 9005, 55, "Отвал L")]});

    assert.equal(
        (await result).accepted,
        true,
        "nothing can be played, so nothing is worth waiting for"
    );
    assert.equal(runtime.window.DriverVoiceGuard.state("dump:9005:id-55"), "");
});

/* ------------------------------------------------------------------ */
/* Контракт шаблона и клиента                                          */
/* ------------------------------------------------------------------ */

test("the Driver assignment form exposes an explicit assignment identifier", () => {
    assert.match(
        DRIVER_TEMPLATE_SOURCE,
        /data-driver-assignment-id="\{\{ pending_assignment_action\.id \}\}"/,
        "the DOM fallback must read the identifier instead of parsing the form action"
    );
});

test("the DOM fallback never sends a non-positive version", () => {
    assert.doesNotMatch(
        DRIVER_TEMPLATE_SOURCE,
        /playDriverAssignmentAlert\(\s*0\s*,/,
        "a zero version would be rejected natively and would lose the recorded phrase"
    );
    assert.match(
        DRIVER_TEMPLATE_SOURCE,
        /playDriverAssignmentAlert\(\s*\n?\s*targetVersion,/,
        "the DOM fallback keeps the positive state version"
    );
});

test("the asynchronous dump-point marker is gone", () => {
    assert.doesNotMatch(
        DRIVER_TEMPLATE_SOURCE,
        /driverLastDumpPointAlertVersion/,
        "the old marker was assigned only after the bridge returned and guarded nothing"
    );
});

/* ------------------------------------------------------------------ */
/* Ранний сигнал в realtime-client                                     */
/* ------------------------------------------------------------------ */

class FakeClassList {
    constructor() {
        this.values = new Set();
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
        removeEventListener(type, listener) {
            const registered = listeners.get(type) || [];
            listeners.set(type, registered.filter((candidate) => candidate !== listener));
        },
        dispatchEvent(event) {
            const registered = (listeners.get(event.type) || []).slice();
            registered.forEach((listener) => listener.call(this, event));
            return true;
        },
    };
}

function createStorage() {
    const values = new Map();
    return {
        getItem(key) {
            return values.has(String(key)) ? values.get(String(key)) : null;
        },
        setItem(key, value) {
            values.set(String(key), String(value));
        },
        removeItem(key) {
            values.delete(String(key));
        },
    };
}

function statePayload(overrides) {
    return Object.assign({
        authenticated: true,
        role_active: true,
        key: "production",
        version: 120,
        relevant: true,
        events_truncated: false,
        events: [assignmentEvent(120, 501, "assignment_pending", "11")],
    }, overrides || {});
}

function createRealtimeRuntime(options) {
    const runtimeOptions = options || {};
    const windowTarget = createEventTarget();
    const documentTarget = createEventTarget();
    const signals = [];
    const fetchCalls = [];
    const timeouts = new Map();
    const intervals = new Map();
    let nextTimerId = 1;
    const currentHref = "http://driver.localhost/driver/";

    const body = {
        dataset: {
            roleAccessActive: "true",
            operationalStateVersion: String(
                typeof runtimeOptions.initialVersion === "number" ? runtimeOptions.initialVersion : 100
            ),
            appRoleCode: "driver",
        },
        classList: new FakeClassList(),
    };
    const documentObject = Object.assign(documentTarget, {
        body,
        activeElement: runtimeOptions.activeElement || null,
        hidden: false,
        hasFocus() {
            return true;
        },
        querySelector() {
            return null;
        },
    });
    const location = {
        href: currentHref,
        origin: new URL(currentHref).origin,
        pathname: new URL(currentHref).pathname,
        replace() {},
        reload() {},
    };
    const responses = (runtimeOptions.payloads || [statePayload()]).slice();
    const windowObject = Object.assign(windowTarget, {
        AppRealtimeConfig: {
            stateUrl: "/realtime/state/",
            initialVersion: typeof runtimeOptions.initialVersion === "number"
                ? runtimeOptions.initialVersion
                : 100,
            workPollIntervalMs: 5000,
            observerPollIntervalMs: 15000,
            idleDelayMs: 2500,
            pollTimeoutMs: 8000,
            maxSilentMs: 7000,
            mobileQueueKey: MOBILE_QUEUE_KEY,
            screens: [{
                name: "driver",
                role: "driver",
                mode: "custom",
                path: "^/driver/?$",
                customRefresh: true,
                foregroundReconcile: true,
            }],
            customRefreshPaths: ["^/driver/?$"],
        },
        location,
        localStorage: createStorage(),
        sessionStorage: createStorage(),
        navigator: {onLine: true},
        AbortController,
        handleOperationalStateSignals(signalContext) {
            signals.push(signalContext);
        },
        applyOperationalStateRefresh() {
            /* Фрагмент никогда не отвечает: ранний сигнал обязан пройти и без
               него, иначе голос по-прежнему ждёт обновления экрана. */
            return new Promise(function () {});
        },
        fetch(url, fetchOptions) {
            fetchCalls.push({url: String(url), options: fetchOptions});
            const next = responses.length ? responses.shift() : statePayload();
            return Promise.resolve({
                status: 200,
                ok: true,
                json() {
                    return Promise.resolve(next);
                },
            });
        },
        requestAnimationFrame(callback) {
            callback();
            return 0;
        },
        setTimeout(callback, delay) {
            const timerId = nextTimerId++;
            timeouts.set(timerId, {callback, delay: Number(delay || 0)});
            return timerId;
        },
        clearTimeout(timerId) {
            timeouts.delete(timerId);
        },
        setInterval(callback, delay) {
            const timerId = nextTimerId++;
            intervals.set(timerId, {callback, delay: Number(delay || 0)});
            return timerId;
        },
        clearInterval(timerId) {
            intervals.delete(timerId);
        },
    });
    windowObject.window = windowObject;
    windowObject.document = documentObject;
    documentObject.location = location;

    class FakeCustomEvent {
        constructor(type, init) {
            this.type = type;
            this.detail = init && init.detail ? init.detail : {};
        }
    }

    const context = vm.createContext({
        window: windowObject,
        document: documentObject,
        navigator: windowObject.navigator,
        CustomEvent: FakeCustomEvent,
        AbortController,
        URL,
        Promise,
        Error,
        Date,
        JSON,
        Math,
        Number,
        Object,
        Array,
        RegExp,
        String,
        Boolean,
        console,
    });
    vm.runInContext(REALTIME_CLIENT_SOURCE, context, {filename: REALTIME_CLIENT_PATH});
    documentObject.dispatchEvent({type: "DOMContentLoaded"});

    return {
        window: windowObject,
        signals,
        fetchCalls,
        /* Пробуждение после фона откладывает опрос нулевым таймером. */
        flushZeroTimers() {
            let ranTimer = true;
            while (ranTimer) {
                ranTimer = false;
                for (const [timerId, timer] of Array.from(timeouts.entries())) {
                    if (timer.delay !== 0) continue;
                    timeouts.delete(timerId);
                    timer.callback();
                    ranTimer = true;
                }
            }
        },
    };
}

test("the early signal fires while the fragment refresh never resolves", async () => {
    const runtime = createRealtimeRuntime();
    await settlePromises();

    assert.equal(
        runtime.signals.length,
        1,
        "the voice signal must not wait for the screen fragment"
    );
    assert.equal(runtime.signals[0].events[0].version, 120);
    assert.equal(runtime.signals[0].requestedAfterVersion, 100);
});

test("the early signal fires while the interface is busy", async () => {
    const runtime = createRealtimeRuntime({
        activeElement: {tagName: "INPUT", isContentEditable: false},
    });
    await settlePromises();

    assert.equal(
        runtime.signals.length,
        1,
        "input focus defers the screen update, it must not defer the voice"
    );
});

test("the initial bootstrap never announces stored history", async () => {
    const runtime = createRealtimeRuntime({
        initialVersion: 0,
        payloads: [statePayload({version: 120})],
    });
    await settlePromises();

    assert.equal(
        runtime.fetchCalls[0].url.includes("after="),
        false,
        "the first request is sent without a cursor"
    );
    assert.equal(
        runtime.signals.length,
        0,
        "a response to a request without a cursor must not replay history"
    );
});

test("a truncated delta is ignored by the early hook", async () => {
    const runtime = createRealtimeRuntime({
        payloads: [statePayload({events_truncated: true})],
    });
    await settlePromises();

    assert.equal(
        runtime.signals.length,
        0,
        "a truncated delta carries the oldest events, so the newest one may be missing"
    );
});

test("an irrelevant response is ignored by the early hook", async () => {
    const runtime = createRealtimeRuntime({
        payloads: [statePayload({relevant: false, events: []})],
    });
    await settlePromises();

    assert.equal(runtime.signals.length, 0);
});

test("only events newer than the requested cursor are signalled", async () => {
    const runtime = createRealtimeRuntime({
        payloads: [statePayload({
            version: 120,
            events: [
                assignmentEvent(95, 601, "assignment_pending", "01"),
                assignmentEvent(120, 602, "assignment_pending", "02"),
            ],
        })],
    });
    await settlePromises();

    assert.deepEqual(
        runtime.signals[0].events.map((event) => event.version),
        [120],
        "an event at or below the requested cursor has already been handled"
    );
});

test("the captured cursor belongs to the request, not to the current state", async () => {
    const runtime = createRealtimeRuntime({
        payloads: [statePayload({version: 100, events: [], relevant: false})],
    });
    await settlePromises();

    assert.equal(
        runtime.signals.length,
        0,
        "a response that does not move past the requested cursor announces nothing"
    );
});

test("returning from the background uses the exact event version", async () => {
    const runtime = createRealtimeRuntime({
        payloads: [
            statePayload({version: 120, events: [assignmentEvent(120, 501, "assignment_pending", "11")]}),
            statePayload({
                version: 145,
                events: [
                    assignmentEvent(120, 501, "assignment_pending", "11"),
                    assignmentEvent(145, 502, "assignment_pending", "14"),
                ],
            }),
        ],
    });
    await settlePromises();
    const baseline = runtime.signals.length;

    runtime.window.dispatchEvent({type: "pagehide"});
    runtime.window.dispatchEvent({type: "pageshow", persisted: true});
    runtime.flushZeroTimers();
    await settlePromises();

    const reconcileSignals = runtime.signals.slice(baseline);
    assert.ok(
        reconcileSignals.length >= 1,
        "the foreground reconcile path must reach the voice hook as well"
    );
    reconcileSignals.forEach((signal) => {
        signal.events.forEach((event) => {
            assert.ok(
                Number(event.version) > Number(signal.requestedAfterVersion),
                "the exact event version is passed on; the native side deduplicates it"
            );
        });
    });
});
