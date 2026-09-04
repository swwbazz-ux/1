"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");


const TEMPLATE_ROOT = path.resolve(__dirname, "..", "..", "..", "templates");
const BASE_TEMPLATE_SOURCE = fs.readFileSync(
    path.join(TEMPLATE_ROOT, "base.html"),
    "utf8"
);
const DRIVER_TEMPLATE_SOURCE = fs.readFileSync(
    path.join(TEMPLATE_ROOT, "users", "driver_shift.html"),
    "utf8"
);
const EXCAVATOR_TEMPLATE_SOURCE = fs.readFileSync(
    path.join(TEMPLATE_ROOT, "trips", "excavator_work.html"),
    "utf8"
);
const DISPATCHER_TEMPLATE_SOURCE = fs.readFileSync(
    path.join(TEMPLATE_ROOT, "trips", "dispatcher_control.html"),
    "utf8"
);

const FRAGMENT_CLIENT_START = "/* OPERATIONAL_FRAGMENT_CLIENT_START */";
const FRAGMENT_CLIENT_END = "/* OPERATIONAL_FRAGMENT_CLIENT_END */";
const FRAGMENT_CONTRACT = "operational-fragment-v1";


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
            continue;
        }
        if (character === "}") {
            depth -= 1;
            if (depth === 0) {
                return source.slice(start, index + 1);
            }
        }
    }
    assert.fail(`${label} closing brace was not found.`);
}


function createExcavatorRefreshRuntime() {
    let currentShell = null;
    let requestCount = 0;
    let replacementCount = 0;
    const shiftPendingButton = {
        matches(selector) {
            return selector === "[data-eo-shift-button]";
        },
    };
    const shiftScreen = {
        dataset: {eoShiftDirty: "false"},
        querySelector() {
            return null;
        },
    };
    const faceScreen = {
        dataset: {eoFaceDirty: "false", eoFacePending: "false"},
    };
    const oldShell = {
        dataset: {eoActiveTab: "shift"},
        contains() {
            return false;
        },
        querySelector(selector) {
            if (selector === '[data-eo-screen="shift"]') return shiftScreen;
            if (selector === '[data-eo-screen="face"]') return faceScreen;
            return null;
        },
        replaceWith(replacement) {
            replacementCount += 1;
            currentShell = replacement;
        },
    };
    const newShell = {dataset: {eoActiveTab: "shift"}};
    currentShell = oldShell;
    const document = {
        activeElement: null,
        getElementById() {
            return null;
        },
        querySelector(selector) {
            if (selector === "[data-eo-shell]") return currentShell;
            return null;
        },
        querySelectorAll(selector) {
            assert.equal(selector, ".mobile-shift__action.is-pending");
            return [shiftPendingButton];
        },
    };
    const window = {
        AppOperationalFragment: {
            request(screen) {
                assert.equal(screen, "excavator");
                requestCount += 1;
                return Promise.resolve({html: "<main></main>"});
            },
            parseRoot() {
                return newShell;
            },
        },
        bindMobileShiftScreens() {},
        initExcavatorWorkShell() {},
    };
    const source = [
        "var excavatorWorkMutationGeneration = 0;",
        "function readExcavatorAssignmentSnapshot() { return {}; }",
        "function syncExcavatorAssignmentSnapshot() {}",
        "function scheduleExcavatorViewportHeightSync() {}",
        extractBraceBlock(
            EXCAVATOR_TEMPLATE_SOURCE,
            "function isExcavatorRefreshUnsafe(options)",
            "Excavator refresh safety predicate"
        ),
        extractBraceBlock(
            EXCAVATOR_TEMPLATE_SOURCE,
            "function refreshExcavatorWorkFromServer(options)",
            "Excavator fragment refresh"
        ),
        "context.refresh = refreshExcavatorWorkFromServer;",
    ].join("\n");
    const context = {context: {}, document, window, Promise, Array};
    vm.runInNewContext(source, context, {
        filename: "templates/trips/excavator_work.html#shift-fragment-refresh",
    });
    return {
        refresh: context.context.refresh,
        requestCount() {
            return requestCount;
        },
        replacementCount() {
            return replacementCount;
        },
    };
}


