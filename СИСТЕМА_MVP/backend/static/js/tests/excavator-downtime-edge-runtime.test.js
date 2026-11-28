const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../excavator-downtime-edge-v1.js"), "utf8");

function downtimeGesture() {
    const window = {addEventListener() {}};
    const document = {body: {classList: {contains: () => false}}, querySelectorAll: () => []};
    vm.runInNewContext(source, {window, document});
    const reason = {closest: () => reason};
    const panel = {
        contains: target => target === reason,
        style: {setProperty() {}, removeProperty() {}},
        setAttribute() {}, removeAttribute() {},
    };
    const work = {hidden: true, setAttribute() {}, removeAttribute() {}};
    const listeners = {};
    const shell = {
        dataset: {eoActiveTab: "events"}, clientWidth: 360, isConnected: true,
        addEventListener(name, fn) { listeners[name] = fn; },
        querySelector: () => work,
        hasAttribute: () => shell.dataset.eoDowntimePreview !== undefined,
        removeAttribute() { delete shell.dataset.eoDowntimePreview; },
    };
    const switches = [];
    window.ExcavatorDowntimeEdge.bind(shell, panel, tab => switches.push(tab));
    function pointer(type, x, y = 100) {
        listeners[type]({target: reason, pointerType: "touch", isPrimary: true, button: 0,
            pointerId: 1, clientX: x, clientY: y});
    }
    function clickIsSuppressed() {
        let suppressed = false;
        listeners.click({detail: 1, preventDefault() { suppressed = true; }, stopImmediatePropagation() {}});
        return suppressed;
    }
    return {shell, work, switches, pointer, clickIsSuppressed};
}

test("right swipe from a downtime reason reveals Work without activating the reason", () => {
    const g = downtimeGesture();
    g.pointer("pointerdown", 50);
    g.pointer("pointermove", 145);
    assert.equal(g.work.hidden, false);
    assert.deepEqual(g.switches, []);
    g.pointer("pointerup", 150);
    assert.deepEqual(g.switches, ["trucks"]);
    assert.equal(g.clickIsSuppressed(), true);
});

test("short, cancelled and vertical gestures keep Downtime and the following tap works", () => {
    for (const [x, y, end] of [[58, 100, "pointerup"], [75, 100, "pointerup"], [155, 100, "pointercancel"], [54, 135, "pointerup"]]) {
        const g = downtimeGesture();
        g.pointer("pointerdown", 50);
        g.pointer("pointermove", x, y);
        g.pointer(end, x, y);
        assert.deepEqual(g.switches, []);
        assert.equal(g.shell.dataset.eoActiveTab, "events");
        assert.equal(g.work.hidden, true);
        assert.equal(g.clickIsSuppressed(), true);
        g.pointer("pointerdown", 50);
        g.pointer("pointerup", 50);
        assert.equal(g.clickIsSuppressed(), false);
    }
});
