"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");


const TEMPLATE_ROOT = path.resolve(__dirname, "..", "..", "..", "templates");
const DRIVER_SOURCE = fs.readFileSync(
    path.join(TEMPLATE_ROOT, "users", "driver_shift.html"),
    "utf8"
);
const EXCAVATOR_SOURCE = fs.readFileSync(
    path.join(TEMPLATE_ROOT, "trips", "excavator_work.html"),
    "utf8"
);


function extractMarkedSource(source, startMarker, endMarker, label) {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start + startMarker.length);
    assert.notEqual(start, -1, `${label} start marker was not found.`);
    assert.notEqual(end, -1, `${label} end marker was not found.`);
    assert.ok(end > start, `${label} markers are in the wrong order.`);
    return source.slice(start + startMarker.length, end);
}


function extractBraceBlock(source, signature, label, fromIndex = 0) {
    const start = source.indexOf(signature, fromIndex);
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
        if (character === "'" || character === '"' || character === "`") {
            quote = character;
            continue;
        }
        if (character === "{") {
            depth += 1;
        } else if (character === "}") {
            depth -= 1;
            if (depth === 0) return source.slice(start, index + 1);
        }
    }
    assert.fail(`${label} closing brace was not found.`);
}


function createFrameQueue(startFrameId = 1) {
    const frames = new Map();
    let nextFrameId = startFrameId;
    return {
        window: {
            requestAnimationFrame(callback) {
                const frameId = nextFrameId;
                nextFrameId += 1;
                frames.set(frameId, callback);
                return frameId;
            },
            cancelAnimationFrame(frameId) {
                frames.delete(frameId);
            },
        },
        runFrame() {
            const callbacks = Array.from(frames.values());
            frames.clear();
            callbacks.forEach((callback) => callback());
        },
        pendingCount() {
            return frames.size;
        },
    };
}


function loadScheduler(options) {
    const queue = createFrameQueue(options.frameStartId);
    const callbackTabs = [];
    const fullFitCalls = [];
    const overflowResults = [];
    let overflowCalls = 0;
    let viewportFitGeneration = 0;
    const activePanel = {
        isConnected: true,
        dataset: {driverTabPanel: ""},
        classList: {
            contains(name) {
                return name === "is-active";
            },
        },
    };
    const shell = {
        isConnected: true,
        dataset: {
            activeTab: "",
            driverDensity: "normal",
            driverViewportDensity: "normal",
            eoActiveTab: "",
        },
        querySelector(selector) {
            return selector === "[data-driver-tab-panel].is-active"
                ? context.activePanel
                : null;
        },
    };
    const context = {
        activePanel,
        callbackTabs,
        schedule: null,
        scheduleViewport: null,
    };
    const sandbox = {
        context,
        shell,
        window: queue.window,
        fitDriverText(panel) {
            callbackTabs.push(panel.dataset.driverTabPanel);
        },
        driverPanelOverflows() {
            overflowCalls += 1;
            return overflowResults.length ? overflowResults.shift() : false;
        },
        driverIsEditingField() {
            return false;
        },
        driverViewportIsTemporarilyReduced() {
            return false;
        },
        invalidateDriverViewportFit() {
            viewportFitGeneration += 1;
            [
                "driverViewportFitFrame",
                "driverViewportFitTextFrame",
                "driverViewportFitDensityFrame",
            ].forEach((key) => {
                if (queue.window[key] !== null && typeof queue.window[key] !== "undefined") {
                    queue.window.cancelAnimationFrame(queue.window[key]);
                    queue.window[key] = null;
                }
            });
            return viewportFitGeneration;
        },
        driverViewportFitIsCurrent(generation) {
            return generation === viewportFitGeneration && shell.isConnected;
        },
        fitDriverViewport(generation) {
            fullFitCalls.push(generation);
        },
        syncDowntimeStatus() {
            callbackTabs.push("events");
        },
    };
    vm.runInNewContext(
        `${options.source}\n${options.additionalSource || ""}\n` +
            `context.schedule = typeof ${options.functionName} === "function" ? ${options.functionName} : null;\n` +
            "context.scheduleViewport = typeof scheduleDriverViewportFit === \"function\" ? scheduleDriverViewportFit : null;",
        sandbox,
        {filename: options.filename}
    );
    assert.equal(
        typeof context.schedule,
        "function",
        `${options.functionName} must be declared inside its production marker.`
    );
    return {
        callbackTabs,
        fullFitCalls,
        queue,
        shell,
        activePanel,
        disconnect() {
            shell.isConnected = false;
        },
        setActivePanel(panel) {
            context.activePanel = panel;
        },
        setOverflowResults(results) {
            overflowResults.splice(0, overflowResults.length, ...results);
        },
        overflowCallCount() {
            return overflowCalls;
        },
        schedule(tab) {
            shell.dataset.driverDensity = shell.dataset.driverViewportDensity
                || shell.dataset.driverDensity
                || "normal";
            shell.dataset.activeTab = tab;
            shell.dataset.eoActiveTab = tab;
            activePanel.dataset.driverTabPanel = tab;
            context.schedule(tab);
        },
        scheduleViewport() {
            assert.equal(typeof context.scheduleViewport, "function");
            context.scheduleViewport();
        },
    };
}


