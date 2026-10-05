"use strict";
// Настоящие обработчики экрана + настоящий транспорт, включая журнал/HTTP.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const backend = path.resolve(__dirname, "../../..");
const template = fs.readFileSync(path.join(backend, "templates/trips/dispatcher_control.html"), "utf8");
const transportCode = fs.readFileSync(path.join(backend, "static/js/dispatcher-transport-v1.js"), "utf8");
const handlers = [
    ["activateMobileGarageExcavator", "bindMobileGarageExcavatorSwipe"],
    ["executeMobileComplexToGarage", "animateMobileComplexTrucksToGarage"],
    ["executeMobileComplexTrucksRelease", "addMobileFillAssignedTruck"],
].map(([start, end]) => {
    const first = template.indexOf("        function " + start + "(");
    const last = template.indexOf("        function " + end + "(", first);
    assert.ok(first >= 0 && last > first);
    return template.slice(first, last);
}).join("\n");

class Node {
    constructor(dataset = {}) {
        this.dataset = dataset;
        this.children = [];
        this.hidden = false;
        this.disabled = false;
        this.parentNode = null;
        this.style = {display: "", setProperty() {}, removeProperty() {}};
        const classes = new Set();
        this.classList = {add: (...names) => names.forEach(n => classes.add(n)),
            remove: (...names) => names.forEach(n => classes.delete(n)), contains: name => classes.has(name)};
    }
    get nextSibling() {
        return this.parentNode && this.parentNode.children[this.parentNode.children.indexOf(this) + 1] || null;
    }
    appendChild(node) { node.remove(); this.children.push(node); node.parentNode = this; }
    insertBefore(node, next) {
        node.remove(); this.children.splice(this.children.indexOf(next), 0, node); node.parentNode = this;
    }
    remove() {
        if (this.parentNode) this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1);
        this.parentNode = null;
    }
    querySelector() { return null; }
    querySelectorAll() { return this.children.slice(); }
}