function extractExcavatorShiftSuccessHandler() {
    const marker = '}).then(function () {\n            playExcavatorSound(action === "close" ? "shift_end" : "shift_start");';
    const start = EXCAVATOR_TEMPLATE_SOURCE.indexOf(marker);
    assert.notEqual(start, -1, "Excavator Shift success handler was not found.");
    return extractBraceBlock(
        EXCAVATOR_TEMPLATE_SOURCE,
        "function ()",
        "Excavator Shift success handler",
        start
    );
}


function createFragmentRuntime(fetchImplementation) {
    const parseCalls = [];
    const parsedRoot = {nodeType: 1, dataset: {source: "fragment"}};
    const fakeDocument = {
        createElement(tagName) {
            assert.equal(tagName, "template");
            let source = "";
            return {
                set innerHTML(value) {
                    source = String(value);
                },
                get innerHTML() {
                    return source;
                },
                content: {
                    querySelectorAll(selector) {
                        parseCalls.push({html: source, selector});
                        return (
                            source === '<section data-test-root="1"></section>' &&
                            selector === "[data-test-root]"
                        ) ? [parsedRoot] : [];
                    },
                },
            };
        },
    };

    const runtimeWindow = {
        location: {
            href: "http://driver.localhost/driver/shift/?tab=work",
            origin: "http://driver.localhost",
        },
        fetch: fetchImplementation,
        setTimeout,
        clearTimeout,
        AbortController,
    };
    runtimeWindow.window = runtimeWindow;
    const context = {
        window: runtimeWindow,
        fetch: fetchImplementation,
        URL,
        document: fakeDocument,
        Promise,
        Error,
        TypeError,
        setTimeout,
        clearTimeout,
        AbortController,
    };
    const helperSource = extractMarkedSource(
        BASE_TEMPLATE_SOURCE,
        FRAGMENT_CLIENT_START,
        FRAGMENT_CLIENT_END,
        "Operational fragment client"
    );
    vm.runInNewContext(helperSource, context, {
        filename: "templates/base.html#operational-fragment-client",
    });
    assert.equal(
        typeof runtimeWindow.AppOperationalFragment,
        "object",
        "Production helper must publish window.AppOperationalFragment."
    );
    return {runtimeWindow, parseCalls, parsedRoot};
}