function loadDriverDialFitter() {
    const source = [
        extractBraceBlock(
            DRIVER_SOURCE,
            "function splitDriverDialLabel(text)",
            "Driver dial label splitter"
        ),
        extractBraceBlock(
            DRIVER_SOURCE,
            "function preferredDriverDialFontSize(coreWidth, lineCount, textLength)",
            "Driver preferred dial font size"
        ),
        extractBraceBlock(
            DRIVER_SOURCE,
            "function minimumDriverDialFontSize(lineCount, textLength)",
            "Driver minimum dial font size"
        ),
        extractBraceBlock(
            DRIVER_SOURCE,
            "function renderDriverDialLabel(label, text)",
            "Driver dial label renderer"
        ),
        extractBraceBlock(
            DRIVER_SOURCE,
            "function driverDialCoreHasVisibleGeometry(core)",
            "Driver dial visible-geometry guard"
        ),
        extractBraceBlock(
            DRIVER_SOURCE,
            "function fitDriverDialLabel(label, force)",
            "Driver dial label fitter"
        ),
    ].join("\n");

    let coreWidth = 260;
    let coreHeight = 260;
    let fontSize = "35.5px";
    let fontSizeWrites = 0;
    let fontSizeRemovals = 0;
    let replaceCalls = 0;
    const attributes = new Map();
    const labelClasses = new Set(["is-two-line"]);
    const coreClasses = new Set(["has-multiline-label"]);
    const children = [{className: "driver-work-label-line", textContent: "ОЖИДАНИЕ"}];

    const label = {
        dataset: {
            driverDialRaw: "ОЖИДАНИЕ РАЗГРУЗКИ",
            driverDialFitText: "ОЖИДАНИЕ РАЗГРУЗКИ",
            driverDialFitWidth: "260",
            driverDialFitHeight: "260",
        },
        style: {
            removeProperty(name) {
                if (name !== "font-size") return;
                fontSize = "";
                fontSizeRemovals += 1;
            },
            get fontSize() {
                return fontSize;
            },
            set fontSize(value) {
                fontSize = value;
                fontSizeWrites += 1;
            },
        },
        classList: {
            add(name) {
                labelClasses.add(name);
            },
            remove(...names) {
                names.forEach((name) => labelClasses.delete(name));
            },
        },
        get children() {
            return children;
        },
        get clientWidth() {
            return Math.max(0, Math.round(coreWidth * 0.92));
        },
        get scrollHeight() {
            if (!(coreHeight > 0)) return 0;
            const size = parseFloat(fontSize) || 56;
            return Math.ceil(children.length * size * 1.04 + 13);
        },
        get textContent() {
            return this.dataset.driverDialRaw || children.map((child) => child.textContent).join(" ");
        },
        closest(selector) {
            return selector === ".driver-work-dial-core" ? core : null;
        },
        replaceChildren() {
            replaceCalls += 1;
            children.splice(0, children.length);
        },
        appendChild(child) {
            children.push(child);
            return child;
        },
        setAttribute(name, value) {
            attributes.set(name, value);
        },
    };
    const core = {
        isConnected: true,
        hidden: false,
        get clientWidth() {
            return coreWidth;
        },
        get clientHeight() {
            return coreHeight;
        },
        classList: {
            toggle(name, force) {
                if (force) coreClasses.add(name);
                else coreClasses.delete(name);
            },
        },
        querySelector(selector) {
            if (selector === ".driver-work-percent") return {offsetHeight: coreHeight > 0 ? 22 : 0};
            if (selector === ".driver-work-note") return {offsetHeight: coreHeight > 0 ? 15 : 0};
            return null;
        },
        closest() {
            return null;
        },
        getClientRects() {
            return coreWidth > 0 && coreHeight > 0 ? [{}] : [];
        },
    };
    const document = {
        createElement() {
            const node = {className: "", textContent: ""};
            Object.defineProperty(node, "scrollWidth", {
                get() {
                    if (!(coreWidth > 0)) return 0;
                    const size = parseFloat(fontSize) || 56;
                    return Math.ceil(String(node.textContent || "").length * size * 0.58);
                },
            });
            return node;
        },
    };
    const context = {fit: null};
    vm.runInNewContext(
        `${source}\ncontext.fit = fitDriverDialLabel;`,
        {
            context,
            document,
            window: {
                getComputedStyle() {
                    return {rowGap: "12px", gap: "12px"};
                },
            },
        },
        {filename: "driver_shift.html#driver-dial-fit"}
    );
    assert.equal(typeof context.fit, "function");

    return {
        label,
        fit(force) {
            context.fit(label, force);
        },
        setGeometry(width, height) {
            coreWidth = width;
            coreHeight = height;
        },
        setText(text) {
            label.dataset.driverDialRaw = text;
        },
        snapshot() {
            return {
                fontSize,
                fontSizeWrites,
                fontSizeRemovals,
                replaceCalls,
                children: children.map((child) => ({
                    className: child.className,
                    textContent: child.textContent,
                })),
                dataset: {...label.dataset},
                attributes: Object.fromEntries(attributes),
            };
        },
    };
}


