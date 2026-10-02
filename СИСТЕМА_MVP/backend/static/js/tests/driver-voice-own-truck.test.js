"use strict";
/* Бой 03.10.2026, самосвал 22: водителю приходят события экскаватора его
   назначения — и погрузки ЧУЖИХ самосвалов этим экскаватором. Голос объявлял
   точку чужой машины («едем на ККД», когда своя шла на СКДР). Озвучивается
   только свой самосвал; событие без номера (резервный путь по разметке своего
   экрана) — как раньше. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const VOICE = fs.readFileSync(path.resolve(__dirname, "../driver-shift-voice-v1.js"), "utf8").replace(/\r\n/g, "\n");

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

function select(events, ownTruckId) {
    const context = vm.createContext({
        Number, String, Array,
        document: {
            querySelector: (sel) => (sel === "[data-driver-shell]" && ownTruckId
                ? { dataset: { driverCurrentTruckId: String(ownTruckId) } }
                : null),
        },
        input: { events },
    });
    vm.runInContext(block(VOICE, "function latestDriverDumpPointEvent(context)"), context);
    const result = vm.runInContext("latestDriverDumpPointEvent(input)", context);
    return result ? JSON.parse(JSON.stringify(result)) : null;
}

function loaded(version, truckId, tripId, pointName) {
    return {
        version, type: "trip_changed",
        payload: { action: "truck_loaded", trip_id: tripId, truck_id: truckId, dump_point_id: 1, dump_point_name: pointName },
    };
}

test("a load of another truck by the same excavator is not announced to this driver", () => {
    assert.equal(select([loaded(40660, 7, 4163, "ККД")], 13), null);
});

test("this truck's own load is still announced, even when a stranger's load is newer", () => {
    const own = loaded(40638, 13, 4161, "СКДР");
    const stranger = loaded(40660, 7, 4163, "ККД");
    const picked = select([own, stranger], 13);
    assert.equal(picked.tripId, 4161);
    assert.equal(picked.dumpPointName, "СКДР");
});

test("an event without a truck number (fallback from this screen's markup) is announced as before", () => {
    const fallback = { version: 1, type: "trip_changed", payload: { action: "truck_loaded", trip_id: 4161, dump_point_name: "СКДР" } };
    assert.equal(select([fallback], 13).tripId, 4161);
    // Экран ещё не знает свой самосвал — фильтровать не по чему, поведение прежнее.
    assert.equal(select([loaded(5, 7, 4163, "ККД")], 0).tripId, 4163);
});