function assertNarrowFragmentHandler(source, signature, label, expectedScreen) {
    const handler = extractBraceBlock(source, signature, label);
    assert.match(
        handler,
        /AppOperationalFragment\s*\.\s*request\s*\(/,
        `${label} must request the production JSON fragment contract.`
    );
    assert.match(
        handler,
        new RegExp(`["']${expectedScreen}["']`),
        `${label} must request its own screen fragment.`
    );
    assert.match(
        handler,
        /AppOperationalFragment\s*\.\s*parseRoot\s*\(/,
        `${label} must parse only the returned fragment root.`
    );
    assert.doesNotMatch(
        handler,
        /\bfetch\s*\(/,
        `${label} must not issue a direct full-page GET.`
    );
    assert.doesNotMatch(
        handler,
        /response\s*\.\s*text\s*\(/,
        `${label} must not consume a full HTML response.`
    );
    assert.doesNotMatch(
        handler,
        /\bDOMParser\s*\(/,
        `${label} must use the shared narrow-fragment parser.`
    );
}


test("production fragment helper sends a versioned JSON-only request", async () => {
    const fetchCalls = [];
    let jsonCalls = 0;
    let textCalls = 0;
    const payload = {
        contract: FRAGMENT_CONTRACT,
        screen: "driver",
        version: 73,
        html: '<main data-driver-shell="1"></main>',
    };
    const {runtimeWindow} = createFragmentRuntime((url, options) => {
        fetchCalls.push({url, options});
        return Promise.resolve({
            ok: true,
            status: 200,
            json() {
                jsonCalls += 1;
                return Promise.resolve(payload);
            },
            text() {
                textCalls += 1;
                return Promise.resolve("<!doctype html><html></html>");
            },
        });
    });

    const result = await runtimeWindow.AppOperationalFragment.request("driver", 73);

    assert.equal(result, payload);
    assert.equal(fetchCalls.length, 1);
    const requestUrl = new URL(fetchCalls[0].url);
    assert.equal(requestUrl.pathname, "/driver/shift/");
    assert.equal(requestUrl.searchParams.get("tab"), "work");
    assert.equal(requestUrl.searchParams.get("_operational_fragment"), "driver");
    assert.equal(requestUrl.searchParams.get("_operational_version"), "73");
    assert.equal(fetchCalls[0].options.method, "GET");
    assert.equal(fetchCalls[0].options.credentials, "same-origin");
    assert.equal(fetchCalls[0].options.headers.Accept, "application/json");
    assert.equal(jsonCalls, 1);
    assert.equal(textCalls, 0);
});


test("production fragment helper rejects contract and screen mismatches", async () => {
    const responses = [
        {
            contract: "unexpected-contract",
            screen: "driver",
            version: 74,
            html: "<main></main>",
        },
        {
            contract: FRAGMENT_CONTRACT,
            screen: "dispatcher",
            version: 75,
            html: "<main></main>",
        },
    ];
    let fetchIndex = 0;
    const {runtimeWindow} = createFragmentRuntime(() => Promise.resolve({
        ok: true,
        status: 200,
        json() {
            const payload = responses[fetchIndex];
            fetchIndex += 1;
            return Promise.resolve(payload);
        },
    }));

    await assert.rejects(
        runtimeWindow.AppOperationalFragment.request("driver", 74),
        /contract/i
    );
    await assert.rejects(
        runtimeWindow.AppOperationalFragment.request("driver", 75),
        /contract|screen|mismatch/i
    );
});


test("production fragment helper times out a hung request and remains reusable", async () => {
    let requestCount = 0;
    let firstRequestAborted = false;
    const recoveredPayload = {
        contract: FRAGMENT_CONTRACT,
        screen: "excavator",
        version: 77,
        html: '<main data-eo-shell="1"></main>',
    };
    const {runtimeWindow} = createFragmentRuntime((url, options) => {
        requestCount += 1;
        if (requestCount === 1) {
            options.signal.addEventListener("abort", () => {
                firstRequestAborted = true;
            });
            return new Promise(() => {});
        }
        return Promise.resolve({
            ok: true,
            status: 200,
            json() {
                return Promise.resolve(recoveredPayload);
            },
        });
    });

    await assert.rejects(
        runtimeWindow.AppOperationalFragment.request(
            "excavator",
            76,
            {timeoutMs: 10}
        ),
        (error) => error && error.code === "OPERATIONAL_FRAGMENT_TIMEOUT"
    );
    assert.equal(firstRequestAborted, true);

    const recovered = await runtimeWindow.AppOperationalFragment.request(
        "excavator",
        77,
        {timeoutMs: 100}
    );
    assert.equal(recovered, recoveredPayload);
    assert.equal(requestCount, 2);
});


test("production fragment helper parses only the requested root", () => {
    const {runtimeWindow, parseCalls, parsedRoot} = createFragmentRuntime(() => {
        throw new Error("parseRoot must not perform a request");
    });

    const result = runtimeWindow.AppOperationalFragment.parseRoot(
        '<section data-test-root="1"></section>',
        "[data-test-root]"
    );

    assert.equal(result, parsedRoot);
    assert.deepEqual(parseCalls, [{
        html: '<section data-test-root="1"></section>',
        selector: "[data-test-root]",
    }]);
});


test("Excavator fragment refresh admits only its scoped pending Shift owner", async () => {
    const runtime = createExcavatorRefreshRuntime();

    const blocked = await runtime.refresh({preserveTab: true});
    assert.equal(blocked, false);
    assert.equal(runtime.requestCount(), 0);
    assert.equal(runtime.replacementCount(), 0);

    const applied = await runtime.refresh({preserveTab: true, pendingOwner: "shift"});
    assert.equal(applied, true);
    assert.equal(runtime.requestCount(), 1);
    assert.equal(runtime.replacementCount(), 1);
});


test("successful Excavator Shift save clears its draft before owned fragment reconciliation", async () => {
    const refreshCalls = [];
    let clearCalls = 0;
    let reloadCalls = 0;
    const shiftScreen = {dataset: {eoShiftDirty: "true"}};
    const sandbox = {
        shiftPendingActionKey: "pending-key",
        shiftPendingActionId: "pending-id",
        shiftScreen,
        clearShiftErrors() {
            clearCalls += 1;
        },
        playExcavatorSound() {
            return Promise.resolve(true);
        },
        action: "open",
        refreshExcavatorWorkFromServer(options) {
            refreshCalls.push(options);
            return Promise.resolve(true);
        },
        window: {
            AppRealtime: null,
            location: {
                reload() {
                    reloadCalls += 1;
                },
            },
        },
    };
    vm.runInNewContext(
        `context.success = ${extractExcavatorShiftSuccessHandler()};`,
        {...sandbox, context: sandbox},
        {filename: "templates/trips/excavator_work.html#shift-save-success"}
    );

    await sandbox.success();

    assert.equal(shiftScreen.dataset.eoShiftDirty, "false");
    assert.equal(clearCalls, 1);
    assert.equal(refreshCalls.length, 1);
    assert.equal(refreshCalls[0].pendingOwner, "shift");
    assert.equal(reloadCalls, 0);
});


test("four production refresh handlers use narrow fragments, never a full HTML GET", () => {
    assertNarrowFragmentHandler(
        DRIVER_TEMPLATE_SOURCE,
        "window.applyOperationalStateRefresh = function (context)",
        "Driver operational refresh",
        "driver"
    );
    assertNarrowFragmentHandler(
        EXCAVATOR_TEMPLATE_SOURCE,
        "function refreshExcavatorWorkFromServer(options)",
        "Excavator operational refresh",
        "excavator"
    );
    assertNarrowFragmentHandler(
        DISPATCHER_TEMPLATE_SOURCE,
        "function refreshDispatcherDesktopBoardFromServer(",
        "Dispatcher operational refresh",
        "dispatcher"
    );
    assertNarrowFragmentHandler(
        DISPATCHER_TEMPLATE_SOURCE,
        "function refreshMobileBoardFromServer(options)",
        "Mining Master operational refresh",
        "mining_master"
    );
});


test("Mining Master delegates state polling exclusively to the shared realtime client", () => {
    assert.equal(
        /\brunMiningMasterPassiveReconcile\b/.test(DISPATCHER_TEMPLATE_SOURCE),
        false,
        "The screen must not keep a second forced-poll entry point beside AppRealtime."
    );
    assert.equal(
        /\bminingMasterMobilePassiveReconcileMs\b/.test(DISPATCHER_TEMPLATE_SOURCE),
        false,
        "The removed ten-second polling cadence must not be reintroduced."
    );
    assert.equal(
        /AppRealtime\s*\.\s*poll\s*\(\s*\{\s*force\s*:\s*true\s*\}\s*\)/
            .test(DISPATCHER_TEMPLATE_SOURCE),
        false,
        "Focus, visibility, online and connection recovery must use shared AppRealtime scheduling."
    );
});