test("Driver dial fitting preserves the last visible result while its panel is hidden", () => {
    const runtime = loadDriverDialFitter();
    runtime.fit();
    const before = runtime.snapshot();

    runtime.setGeometry(0, 0);
    runtime.fit();

    assert.deepEqual(
        runtime.snapshot(),
        before,
        "A zero-size hidden core must not clear the font, rebuild children, or overwrite the fit cache."
    );
});


test("Driver dial fitting is cached until visible geometry or text changes", () => {
    const runtime = loadDriverDialFitter();

    runtime.fit();
    const stable = runtime.snapshot();
    runtime.fit();
    assert.deepEqual(
        runtime.snapshot(),
        stable,
        "The same text and visible core geometry must not rebuild or refit the label."
    );

    runtime.setGeometry(300, 300);
    const beforeResize = runtime.snapshot();
    runtime.fit();
    const resized = runtime.snapshot();
    assert.ok(
        resized.fontSizeWrites > beforeResize.fontSizeWrites,
        "A genuine visible core resize must refit the label."
    );

    runtime.setText("РАЗГРУЗИТЬ");
    const beforeTextChange = runtime.snapshot();
    runtime.fit();
    const changedText = runtime.snapshot();
    assert.ok(
        changedText.replaceCalls > beforeTextChange.replaceCalls,
        "A genuine text change must rebuild and refit the label."
    );
    assert.equal(changedText.attributes["aria-label"], "РАЗГРУЗИТЬ");
});