function runtime(options = {}) {
    const storage = new Map(Object.entries(options.storage || {})), events = [], timers = new Map(), errors = [];
    let timerId = 0;
    const grid = new Node(), preview = new Node(), card = new Node({mmMobileExcavatorId: "9"});
    const button = new Node({mmMobileActivateExcavator: "9"});
    const trucks = [new Node({mmMobileHomeTruckId: "7"}), new Node({mmMobileHomeTruckId: "8"})];
    trucks.forEach(truck => preview.appendChild(truck)); grid.appendChild(card);
    card.querySelector = selector => selector === ".mm-mobile-truck-preview" ? preview : null;
    const shell = new Node();
    shell.querySelector = selector => selector === ".mm-mobile-complex-grid" ? grid : null;
    const garageTrucks = new Set();
    let createdCard = null, garageButton = null;
    const prefix = "mining-master-mobile-sync-queue-v3:command:";
    function confirmedEffect(name) {
        // Каждый предметный жест обязан уже иметь исходник, даже до HTTP.
        const records = [...storage].filter(([key]) => key.startsWith(prefix));
        assert.equal(records.length, 1, name + " precedes durable storage");
        events.push(name);
    }
    const context = {
        Promise, Date, Math, JSON, Object, Array, String, Error, CSS: {escape: v => v}, shell,
        isMiningMasterMobileReadonly: () => Boolean(options.readonly),
        isAppRoleReadonly: () => Boolean(options.readonly),
        dispatcherMoveExcavatorUrl: "/mining-master/assignments/excavator/move/",
        dispatcherAssignTruckUrl: "/mining-master/assignments/truck/assign/",
        collectComplexAssignmentStates: () => ({7: 11, 8: 12}),
        localStorage: {
            get length() { return storage.size; }, key: i => [...storage.keys()][i] || null,
            getItem: key => storage.get(key) || null,
            setItem(key, value) {
                if (options.quota) throw Error("quota");
                storage.set(key, String(value));
                if (key.startsWith(prefix)) events.push("saved:" + JSON.parse(value).delivery.state);
            },
        },
        setTimeout(fn, delay) { const id = ++timerId; timers.set(id, {fn, delay}); return id; },
        clearTimeout: id => timers.delete(id),
        fetch: async (url, init) => {
            events.push("http");
            if (options.fetch) return options.fetch(url, init);
            return {ok: true, status: 200, json: async () => ({ok: true})};
        },
        sessionStorage: {removeItem() {}, setItem() {}},
        requestAnimationFrame: fn => fn(),
        showDispatcherDnDError: error => errors.push(error),
        clearMobileGarageExcavatorWorkZones() {},
        resetMobileGarageExcavatorSwipe() {},
        resetMobileComplexSwipe: () => shell.classList.remove("is-excavator-garage-peeking"),
        resetMobileComplexTruckSwipe: node => {
            node.classList.remove("is-releasing-trucks"); shell.classList.remove("is-truck-garage-peeking");
        },
        createMobileHomeComplexFromGarage: () => {
            confirmedEffect("createComplex"); createdCard = new Node(); grid.appendChild(createdCard); return createdCard;
        },
        hideMobileGarageExcavatorButton: node => { confirmedEffect("hideExcavator"); node.hidden = true; },
        animateMobileComplexTrucksToGarage: () => { confirmedEffect("animateTrucks"); return 720; },
        restoreMobileTruckToGarages: node => { confirmedEffect("garageTruck"); garageTrucks.add(node.dataset.mmMobileHomeTruckId); },
        removeMobileTruckFromGarages: id => garageTrucks.delete(id),
        createMobileGarageExcavatorFromHome: () => {
            confirmedEffect("garageExcavator"); garageButton = new Node(); grid.appendChild(garageButton);
            return {created: true, button: garageButton};
        },
        closeDetails: () => events.push("close"),
        bindMobileComplexCardInteractions() {}, bindMobileHomeTruckDrag() {},
        updateMobileHomeComplexGridLayout() {}, updateMobileExcavatorGarageGridLayout() {},
        scheduleMobileLocalLayoutReconcile() {}, syncMobileGarageNavState() {},
        ensureMobileTruckPreviewEmpty() {}, updateMobileHomeTruckPreviewLayout() {},
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(transportCode, context);
    const transport = context.createDispatcherTransport({getCommandContext: () => ({
        actor_id: "1", access_id: "2", role: "mining_master", shift_id: "3",
    })});
    if (options.oldTransport) delete transport.storePost;
    context.dispatcherTransport = transport;
    vm.runInContext(handlers, context);
    function invoke(action) {
        return action === "activate" ? context.activateMobileGarageExcavator(button, 0, "home")
            : action === "disband" ? context.executeMobileComplexToGarage(card)
                : context.executeMobileComplexTrucksRelease(card);
    }
    function runUiTimers() {
        for (const [id, timer] of [...timers]) {
            if ([120, 180, 260, 880].includes(timer.delay)) { timers.delete(id); timer.fn(); }
        }
    }
    return {invoke, events, storage, transport, errors, timers, runUiTimers, card, button, preview, trucks, garageTrucks,
        get createdCard() { return createdCard; }, get garageButton() { return garageButton; }};
}

for (const action of ["activate", "disband", "release"]) {
    test(action + ": quota does not confirm, move cards, start a timer or send HTTP", async () => {
        const r = runtime({quota: true});
        assert.equal(await r.invoke(action), false);
        assert.deepEqual(r.events, []);
        assert.equal(r.storage.size, 0);
        assert.equal(r.timers.size, 0);
        assert.equal(r.errors[0].code, "storage_unavailable");
        assert.equal(r.button.hidden, false);
        assert.equal(r.card.hidden, false);
        assert.deepEqual(r.preview.children, r.trucks);
    });

    test(action + ": original is durable before every local effect and ACK preserves it", async () => {
        const r = runtime();
        const sending = r.invoke(action);
        const saved = [...r.storage.entries()].find(([key]) => key.startsWith(r.transport.journalPrefix));
        const original = JSON.parse(saved[1]).request;
        assert.equal(original.autoRetry, false);
        assert.deepEqual(original.author, {actor_id: "1", access_id: "2", role: "mining_master", shift_id: "3"});
        assert.equal(r.events[0], "saved:pending");
        await sending;
        const acknowledged = JSON.parse(r.storage.get(saved[0]));
        assert.deepEqual(acknowledged.request, original);
        assert.equal(acknowledged.delivery.state, "acknowledged");
        assert.equal(r.events.filter(e => e === "http").length, 1);
        assert.deepEqual(r.errors, []);
    });

    test(action + ": quick 409 rollback survives late animation timers and preserves the command", async () => {
        const r = runtime({fetch: async () => ({ok: false, status: 409, json: async () => ({ok: false, code: "state_conflict"})})});
        assert.equal(await r.invoke(action), false);
        r.runUiTimers();
        assert.equal(r.card.hidden, false);
        assert.equal(r.button.hidden, false);
        assert.equal(r.button.disabled, false);
        assert.deepEqual(r.preview.children, r.trucks);
        assert.equal(r.garageTrucks.size, 0);
        assert.equal(r.createdCard && r.createdCard.parentNode, null);
        assert.equal(r.garageButton && r.garageButton.parentNode, null);
        assert.equal(r.events.includes("close"), false, "old timer cannot close the screen after rollback");
        const record = [...r.storage.entries()].find(([key]) => key.startsWith(r.transport.journalPrefix));
        assert.equal(JSON.parse(record[1]).delivery.state, "held");
        assert.equal(r.errors[0].code, "state_conflict");
    });

    test(action + ": hanging response rolls back after the deadline with the original still recoverable", async () => {
        const r = runtime({fetch: async () => ({ok: true, json: () => new Promise(() => {})})});
        const pending = r.invoke(action);
        await new Promise(resolve => setImmediate(resolve));
        r.runUiTimers();
        const deadline = [...r.timers.values()].find(timer => timer.delay === 12000);
        assert.ok(deadline);
        deadline.fn();
        assert.equal(await pending, false);
        assert.equal(r.card.hidden, false);
        assert.equal(r.button.hidden, false);
        assert.deepEqual(r.preview.children, r.trucks);
        assert.equal(r.transport.getQueueState().pendingCount, 0);
        const record = [...r.storage.entries()].find(([key]) => key.startsWith(r.transport.journalPrefix));
        assert.equal(JSON.parse(record[1]).delivery.state, "held");
        assert.equal(r.errors[0].code, "request_timeout");
    });
}

test("corrupt old queue, inactive access and old transport cannot produce a structural confirmation", async () => {
    for (const options of [{storage: {"mining-master-mobile-sync-queue-v3": "broken"}}, {readonly: true}, {oldTransport: true}]) {
        const r = runtime(options);
        for (const action of ["activate", "disband", "release"]) await r.invoke(action);
        assert.deepEqual(r.events, []);
        assert.equal(r.timers.size, 0);
        if (!options.readonly) {
            assert.equal(r.errors.length, 3);
            assert.ok(r.errors.every(error => error.code === "storage_unavailable"));
        }
        if (options.storage) assert.equal(r.storage.get("mining-master-mobile-sync-queue-v3"), "broken");
    }
});

test("process loss between save and HTTP preserves original without unsafe structural replay", async () => {
    const r = runtime();
    const command = r.transport.storePost("/mining-master/assignments/excavator/move/", {
        client_action_id: "staged", excavator_id: "9", zone: "active", expected_zone: "inactive",
    }, {queueOnNetworkFailure: false});
    assert.deepEqual(r.events, ["saved:pending"]);
    const original = r.storage.get(r.transport.journalPrefix + command.id);
    const next = runtime({storage: Object.fromEntries(r.storage), fetch: async (url) => {
        assert.equal(url, "/assignments/commands/receipt/");
        return {ok: true, json: async () => ({ok: true, status: "unresolved"})};
    }});
    await next.transport.flush();
    assert.equal(next.storage.get(next.transport.journalPrefix + command.id), original);
});
