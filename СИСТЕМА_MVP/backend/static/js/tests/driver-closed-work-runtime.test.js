"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const gestures = fs.readFileSync(path.resolve(__dirname, "../driver-shift-gestures-v1.js"), "utf8");

test("closed driver work blocks unload gestures and form submission until the shift opens", () => {
    const shell = {dataset: {driverShiftOpen: "false"}};
    const work = {closest: (selector) => selector === "[data-driver-shell]" ? shell : null};
    const workControl = {
        closest(selector) {
            assert.match(selector, /data-driver-tab-panel="work"/);
            return work;
        },
    };
    const shiftControl = {closest: () => null};
    const handlers = new Map();
    const notices = [];
    vm.runInNewContext(gestures, {window: {
        addEventListener(type, handler, options) {
            assert.equal(options.capture, true);
            handlers.set(type, handler);
        },
        showDriverToast: (...args) => notices.push(args),
    }});

    function isBlocked(type, target = workControl, key) {
        let prevented = false;
        let stopped = false;
        handlers.get(type)({
            type, target, key,
            preventDefault() { prevented = true; },
            stopImmediatePropagation() { stopped = true; },
        });
        assert.equal(prevented, stopped);
        return prevented;
    }

    assert.equal(isBlocked("pointerdown"), true);
    assert.deepEqual(notices, [["Сначала начните смену", 4000]]);
    for (const type of ["touchstart", "mousedown", "click", "submit"]) {
        assert.equal(isBlocked(type), true);
    }
    assert.equal(isBlocked("keydown", workControl, "Enter"), true);
    assert.equal(isBlocked("keydown", workControl, "Tab"), false);
    assert.equal(isBlocked("click", shiftControl), false);
    assert.equal(isBlocked("submit", shiftControl), false);

    shell.dataset.driverShiftOpen = "true";
    assert.equal(isBlocked("pointerdown"), false);
    assert.equal(isBlocked("submit"), false);
    assert.deepEqual(notices.map((notice) => notice[1]), [4000, 4000, 4000, 4000]);
});