test("Driver dial fitting can be forced after web fonts finish loading", () => {
    const runtime = loadDriverDialFitter();

    runtime.fit();
    const stable = runtime.snapshot();
    runtime.fit();
    assert.deepEqual(runtime.snapshot(), stable, "An ordinary identical fit must use the cache.");

    runtime.fit(true);
    const forced = runtime.snapshot();
    assert.ok(
        forced.fontSizeWrites > stable.fontSizeWrites,
        "A forced fit must recalculate font size even when text and core geometry are unchanged."
    );
    assert.ok(
        forced.replaceCalls > stable.replaceCalls,
        "A forced fit must rebuild the label using the resolved web font metrics."
    );
    assert.equal(forced.fontSize, stable.fontSize);
    assert.equal(forced.dataset.driverDialFitKey, stable.dataset.driverDialFitKey);
    assert.match(
        DRIVER_SOURCE,
        /document\.fonts\.ready\.then\s*\(\s*function\s*\(\s*\)\s*\{[\s\S]*scheduleDriverDialLabelFit\s*\(\s*true\s*\)/,
        "The web-font completion hook must request a forced dial-label fit."
    );
});


test("Driver preserves a forced web-font refit requested while Work is hidden", () => {
    const runtime = loadDriverDialFitter();

    runtime.fit();
    const visible = runtime.snapshot();
    runtime.setGeometry(0, 0);
    runtime.fit(true);
    const hiddenForced = runtime.snapshot();
    assert.equal(hiddenForced.fontSize, visible.fontSize);
    assert.equal(hiddenForced.replaceCalls, visible.replaceCalls);
    assert.equal(hiddenForced.dataset.driverDialFitKey, undefined);

    runtime.setGeometry(260, 260);
    runtime.fit();
    const returned = runtime.snapshot();
    assert.ok(
        returned.fontSizeWrites > hiddenForced.fontSizeWrites,
        "Returning to Work must complete the web-font refit that was requested while hidden."
    );
    assert.ok(
        returned.replaceCalls > hiddenForced.replaceCalls,
        "The first visible fit after font resolution must rebuild the label with current metrics."
    );
    assert.ok(returned.dataset.driverDialFitKey);
});


test("Driver tab settling runs after two frames and replaces stale work", () => {
    const source = extractMarkedSource(
        DRIVER_SOURCE,
        "/* DRIVER_TAB_SETTLE_START */",
        "/* DRIVER_TAB_SETTLE_END */",
        "Driver tab settle scheduler"
    );
    const runtime = loadScheduler({
        source,
        functionName: "scheduleDriverTabSettle",
        filename: "driver_shift.html#driver-tab-settle",
    });

    runtime.schedule("work");
    assert.deepEqual(runtime.callbackTabs, [], "The click stack must stay measurement-free.");
    assert.equal(runtime.queue.pendingCount(), 1);
    runtime.queue.runFrame();
    assert.deepEqual(runtime.callbackTabs, [], "The first frame must paint the selected tab.");
    assert.equal(runtime.queue.pendingCount(), 1);
    runtime.queue.runFrame();
    assert.deepEqual(runtime.callbackTabs, ["work"]);

    runtime.callbackTabs.length = 0;
    runtime.schedule("shift");
    runtime.schedule("manifest");
    assert.equal(runtime.queue.pendingCount(), 1, "A newer tab must cancel the stale first frame.");
    runtime.queue.runFrame();
    assert.deepEqual(runtime.callbackTabs, []);
    runtime.queue.runFrame();
    assert.deepEqual(runtime.callbackTabs, ["manifest"]);
});


test("Driver density settles from normal through compact to tight", () => {
    const source = extractMarkedSource(
        DRIVER_SOURCE,
        "/* DRIVER_TAB_SETTLE_START */",
        "/* DRIVER_TAB_SETTLE_END */",
        "Driver tab settle scheduler"
    );
    const runtime = loadScheduler({
        source,
        functionName: "scheduleDriverTabSettle",
        filename: "driver_shift.html#driver-tab-density",
    });
    runtime.setOverflowResults([true, true]);

    runtime.schedule("work");
    runtime.queue.runFrame();
    runtime.queue.runFrame();
    assert.deepEqual(runtime.callbackTabs, ["work"]);
    assert.equal(runtime.shell.dataset.driverDensity, "compact");
    assert.equal(runtime.queue.pendingCount(), 1);

    runtime.queue.runFrame();
    assert.equal(runtime.shell.dataset.driverDensity, "tight");
    assert.equal(runtime.queue.pendingCount(), 0);
    assert.equal(runtime.overflowCallCount(), 2);
});


test("Driver tab switch cancels an older density frame", () => {
    const source = extractMarkedSource(
        DRIVER_SOURCE,
        "/* DRIVER_TAB_SETTLE_START */",
        "/* DRIVER_TAB_SETTLE_END */",
        "Driver tab settle scheduler"
    );
    const runtime = loadScheduler({
        source,
        functionName: "scheduleDriverTabSettle",
        filename: "driver_shift.html#driver-tab-density-cancel",
    });
    runtime.setOverflowResults([true]);
    runtime.schedule("work");
    runtime.queue.runFrame();
    runtime.queue.runFrame();
    assert.equal(runtime.shell.dataset.driverDensity, "compact");
    assert.equal(runtime.queue.pendingCount(), 1);

    runtime.setOverflowResults([false]);
    runtime.schedule("manifest");
    assert.equal(runtime.queue.pendingCount(), 1, "Only the new tab first frame may remain.");
    runtime.queue.runFrame();
    runtime.queue.runFrame();
    assert.deepEqual(runtime.callbackTabs, ["work", "manifest"]);
    assert.equal(
        runtime.shell.dataset.driverDensity,
        "normal",
        "The new non-overflowing tab must use the viewport baseline, not the stale density frame."
    );
    assert.equal(runtime.queue.pendingCount(), 0);
});


test("Driver applies viewport density and schedules Work dial fit before its first paint", () => {
    const openDriverTab = extractBraceBlock(
        DRIVER_SOURCE,
        "function openDriverTab(tab)",
        "Driver openDriverTab"
    );
    const baselineIndex = openDriverTab.search(
        /shell\.dataset\.driverDensity\s*=\s*shell\.dataset\.driverViewportDensity/
    );
    const panelToggleIndex = openDriverTab.search(
        /panel\.classList\.toggle\s*\(\s*["']is-active["']/
    );
    const dialFitIndex = openDriverTab.indexOf("scheduleDriverDialLabelFit", panelToggleIndex);
    const tabSettleIndex = openDriverTab.indexOf("scheduleDriverTabSettle", panelToggleIndex);

    assert.notEqual(baselineIndex, -1, "Every tab click must restore the viewport density baseline.");
    assert.notEqual(panelToggleIndex, -1, "The active panel toggle was not found.");
    assert.ok(
        baselineIndex < panelToggleIndex,
        "Density must be stable before the newly selected panel becomes visible."
    );
    assert.match(
        openDriverTab,
        /tab\s*===\s*["']work["'][\s\S]*scheduleDriverDialLabelFit\s*\(/,
        "Opening Work must schedule its dial-label fit in the click stack."
    );
    assert.ok(
        dialFitIndex > panelToggleIndex && dialFitIndex < tabSettleIndex,
        "Work label fitting must be queued after activation and before the generic tab settle."
    );

    const settleSource = extractMarkedSource(
        DRIVER_SOURCE,
        "/* DRIVER_TAB_SETTLE_START */",
        "/* DRIVER_TAB_SETTLE_END */",
        "Driver tab settle scheduler"
    );
    const settleSchedule = extractBraceBlock(
        settleSource,
        "function scheduleDriverTabSettle(expectedTab)",
        "Driver tab settle schedule"
    );
    const settleBaselineIndex = settleSchedule.search(
        /shell\.dataset\.driverDensity\s*=\s*shell\.dataset\.driverViewportDensity/
    );
    const firstFrameIndex = settleSchedule.indexOf("window.requestAnimationFrame");
    const secondFrameIndex = settleSchedule.indexOf("driverTabSettleSecondFrame");
    assert.ok(
        settleBaselineIndex !== -1 && settleBaselineIndex < firstFrameIndex,
        "Any scheduler fallback baseline must also run synchronously before its first frame."
    );
    assert.doesNotMatch(
        settleSchedule.slice(secondFrameIndex),
        /shell\.dataset\.driverDensity\s*=\s*shell\.dataset\.driverViewportDensity/,
        "The second frame must not visibly reset the density baseline after the tab was painted."
    );
});


test("Driver full viewport scheduling preempts a pending tab settle", () => {
    const settleSource = extractMarkedSource(
        DRIVER_SOURCE,
        "/* DRIVER_TAB_SETTLE_START */",
        "/* DRIVER_TAB_SETTLE_END */",
        "Driver tab settle scheduler"
    );
    const viewportSource = extractBraceBlock(
        DRIVER_SOURCE,
        "function scheduleDriverViewportFit()",
        "Driver viewport scheduler"
    );
    const runtime = loadScheduler({
        source: settleSource,
        additionalSource: viewportSource,
        functionName: "scheduleDriverTabSettle",
        filename: "driver_shift.html#driver-viewport-preemption",
    });

    runtime.schedule("shift");
    runtime.queue.runFrame();
    assert.equal(runtime.queue.pendingCount(), 1, "The tab second frame must be pending.");
    runtime.scheduleViewport();
    assert.equal(runtime.queue.pendingCount(), 1, "The full fit must replace the tab frame.");
    runtime.queue.runFrame();
    assert.deepEqual(runtime.callbackTabs, []);
    assert.equal(runtime.fullFitCalls.length, 1);
    assert.equal(runtime.queue.pendingCount(), 0);

    const invalidateIndex = viewportSource.indexOf("invalidateDriverTabSettle()");
    const frameIndex = viewportSource.indexOf("requestAnimationFrame");
    assert.ok(invalidateIndex >= 0 && invalidateIndex < frameIndex);
    assert.match(
        DRIVER_SOURCE,
        /addEventListener\s*\(\s*["']resize["']\s*,\s*requestCurrentDriverViewportFit/
    );
    assert.match(DRIVER_SOURCE, /document\.fonts\.ready\.then\s*\(\s*scheduleDriverViewportFit\s*\)/);
});


test("Driver ignores disconnected, missing and wrong active panels", () => {
    const source = extractMarkedSource(
        DRIVER_SOURCE,
        "/* DRIVER_TAB_SETTLE_START */",
        "/* DRIVER_TAB_SETTLE_END */",
        "Driver tab settle scheduler"
    );

    const detachedBeforeFirst = loadScheduler({
        source,
        functionName: "scheduleDriverTabSettle",
        filename: "driver_shift.html#driver-detached-before-first",
    });
    detachedBeforeFirst.schedule("work");
    detachedBeforeFirst.disconnect();
    detachedBeforeFirst.queue.runFrame();
    assert.deepEqual(detachedBeforeFirst.callbackTabs, []);
    assert.equal(detachedBeforeFirst.queue.pendingCount(), 0);

    const detachedBeforeSecond = loadScheduler({
        source,
        functionName: "scheduleDriverTabSettle",
        filename: "driver_shift.html#driver-detached-before-second",
    });
    detachedBeforeSecond.schedule("work");
    detachedBeforeSecond.queue.runFrame();
    detachedBeforeSecond.disconnect();
    detachedBeforeSecond.queue.runFrame();
    assert.deepEqual(detachedBeforeSecond.callbackTabs, []);

    const missingPanel = loadScheduler({
        source,
        functionName: "scheduleDriverTabSettle",
        filename: "driver_shift.html#driver-missing-panel",
    });
    missingPanel.schedule("work");
    missingPanel.queue.runFrame();
    missingPanel.setActivePanel(null);
    missingPanel.queue.runFrame();
    assert.deepEqual(missingPanel.callbackTabs, []);

    const wrongPanel = loadScheduler({
        source,
        functionName: "scheduleDriverTabSettle",
        filename: "driver_shift.html#driver-wrong-panel",
    });
    wrongPanel.schedule("work");
    wrongPanel.queue.runFrame();
    wrongPanel.activePanel.dataset.driverTabPanel = "shift";
    wrongPanel.queue.runFrame();
    assert.deepEqual(wrongPanel.callbackTabs, []);
});


test("Excavator defers only the Events status sync and replaces stale work", () => {
    const source = extractMarkedSource(
        EXCAVATOR_SOURCE,
        "/* EXCAVATOR_TAB_SETTLE_START */",
        "/* EXCAVATOR_TAB_SETTLE_END */",
        "Excavator tab settle scheduler"
    );
    const runtime = loadScheduler({
        source,
        functionName: "scheduleExcavatorTabSettle",
        filename: "excavator_work.html#excavator-tab-settle",
    });

    runtime.schedule("events");
    assert.deepEqual(runtime.callbackTabs, []);
    runtime.queue.runFrame();
    assert.deepEqual(runtime.callbackTabs, []);
    runtime.queue.runFrame();
    assert.deepEqual(runtime.callbackTabs, ["events"]);

    runtime.callbackTabs.length = 0;
    runtime.schedule("events");
    runtime.schedule("trucks");
    runtime.queue.runFrame();
    runtime.queue.runFrame();
    assert.deepEqual(
        runtime.callbackTabs,
        [],
        "A stale Events click must not start a request after another tab wins."
    );

    runtime.schedule("shift");
    runtime.queue.runFrame();
    runtime.queue.runFrame();
    assert.deepEqual(runtime.callbackTabs, [], "Non-Events tabs must not sync downtime status.");
});


test("Excavator ignores a shell detached before either settle frame", () => {
    const source = extractMarkedSource(
        EXCAVATOR_SOURCE,
        "/* EXCAVATOR_TAB_SETTLE_START */",
        "/* EXCAVATOR_TAB_SETTLE_END */",
        "Excavator tab settle scheduler"
    );
    const beforeFirst = loadScheduler({
        source,
        functionName: "scheduleExcavatorTabSettle",
        filename: "excavator_work.html#excavator-detached-before-first",
    });
    beforeFirst.schedule("events");
    beforeFirst.disconnect();
    beforeFirst.queue.runFrame();
    assert.deepEqual(beforeFirst.callbackTabs, []);
    assert.equal(beforeFirst.queue.pendingCount(), 0);

    const beforeSecond = loadScheduler({
        source,
        functionName: "scheduleExcavatorTabSettle",
        filename: "excavator_work.html#excavator-detached-before-second",
    });
    beforeSecond.schedule("events");
    beforeSecond.queue.runFrame();
    beforeSecond.disconnect();
    beforeSecond.queue.runFrame();
    assert.deepEqual(beforeSecond.callbackTabs, []);
    assert.equal(beforeSecond.queue.pendingCount(), 0);
});


test("Driver and Excavator both cancel requestAnimationFrame id zero", () => {
    const cases = [
        {
            source: extractMarkedSource(
                DRIVER_SOURCE,
                "/* DRIVER_TAB_SETTLE_START */",
                "/* DRIVER_TAB_SETTLE_END */",
                "Driver tab settle scheduler"
            ),
            functionName: "scheduleDriverTabSettle",
            filename: "driver_shift.html#driver-zero-frame",
            first: "work",
            second: "shift",
            expected: ["shift"],
        },
        {
            source: extractMarkedSource(
                EXCAVATOR_SOURCE,
                "/* EXCAVATOR_TAB_SETTLE_START */",
                "/* EXCAVATOR_TAB_SETTLE_END */",
                "Excavator tab settle scheduler"
            ),
            functionName: "scheduleExcavatorTabSettle",
            filename: "excavator_work.html#excavator-zero-frame",
            first: "events",
            second: "trucks",
            expected: [],
        },
    ];

    cases.forEach((scenario) => {
        const runtime = loadScheduler({...scenario, frameStartId: 0});
        runtime.schedule(scenario.first);
        runtime.schedule(scenario.second);
        assert.equal(runtime.queue.pendingCount(), scenario.second === "trucks" ? 0 : 1);
        runtime.queue.runFrame();
        runtime.queue.runFrame();
        assert.deepEqual(runtime.callbackTabs, scenario.expected);
    });
});


test("Driver tab source keeps expensive fitting out of the click path", () => {
    const openDriverTab = extractBraceBlock(
        DRIVER_SOURCE,
        "function openDriverTab(tab)",
        "Driver openDriverTab"
    );
    assert.match(openDriverTab, /scheduleDriverTabSettle\s*\(\s*tab\s*\)/);
    assert.doesNotMatch(openDriverTab, /scheduleDriverViewportFit\s*\(/);

    const fitDriverText = extractBraceBlock(
        DRIVER_SOURCE,
        "function fitDriverText(",
        "Driver text fit"
    );
    assert.match(
        fitDriverText,
        /fitRoot\s*\.\s*querySelectorAll\s*\(/,
        "Driver text fitting must query only the supplied active panel/root."
    );
    assert.doesNotMatch(
        fitDriverText,
        /shell\s*\.\s*querySelectorAll\s*\(/,
        "Driver text fitting must not scan every hidden tab."
    );

    const settleSource = extractMarkedSource(
        DRIVER_SOURCE,
        "/* DRIVER_TAB_SETTLE_START */",
        "/* DRIVER_TAB_SETTLE_END */",
        "Driver tab settle scheduler"
    );
    assert.match(settleSource, /querySelector\s*\(\s*["']\[data-driver-tab-panel\]\.is-active["']\s*\)/);
    assert.match(settleSource, /fitDriverText\s*\(\s*panel\s*\)/);
    assert.doesNotMatch(settleSource, /fitDriverViewport\s*\(/);
    assert.match(
        DRIVER_SOURCE,
        /shell\.dataset\.driverViewportDensity\s*=\s*baseDensity/,
        "The full fit must remember the viewport baseline for later tabs."
    );
});


test("Excavator click paints first and defers the Events request", () => {
    const tabsBinding = EXCAVATOR_SOURCE.indexOf("tabs.forEach(function (tab)");
    assert.notEqual(tabsBinding, -1, "Excavator tabs binding was not found.");
    const clickHandler = extractBraceBlock(
        EXCAVATOR_SOURCE,
        'tab.addEventListener("click", function ()',
        "Excavator tab click handler",
        tabsBinding
    );
    const activateIndex = clickHandler.indexOf("activateTab(tabName)");
    const scheduleIndex = clickHandler.indexOf("scheduleExcavatorTabSettle(tabName)");
    assert.notEqual(activateIndex, -1, "The selected tab must be activated immediately.");
    assert.notEqual(scheduleIndex, -1, "The settle scheduler must run after activation.");
    assert.ok(activateIndex < scheduleIndex, "Activation must happen before deferred work is queued.");
    assert.doesNotMatch(
        clickHandler,
        /syncDowntimeStatus\s*\(/,
        "The click stack must not issue the Events status request directly."
    );

    const settleSource = extractMarkedSource(
        EXCAVATOR_SOURCE,
        "/* EXCAVATOR_TAB_SETTLE_START */",
        "/* EXCAVATOR_TAB_SETTLE_END */",
        "Excavator tab settle scheduler"
    );
    assert.match(settleSource, /(?:name|tabName)\s*!==\s*["']events["']/);
    assert.match(settleSource, /eoActiveTab\s*!==\s*["']events["']/);
    assert.match(settleSource, /syncDowntimeStatus\s*\(/);
});


test("Excavator downtime status applies only the latest connected-shell response", async () => {
    const source = extractBraceBlock(
        EXCAVATOR_SOURCE,
        "function syncDowntimeStatus()",
        "Excavator downtime status sync"
    );
    const pendingRequests = [];
    const applied = [];
    const cleared = [];
    const shell = {isConnected: true};
    const context = {
        currentShell: shell,
        sync: null,
    };
    function fakeFetch() {
        return new Promise((resolve) => {
            pendingRequests.push({resolve});
        });
    }
    vm.runInNewContext(
        `
        var downtimeStatusSyncGeneration = 0;
        ${source}
        context.sync = syncDowntimeStatus;
        `,
        {
            context,
            document: {
                querySelector(selector) {
                    assert.equal(selector, "[data-eo-shell]");
                    return context.currentShell;
                },
            },
            shell,
            fetch: fakeFetch,
            applyActiveDowntime(payload) {
                applied.push(payload.id);
            },
            clearActiveDowntime(payload) {
                cleared.push(payload.id);
            },
            assert,
        },
        {filename: "excavator_work.html#downtime-status-sync"}
    );

    const first = context.sync();
    const second = context.sync();
    pendingRequests[1].resolve({
        ok: true,
        json() {
            return Promise.resolve({id: "latest", active: true});
        },
    });
    const secondResult = await second;
    assert.equal(secondResult.id, "latest");
    assert.deepEqual(applied, ["latest"]);

    pendingRequests[0].resolve({
        ok: true,
        json() {
            return Promise.resolve({id: "stale", active: false});
        },
    });
    assert.equal(await first, null);
    assert.deepEqual(applied, ["latest"]);
    assert.deepEqual(cleared, []);

    const detached = context.sync();
    shell.isConnected = false;
    pendingRequests[2].resolve({
        ok: true,
        json() {
            return Promise.resolve({id: "detached", active: true});
        },
    });
    assert.equal(await detached, null);

    shell.isConnected = true;
    const replaced = context.sync();
    context.currentShell = {isConnected: true};
    pendingRequests[3].resolve({
        ok: true,
        json() {
            return Promise.resolve({id: "replaced", active: true});
        },
    });
    assert.equal(await replaced, null);
    assert.deepEqual(applied, ["latest"]);

    assert.match(source, /generation\s*!==\s*downtimeStatusSyncGeneration/);
    assert.match(source, /!shell\.isConnected/);
    assert.match(source, /document\.querySelector\s*\(\s*["']\[data-eo-shell\]["']\s*\)\s*!==\s*shell/);
});


test("Excavator fragment refresh preserves the latest current-shell tab", () => {
    const source = extractBraceBlock(
        EXCAVATOR_SOURCE,
        "function refreshExcavatorWorkFromServer(options)",
        "Excavator operational fragment refresh"
    );
    assert.match(
        source,
        /var\s+latestActiveTab\s*=\s*currentShell\.dataset\.eoActiveTab\s*\|\|\s*activeTab/
    );
    assert.match(
        source,
        /newShell\.dataset\.eoActiveTab\s*=\s*options\.preserveTab\s*===\s*false[\s\S]*:\s*latestActiveTab/
    );
    const latestIndex = source.indexOf("var latestActiveTab");
    const assignmentIndex = source.indexOf("newShell.dataset.eoActiveTab");
    const replaceIndex = source.indexOf("currentShell.replaceWith(newShell)");
    assert.ok(latestIndex >= 0 && latestIndex < assignmentIndex);
    assert.ok(assignmentIndex < replaceIndex);
});
