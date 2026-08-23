"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SOURCE = fs.readFileSync(
    path.resolve(__dirname, "..", "dispatcher-control-v1.js"),
    "utf8"
);

function extractBraceBlock(source, signature) {
    const start = source.indexOf(signature);
    assert.notEqual(start, -1, `${signature} was not found`);
    const open = source.indexOf("{", start + signature.length);
    let depth = 0;
    let quote = "";
    let escaped = false;
    for (let index = open; index < source.length; index += 1) {
        const character = source[index];
        if (quote) {
            if (escaped) escaped = false;
            else if (character === "\\") escaped = true;
            else if (character === quote) quote = "";
            continue;
        }
        if (character === "'" || character === '"' || character === "`") {
            quote = character;
            continue;
        }
        if (character === "{") depth += 1;
        if (character === "}") {
            depth -= 1;
            if (depth === 0) return source.slice(start, index + 1);
        }
    }
    assert.fail(`${signature} has no closing brace`);
}

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((accept, decline) => {
        resolve = accept;
        reject = decline;
    });
    return {promise, resolve, reject};
}

function responseFor(cardKey, card = {}) {
    return {
        ok: true,
        status: 200,
        json() {
            return Promise.resolve({
                contract: "dispatcher-equipment-detail-v1",
                card_key: cardKey,
                operational_state_version: 41,
                card,
            });
        },
    };
}

function trigger(equipmentId, drag = "truck") {
    return {
        dataset: {
            equipmentId: String(equipmentId),
            dispatcherDrag: drag,
            equipmentName: `Техника ${equipmentId}`,
        },
    };
}

function createRuntime(fetchImplementation) {
    const fetchCalls = [];
    const rendered = [];
    const loadStates = [];
    let wakeCount = 0;
    const detailLayer = {
        hidden: true,
        dataset: {},
        attributes: new Map(),
        setAttribute(name, value) {
            this.attributes.set(name, String(value));
        },
        removeAttribute(name) {
            this.attributes.delete(name);
        },
        querySelector() {
            return null;
        },
    };
    const textNode = () => ({textContent: ""});
    const context = {
        AbortController,
        CSS: {escape(value) { return String(value); }},
        Promise,
        console,
        document: {querySelector() { return null; }},
        fetch(url, options) {
            fetchCalls.push({url, options});
            return fetchImplementation(url, options, fetchCalls.length - 1);
        },
        window: {
            AppRealtime: {
                getDebugState() { return {pendingVersion: 0}; },
                wake() { wakeCount += 1; },
            },
        },
        detailLayer,
        detailType: textNode(),
        detailTitle: textNode(),
        detailStatus: textNode(),
        detailZone: textNode(),
        fetchCalls,
        rendered,
        loadStates,
    };
    vm.runInNewContext(
        `
        var runtimeConfig = {
            dispatcherDetailUrlTemplate: "/dispatcher/control/card/equipment/0/"
        };
        var detailRequestToken = 0;
        var detailRequestController = null;
        var detailRetryAction = null;
        var detailRetry = null;
        var equipmentCards = {};
        function currentDispatcherBoardVersion() { return 41; }
        function resetDetailContent() {}
        function renderDetailGarageIcon() {}
        function setDetailLoadState(message, retry) {
            loadStates.push({message: message, retry: retry});
        }
        function renderEquipmentCard(cardKey, card) {
            rendered.push({cardKey: cardKey, card: card});
            detailLayer.removeAttribute("aria-busy");
        }
        ${extractBraceBlock(SOURCE, "function dispatcherDetailUrl(trigger, boardVersion)")}
        ${extractBraceBlock(SOURCE, "function detailErrorMessage(status)")}
        ${extractBraceBlock(SOURCE, "function openEquipmentCard(cardId, trigger)")}
        ${extractBraceBlock(SOURCE, "function closeEquipmentCard()")}
        `,
        context,
        {filename: "static/js/dispatcher-control-v1.js#detail-runtime"}
    );
    return {
        context,
        detailLayer,
        fetchCalls,
        rendered,
        loadStates,
        wakeCount() { return wakeCount; },
    };
}

async function settle() {
    await Promise.resolve();
    await Promise.resolve();
    await new Promise((resolve) => setImmediate(resolve));
}

test("one Dispatcher card action performs one protected detail GET", async () => {
    const runtime = createRuntime(() => Promise.resolve(responseFor("12", {number: "12"})));

    assert.equal(runtime.context.openEquipmentCard("12", trigger(12)), true);
    await settle();

    assert.equal(runtime.fetchCalls.length, 1);
    assert.equal(
        runtime.fetchCalls[0].url,
        "/dispatcher/control/card/equipment/12/?state_version=41"
    );
    assert.equal(runtime.fetchCalls[0].options.method, "GET");
    assert.equal(runtime.fetchCalls[0].options.credentials, "same-origin");
    assert.equal(runtime.fetchCalls[0].options.cache, "no-store");
    assert.equal(runtime.fetchCalls[0].options.headers.Accept, "application/json");
    assert.equal(runtime.fetchCalls[0].options.headers["X-Requested-With"], "XMLHttpRequest");
    assert.equal(JSON.stringify(runtime.rendered), JSON.stringify([
        {cardKey: "12", card: {number: "12"}},
    ]));
});

test("a newer card request aborts and cannot be overwritten by an older response", async () => {
    const first = deferred();
    const second = deferred();
    const runtime = createRuntime((url, options, index) => (
        index === 0 ? first.promise : second.promise
    ));

    runtime.context.openEquipmentCard("12", trigger(12));
    const firstSignal = runtime.fetchCalls[0].options.signal;
    runtime.context.openEquipmentCard("13", trigger(13));
    assert.equal(firstSignal.aborted, true);

    second.resolve(responseFor("13", {number: "13"}));
    await settle();
    first.resolve(responseFor("12", {number: "12"}));
    await settle();

    assert.equal(JSON.stringify(runtime.rendered), JSON.stringify([
        {cardKey: "13", card: {number: "13"}},
    ]));
});

test("closing the card aborts its request and wakes queued realtime", () => {
    const pending = deferred();
    const runtime = createRuntime(() => pending.promise);

    runtime.context.openEquipmentCard("12", trigger(12));
    const signal = runtime.fetchCalls[0].options.signal;
    runtime.context.closeEquipmentCard();

    assert.equal(signal.aborted, true);
    assert.equal(runtime.detailLayer.hidden, true);
    assert.equal(runtime.wakeCount(), 1);
});
