"use strict";

const test = require("node:test");
const {driverScreenSource} = require("./driver-screen-source");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const {
    createDriverFreeBucketController,
    catalogIsStale,
    displaySnapshot,
    isAuthoritativeCatalog,
    normalizeCatalog,
    resolveInstalledState,
    tileStatusLabel,
} = require("../driver-free-bucket-v1.js");

const driverShiftRuntime = fs.readFileSync(path.resolve(__dirname, "../driver-shift-v1.js"), "utf8");

function functionSource(source, name) {
    const start = source.indexOf("function " + name + "(");
    assert.notEqual(start, -1, name + " must exist");
    const bodyStart = source.indexOf("{", start);
    let depth = 0;
    for (let index = bodyStart; index < source.length; index += 1) {
        if (source[index] === "{") depth += 1;
        if (source[index] === "}") depth -= 1;
        if (depth === 0) return source.slice(start, index + 1);
    }
    throw new Error("function_not_closed");
}

function item(overrides) {
    return Object.assign({
        id: 22,
        label: "EX-22",
        complex_label: "K-22",
        is_primary: false,
        available: true,
        loading_horizon: "H-1",
        loading_block: "B-2",
        rock_type_id: 7,
        rock_type: "Rock",
        dump_points: [
            {id: 8, name: "North", transport_distance_km: "1.2"},
            {id: 9, name: "South", transport_distance_km: "2.4"},
        ],
        missing_fields: [],
    }, overrides || {});
}

function shell() {
    return {
        dataset: {
            driverAccessId: "3",
            driverAuthGeneration: "5",
            driverShiftId: "11",
            driverCurrentTruckId: "17",
            driverFreeBucketEnabled: "true",
        },
        querySelector() { return null; },
    };
}

function serverCatalog(overrides) {
    return Object.assign({
        schema: "driver-free-bucket-catalog-v1",
        complete: true,
        stale: false,
        version: 12,
        generated_at: "2026-09-14T03:00:00Z",
        excavators: [item()],
    }, overrides || {});
}

function storage() {
    const values = new Map();
    return {
        getItem(key) { return values.has(key) ? values.get(key) : null; },
        setItem(key, value) { values.set(key, value); },
        removeItem(key) { values.delete(key); },
    };
}

function eventTarget(initial) {
    const listeners = new Map();
    return Object.assign({
        addEventListener(type, callback) {
            if (!listeners.has(type)) listeners.set(type, new Set());
            listeners.get(type).add(callback);
        },
        removeEventListener(type, callback) {
            if (listeners.has(type)) listeners.get(type).delete(callback);
        },
        emit(type, event) {
            for (const callback of listeners.get(type) || []) callback(event || {type});
        },
    }, initial || {});
}

function selectedEvent(occurredAt, overrides) {
    return Object.assign({
        event_id: "driver-free-bucket-local-1",
        event_type: "driver.free_bucket.selected",
        occurred_at: new Date(occurredAt).toISOString(),
        sequence: 1,
        state: "pending",
        payload: {
            truck_id: 17,
            excavator_id: 22,
            catalog_version: 12,
            catalog_generated_at: "2026-09-27T00:00:00Z",
        },
        context_snapshot: displaySnapshot(item()),
    }, overrides || {});
}

test("catalog remains complete while per-item availability reports missing settings", () => {
    const catalog = normalizeCatalog({
        complete: true,
        excavators: [item({available: false, missing_fields: ["dump_points"]})],
    });
    assert.equal(catalog.complete, true);
    assert.equal(catalog.excavators[0].available, false);
    assert.deepEqual(catalog.excavators[0].missing_fields, ["dump_points"]);
});

test("display snapshot uses canonical server keys and keeps every dump point", () => {
    const snapshot = displaySnapshot(item());
    assert.equal(snapshot.rock_type_name, "Rock");
    assert.equal(snapshot.loading_horizon, "H-1");
    assert.equal(snapshot.loading_block, "B-2");
    assert.deepEqual(snapshot.dump_points.map((point) => point.id), [8, 9]);
    assert.equal(Object.hasOwn(snapshot, "rock_type"), false);
});

test("cached inactive shell keeps a newer durable local selection on restart", () => {
    const installed = resolveInstalledState(
        {active: false, version: 12, generated_at: "2026-09-14T03:00:00Z"},
        {
            active: true,
            version: 12,
            generated_at: "2026-09-14T03:01:00Z",
            sync_mode: "local",
            selection: item(),
        },
    );
    assert.equal(installed.active, true);
    assert.equal(installed.sync_mode, "local");
});

test("newer confirmed inactive server snapshot clears saved selection", () => {
    const installed = resolveInstalledState(
        {active: false, version: 13, generated_at: "2026-09-14T03:02:00Z"},
        {
            active: true,
            version: 12,
            generated_at: "2026-09-14T03:01:00Z",
            sync_mode: "review",
            selection: item(),
        },
    );
    assert.equal(installed.active, false);
});

test("rejected local attempt never outlives a fresh inactive server snapshot, even if it looks newer", () => {
    // Живой случай: телефон сохранил выбор, сервер его отклонил (sync_mode
    // "review"), и с тех пор ни разу не было confirmed-снимка НОВЕЕ этой
    // отклонённой попытки — сервер стабильно отдаёт то же старое "ничего не
    // активно". Раньше это застревало навсегда: ни выбрать другой экскаватор
    // (select() не пускает, пока state.active), ни отменить то, чего сервер
    // не подтверждает. "review" значит "сервер уже отказал" — этого одного
    // достаточно, чтобы больше не доверять локальной копии, независимо от
    // version/generated_at.
    const installed = resolveInstalledState(
        {active: false, version: 12, generated_at: "2026-09-14T03:00:00Z"},
        {
            active: true,
            version: 12,
            generated_at: "2026-09-14T03:01:00Z",
            sync_mode: "review",
            selection: item(),
        },
    );
    assert.equal(installed.active, false);
});

test("cached HTML catalog is marked stale while the device is offline", () => {
    const localStorage = storage();
    const controller = createDriverFreeBucketController({
        shell: shell(),
        storage: localStorage,
        window: {localStorage, navigator: {onLine: false}},
    });
    controller.installCatalog(serverCatalog());
    assert.equal(controller.catalog().stale, true);
    assert.equal(controller.catalog().complete, true);
});

test("primary excavator selection is rejected before enqueue", async () => {
    let enqueueCalls = 0;
    const controller = createDriverFreeBucketController({
        shell: shell(),
        storage: storage(),
        window: {localStorage: storage()},
        outbox: {
            enqueue() { enqueueCalls += 1; return Promise.resolve({event_id: "unexpected"}); },
        },
    });
    controller.installCatalog(serverCatalog({excavators: [item({is_primary: true})]}));
    await assert.rejects(controller.select(controller.catalog().excavators[0]), /free_bucket_unavailable/);
    assert.equal(enqueueCalls, 0);
    assert.equal(controller.state().active, false);
});

test("selection becomes optimistic only after durable enqueue resolves", async () => {
    let release;
    const durable = new Promise((resolve) => { release = resolve; });
    const controller = createDriverFreeBucketController({
        shell: shell(),
        storage: storage(),
        window: {
            localStorage: storage(),
            createDriverFreeBucketSelectedEvent(options) {
                return {
                    event_id: "local-selection-1",
                    event_type: "driver.free_bucket.selected",
                    occurred_at: "2026-09-14T03:01:00Z",
                    payload: {truck_id: options.truckId, excavator_id: options.excavatorId},
                    context_snapshot: options.contextSnapshot,
                };
            },
        },
        now: Date.parse("2026-09-14T03:01:00Z"),
        outbox: {enqueue(event) { return durable.then(() => event); }},
    });
    controller.installCatalog(serverCatalog());
    const pending = controller.select(controller.catalog().excavators[0]);
    assert.equal(controller.state().active, false);
    release();
    await pending;
    assert.equal(controller.state().active, true);
    assert.equal(controller.state().catalog_version, 12);
    assert.equal(controller.state().catalog_generated_at, "2026-09-14T03:00:00Z");
});

test("local offline request expires exactly ten minutes after the tap", async () => {
    const tappedAt = Date.parse("2026-09-27T20:00:00Z");
    let now = tappedAt;
    let scheduled = null;
    const localStorage = storage();
    const event = selectedEvent(tappedAt);
    const windowObject = {
        localStorage,
        navigator: {onLine: false},
        createDriverFreeBucketSelectedEvent() { return event; },
        setTimeout(callback, delay) {
            scheduled = {callback, delay};
            return {unref() {}};
        },
        clearTimeout() {},
    };
    const controller = createDriverFreeBucketController({
        shell: shell(),
        storage: localStorage,
        window: windowObject,
        outbox: {enqueue(value) { return Promise.resolve(value); }},
        now: () => now,
    });
    controller.installCatalog(serverCatalog());

    await controller.select(controller.catalog().excavators[0]);
    assert.equal(controller.state().active, true);
    assert.equal(controller.state().expiry_basis, "local");

    now = tappedAt + 10 * 60 * 1000 - 1;
    assert.equal(controller.state().active, true);
    assert.equal(scheduled.delay, 1);
    now += 1;
    scheduled.callback();
    assert.equal(controller.state().active, false);
});

test("expired local request does not return after restart or repeated outbox projection", async () => {
    const tappedAt = Date.parse("2026-09-27T01:00:00Z");
    let now = tappedAt;
    const localStorage = storage();
    const event = selectedEvent(tappedAt);
    const first = createDriverFreeBucketController({
        shell: shell(),
        storage: localStorage,
        window: {
            localStorage,
            navigator: {onLine: false},
            createDriverFreeBucketSelectedEvent() { return event; },
        },
        outbox: {enqueue(value) { return Promise.resolve(value); }},
        now: () => now,
    });
    first.installCatalog(serverCatalog());
    await first.select(first.catalog().excavators[0]);
    assert.equal(first.state().active, true);
    first.destroy();

    now = tappedAt + 10 * 60 * 1000 + 1;
    const restarted = createDriverFreeBucketController({
        shell: shell(), storage: localStorage,
        window: {localStorage, navigator: {onLine: false}},
        now: () => now,
    });
    restarted.installCatalog(serverCatalog());
    restarted.installState({
        active: false,
        version: 11,
        generated_at: new Date(tappedAt - 1000).toISOString(),
    });
    assert.equal(restarted.state().active, false);
    restarted.installState({
        active: true,
        acceptance_id: 702,
        acceptance_local_id: event.event_id,
        status: "accepted",
        selection: item(),
        version: 12,
        generated_at: new Date(now).toISOString(),
    });
    assert.equal(restarted.state().active, true);
    assert.equal(restarted.project([event]).active, false);
    assert.equal(restarted.project([event]).active, false);
});

test("authoritative expiry uses server time instead of a skewed device clock", () => {
    const serverNow = Date.parse("2026-09-27T02:00:00Z");
    let deviceNow = serverNow + 20 * 60 * 1000;
    const localStorage = storage();
    const serverState = {
        active: true,
        acceptance_id: 701,
        acceptance_local_id: "server-request-701",
        status: "requested",
        can_cancel: true,
        selection: item(),
        sync_mode: "confirmed",
        version: 20,
        generated_at: new Date(serverNow).toISOString(),
        expires_at: new Date(serverNow + 5 * 60 * 1000).toISOString(),
    };
    const controller = createDriverFreeBucketController({
        shell: shell(), storage: localStorage,
        window: {localStorage, navigator: {onLine: true}},
        now: () => deviceNow,
    });
    controller.installState(serverState);
    const localDeadline = controller.state().expires_local_at_ms;
    assert.equal(controller.state().active, true);
    assert.equal(controller.state().expiry_basis, "server");
    assert.equal(localDeadline, deviceNow + 5 * 60 * 1000);

    deviceNow += 60 * 1000;
    controller.destroy();
    const restarted = createDriverFreeBucketController({
        shell: shell(), storage: localStorage,
        window: {localStorage, navigator: {onLine: false}},
        now: () => deviceNow,
    });
    restarted.installState(serverState);
    assert.equal(restarted.state().expires_local_at_ms, localDeadline);
    deviceNow = localDeadline - 1;
    assert.equal(restarted.state().active, true);
    deviceNow = localDeadline;
    assert.equal(restarted.state().active, false);
});

test("ten-minute request ttl never closes an already used free-bucket trip", () => {
    const serverNow = Date.parse("2026-09-27T02:00:00Z");
    const localStorage = storage();
    const controller = createDriverFreeBucketController({
        shell: shell(), storage: localStorage,
        window: {localStorage},
        now: serverNow + 30 * 60 * 1000,
    });
    controller.installState({
        active: true,
        acceptance_id: 703,
        status: "used",
        selection: item(),
        generated_at: new Date(serverNow).toISOString(),
        expires_at: new Date(serverNow + 10 * 60 * 1000).toISOString(),
    });
    assert.equal(controller.state().active, true);
    assert.equal(controller.state().status, "used");
    assert.equal(controller.state().expires_local_at_ms, 0);
});

test("manual free-bucket load cancellation restores the same acceptance only until its original deadline", () => {
    const tappedAt = Date.parse("2026-09-27T02:00:00Z");
    let now = tappedAt + 2 * 60 * 1000;
    const localStorage = storage();
    const controller = createDriverFreeBucketController({
        shell: shell(), storage: localStorage,
        window: {localStorage},
        now: () => now,
    });
    controller.installState({
        active: true,
        acceptance_id: 703,
        acceptance_local_id: "free-cycle-703",
        status: "used",
        selection: item(),
        generated_at: new Date(tappedAt).toISOString(),
    });
    const cancelled = {
        event_id: "driver-manual-cancel-free-703",
        event_type: "driver.trip.loaded.cancelled",
        occurred_at: new Date(now).toISOString(),
        sequence: 4,
        state: "pending",
        context_snapshot: Object.assign(displaySnapshot(item()), {
            authority_type: "free_bucket",
            free_bucket_acceptance_id: 703,
            free_bucket_acceptance_local_id: "free-cycle-703",
            free_bucket_expires_at: new Date(tappedAt + 10 * 60 * 1000).toISOString(),
            free_bucket_expires_local_at_ms: tappedAt + 10 * 60 * 1000,
        }),
    };

    let restored = controller.project([cancelled]);
    assert.equal(restored.active, true);
    assert.equal(restored.status, "accepted");
    assert.equal(restored.acceptance_id, 703);
    assert.equal(restored.acceptance_local_id, "free-cycle-703");
    assert.equal(restored.expires_local_at_ms, tappedAt + 10 * 60 * 1000);

    controller.installState({
        active: true,
        acceptance_id: 704,
        acceptance_local_id: "newer-cycle-704",
        status: "accepted",
        selection: item({id: 23, label: "EX-23"}),
        generated_at: new Date(tappedAt + 3 * 60 * 1000).toISOString(),
    });
    assert.equal(controller.project([cancelled]).acceptance_id, 704);

    controller.installState({
        active: true,
        acceptance_id: 703,
        acceptance_local_id: "free-cycle-703",
        status: "used",
        selection: item(),
        generated_at: new Date(tappedAt).toISOString(),
    });
    now = tappedAt + 10 * 60 * 1000;
    restored = controller.project([cancelled]);
    assert.equal(restored.active, false);
    assert.equal(restored.status, "closed");
});

test("server cancellation result confirms or rolls back only the matching optimistic restore", () => {
    const tappedAt = Date.parse("2026-09-27T02:00:00Z");
    const localStorage = storage();
    const controller = createDriverFreeBucketController({
        shell: shell(), storage: localStorage,
        window: {localStorage},
        now: tappedAt + 60 * 1000,
    });
    const event = {
        event_type: "driver.trip.loaded.cancelled",
        occurred_at: new Date(tappedAt + 60 * 1000).toISOString(),
        context_snapshot: Object.assign(displaySnapshot(item()), {
            authority_type: "free_bucket",
            free_bucket_acceptance_id: 703,
            free_bucket_acceptance_local_id: "free-cycle-703",
            free_bucket_expires_local_at_ms: tappedAt + 10 * 60 * 1000,
        }),
    };
    controller.installState({
        active: true, acceptance_id: 703, acceptance_local_id: "free-cycle-703",
        status: "used", selection: item(), generated_at: new Date(tappedAt).toISOString(),
    });
    let confirmed = controller.confirmManualCancellation(event, {
        free_bucket_restored: true,
        server_ids: {free_bucket_acceptance_id: 703},
        free_bucket_client_acceptance_id: "free-cycle-703",
    });
    assert.equal(confirmed.status, "accepted");
    assert.equal(confirmed.sync_mode, "confirmed");

    controller.installState({
        active: true, acceptance_id: 703, acceptance_local_id: "free-cycle-703",
        status: "used", selection: item(), generated_at: new Date(tappedAt).toISOString(),
    });
    confirmed = controller.confirmManualCancellation(event, {free_bucket_restored: false});
    assert.equal(confirmed.active, false);

    controller.installState({
        active: true, acceptance_id: 704, acceptance_local_id: "newer-cycle-704",
        status: "accepted", selection: item({id: 23}), generated_at: new Date(tappedAt).toISOString(),
    });
    confirmed = controller.confirmManualCancellation(event, {free_bucket_restored: false});
    assert.equal(confirmed.acceptance_id, 704);

    confirmed = controller.confirmManualCancellation(event, {
        free_bucket_restored: true,
        server_ids: {free_bucket_acceptance_id: 703},
        free_bucket_client_acceptance_id: "free-cycle-703",
    });
    assert.equal(confirmed.acceptance_id, 704);
    assert.equal(confirmed.acceptance_local_id, "newer-cycle-704");
});

test("confirmed manual free-bucket restore survives restart without renewing the original ttl", async () => {
    const tappedAt = Date.parse("2026-09-27T02:00:00Z");
    const deadline = tappedAt + 10 * 60 * 1000;
    let now = tappedAt + 5 * 60 * 1000;
    let activeController = null;
    const sandbox = {
        driverOfflineContext() {
            return {shiftId: 11, equipmentId: 17};
        },
        window: {
            DriverFreeBucket: {
                confirmManualCancellation(event, result) {
                    return activeController.confirmManualCancellation(event, result);
                },
            },
        },
    };
    const restoreConfirmedCancellation = vm.runInNewContext(
        "(" + functionSource(driverShiftRuntime, "restoreDriverConfirmedManualCancellation") + ")",
        sandbox
    );
    const receipt = {
        event_id: "driver-manual-cancel-free-703",
        event_type: "driver.trip.loaded.cancelled",
        occurred_at: new Date(tappedAt + 4 * 60 * 1000).toISOString(),
        shift_id: 11,
        equipment_id: 17,
        payload: {truck_id: 17, excavator_id: 22},
        context_snapshot: Object.assign(displaySnapshot(item()), {
            authority_type: "free_bucket",
        }),
        server_ids: {free_bucket_acceptance_id: 703},
        free_bucket_restored: true,
        free_bucket_client_acceptance_id: "free-cycle-703",
        free_bucket_expires_at: new Date(deadline).toISOString(),
        free_bucket_expires_local_at_ms: deadline,
    };
    const fragments = [
        ["empty", {}],
        ["matching used", {
            active: true,
            acceptance_id: 703,
            status: "used",
            selection: item(),
            generated_at: new Date(tappedAt).toISOString(),
        }],
        ["identity-free used", {
            active: true,
            status: "used",
            selection: item(),
            generated_at: new Date(tappedAt).toISOString(),
        }],
    ];

    for (const [label, fragment] of fragments) {
        const localStorage = storage();
        activeController = createDriverFreeBucketController({
            shell: shell(),
            storage: localStorage,
            window: {localStorage},
            now: () => now,
        });
        activeController.installState(fragment);
        const requested = [];
        const restored = await restoreConfirmedCancellation({
            getManualTripProjectionReceipt(shiftId, equipmentId) {
                requested.push([shiftId, equipmentId]);
                return Promise.resolve(receipt);
            },
        });

        assert.equal(restored, true, label);
        assert.deepEqual(requested, [[11, 17]], label);
        assert.equal(activeController.state().active, true, label);
        assert.equal(activeController.state().status, "accepted", label);
        assert.equal(activeController.state().sync_mode, "confirmed", label);
        assert.equal(activeController.state().acceptance_id, 703, label);
        assert.equal(activeController.state().acceptance_local_id, "free-cycle-703", label);
        assert.equal(activeController.state().expires_local_at_ms, deadline, label);

        now = deadline - 1;
        assert.equal(activeController.state().active, true, label + " before boundary");
        now = deadline;
        assert.equal(activeController.state().active, false, label + " at boundary");
        activeController.destroy();
        now = tappedAt + 5 * 60 * 1000;
    }
});

test("visibility pageshow and resume expire a suspended offline request without waiting for its timer", () => {
    const lifecycleCases = [
        ["document", "visibilitychange"],
        ["window", "pageshow"],
        ["document", "resume"],
        ["window", "resume"],
    ];
    lifecycleCases.forEach(([targetName, eventName], index) => {
        const tappedAt = Date.parse("2026-09-27T03:00:00Z") + index * 60 * 60 * 1000;
        let now = tappedAt;
        const rendered = [];
        const localStorage = storage();
        const documentObject = eventTarget({hidden: false});
        const windowObject = eventTarget({
            localStorage,
            navigator: {onLine: false},
            setTimeout() { return {unref() {}}; },
            clearTimeout() {},
            CustomEvent: function CustomEvent(type, options) {
                this.type = type;
                this.detail = options.detail;
            },
            dispatchEvent(event) { rendered.push(event.detail.state); },
        });
        const controller = createDriverFreeBucketController({
            shell: shell(),
            storage: localStorage,
            window: windowObject,
            document: documentObject,
            now: () => now,
        });
        controller.installState({
            active: true,
            acceptance_local_id: "sleep-request-" + index,
            status: "requested",
            selection: item(),
            sync_mode: "local",
            generated_at: new Date(tappedAt).toISOString(),
        });
        rendered.length = 0;
        now = tappedAt + 10 * 60 * 1000;
        (targetName === "document" ? documentObject : windowObject).emit(eventName);
        assert.equal(rendered.at(-1).active, false, eventName);
        controller.destroy();
    });
});

test("active selection A blocks selection B before enqueue and preserves A", async () => {
    let enqueueCalls = 0;
    const controller = createDriverFreeBucketController({
        shell: shell(),
        storage: storage(),
        window: {localStorage: storage()},
        outbox: {enqueue() { enqueueCalls += 1; return Promise.resolve({}); }},
    });
    controller.installCatalog(serverCatalog({excavators: [item(), item({id: 23, label: "EX-23"})]}));
    controller.installState({
        active: true,
        status: "accepted",
        can_cancel: true,
        acceptance_id: 701,
        selection: item(),
        sync_mode: "confirmed",
        version: 12,
        generated_at: "2026-09-14T03:01:00Z",
    });

    await assert.rejects(controller.select(controller.catalog().excavators[1]), /free_bucket_unavailable/);
    assert.equal(enqueueCalls, 0);
    assert.equal(controller.state().active, true);
    assert.equal(controller.state().selection.id, 22);
    assert.equal(controller.state().acceptance_id, 701);
});

test("a server-rejected selected event never leaves the free bucket stuck active", () => {
    // Боевой случай 26.09.2026 (afb373a5): отклонённый driver.free_bucket.selected
    // (conflict/invalid/auth_required) всё равно давал active:true + sync_mode
    // "review" — самосвал застревал навсегда, ни выбрать заново, ни отменить.
    // Сервер его не принял — ковша нет вообще, а не "на сверке".
    const controller = createDriverFreeBucketController({
        shell: shell(),
        storage: storage(),
        window: {localStorage: storage()},
        outbox: {enqueue() { return Promise.resolve({}); }},
    });
    controller.installCatalog(serverCatalog());
    const projected = controller.project([{
        event_type: "driver.free_bucket.selected",
        event_id: "rejected-selection-1",
        sequence: 1,
        state: "conflict",
        occurred_at: "2026-09-26T10:00:00Z",
        payload: {truck_id: 17, excavator_id: 22, catalog_version: 12},
    }]);
    assert.equal(projected.active, false);
    assert.notEqual(projected.sync_mode, "review");
    assert.equal(controller.state().active, false);
});

test("cancel drops a rejected local selection without queueing a meaningless server cancel", async () => {
    let enqueueCalls = 0;
    const localStorage = storage();
    const controller = createDriverFreeBucketController({
        shell: shell(),
        storage: localStorage,
        window: {localStorage},
        outbox: {
            pending() {
                return Promise.resolve([{
                    event_type: "driver.free_bucket.selected",
                    event_id: "rejected-selection-to-cancel",
                    state: "conflict",
                }]);
            },
            enqueue() {
                enqueueCalls += 1;
                return Promise.resolve({event_id: "unexpected-cancel"});
            },
        },
        now: Date.parse("2026-09-26T10:03:00Z"),
    });
    controller.installCatalog(serverCatalog());
    controller.installState({
        active: true,
        acceptance_local_id: "rejected-selection-to-cancel",
        status: "requested",
        can_cancel: true,
        selection: item(),
        sync_mode: "local",
        generated_at: "2026-09-26T10:01:00Z",
    });

    assert.equal(await controller.cancel(), null);
    assert.equal(enqueueCalls, 0);
    assert.equal(controller.state().active, false);
    assert.notEqual(controller.state().sync_mode, "review");
});

test("a rejected cancel (dependency_rejected on an already-rejected selection) still deactivates locally", () => {
    // Замкнутый круг 26.09.2026: cancel() ставил dependsOn на отклонённый
    // selected → сервер отвечал dependency_rejected (state "conflict") →
    // project() снова ставил "review" и оставлял active — отменить было
    // нельзя никогда. Правило 1: намерение водителя отменить — истина, даже
    // если сама отмена не подтверждена сервером.
    const controller = createDriverFreeBucketController({
        shell: shell(),
        storage: storage(),
        window: {localStorage: storage()},
        outbox: {enqueue() { return Promise.resolve({}); }},
        now: Date.parse("2026-09-26T10:03:00Z"),
    });
    controller.installCatalog(serverCatalog());
    controller.project([{
        event_type: "driver.free_bucket.selected",
        event_id: "selection-2",
        sequence: 1,
        state: "conflict",
        occurred_at: "2026-09-26T10:00:00Z",
        payload: {truck_id: 17, excavator_id: 22, catalog_version: 12},
    }]);
    // Отклонённый выбор уже неактивен (правило выше); имитируем отдельный
    // случай — активный выбор, чью отмену сервер отклоняет как dependency_rejected.
    controller.project([{
        event_type: "driver.free_bucket.selected",
        event_id: "selection-3",
        sequence: 2,
        occurred_at: "2026-09-26T10:01:00Z",
        payload: {truck_id: 17, excavator_id: 22, catalog_version: 12},
    }]);
    assert.equal(controller.state().active, true);
    const projected = controller.project([{
        event_type: "driver.free_bucket.cancelled",
        event_id: "cancel-3",
        sequence: 3,
        state: "conflict",
        depends_on: ["selection-3"],
        occurred_at: "2026-09-26T10:02:00Z",
        payload: {free_bucket_acceptance_local_id: "selection-3"},
    }]);
    assert.equal(projected.active, false);
    assert.notEqual(projected.sync_mode, "review");
});

test("a stuck review state from storage becomes inactive as soon as project() runs, even with no new events", () => {
    // Третий сценарий из того же замкнутого круга: состояние, уже записанное
    // в хранилище (stateKey) с sync_mode "review" ДО этой правки, не должно
    // пережить следующий же вызов project() — даже без единого нового
    // события в пакете.
    const controller = createDriverFreeBucketController({
        shell: shell(),
        storage: storage(),
        window: {localStorage: storage()},
        outbox: {enqueue() { return Promise.resolve({}); }},
    });
    controller.installCatalog(serverCatalog());
    controller.installState({
        active: true,
        status: "requested",
        can_cancel: true,
        acceptance_local_id: "stuck-selection",
        selection: item(),
        sync_mode: "review",
        version: 12,
        generated_at: "2026-09-14T03:01:00Z",
    });
    const projected = controller.project([]);
    assert.equal(projected.active, false);
    assert.notEqual(projected.sync_mode, "review");
    assert.equal(controller.state().active, false);
});

test("a rejected local selection falls back to the server's live acceptance, not to 'no bucket'", () => {
    // Стенд 28.09.2026: у сервера принятие #4 (requested, ЭКГ-15), водитель тыкал
    // другую плитку — сервер отвечал free_bucket_target_changed («уже выбран другой
    // экскаватор»), а проекция гасила состояние в «ковша нет»: выбранная плитка не
    // светилась, отменить принятие было нельзя (cancel() требует state.active).
    const controller = createDriverFreeBucketController({
        shell: shell(),
        storage: storage(),
        window: {localStorage: storage()},
        outbox: {enqueue() { return Promise.resolve({}); }},
    });
    controller.installCatalog(serverCatalog());
    controller.installState({
        active: true,
        status: "requested",
        can_cancel: true,
        acceptance_id: 4,
        selection: item(),
        sync_mode: "server",
        version: 30,
        generated_at: "2026-09-28T08:00:00Z",
    });
    const projected = controller.project([{
        event_id: "select-other",
        event_type: "driver.free_bucket.selected",
        sequence: 21,
        state: "conflict",
        last_error: {code: "free_bucket_target_changed"},
        occurred_at: "2026-09-28T08:16:00Z",
        payload: {truck_id: 17, excavator_id: 63, catalog_version: 31},
    }]);
    assert.equal(projected.active, true, "server acceptance stays visible");
    assert.equal(projected.acceptance_id, 4);
    assert.equal(projected.selection.id, 22);
    assert.equal(controller.state().active, true);
});

test("the one-load right is consumed by its load and switched off locally by that trip's completion", () => {
    // Свободный ковш — на один рейс (владелец, 28.09.2026): погрузка под принятие
    // переводит право в «использовано», завершение этого рейса гасит режим сразу,
    // на телефоне; без завершения режим ещё активен.
    const controller = createDriverFreeBucketController({
        shell: shell(),
        storage: storage(),
        window: {localStorage: storage()},
        outbox: {enqueue() { return Promise.resolve({}); }},
    });
    controller.installCatalog(serverCatalog());
    controller.installState({active: false});
    const selected = {
        event_id: "select-one-load", event_type: "driver.free_bucket.selected", sequence: 1, state: "pending",
        occurred_at: "2026-09-28T10:00:00Z", payload: {truck_id: 17, excavator_id: 22, catalog_version: 12},
    };
    const loaded = {
        event_id: "manual-load-one", event_type: "driver.trip.loaded", sequence: 2, state: "pending",
        local_trip_id: "manual-load-one", occurred_at: "2026-09-28T10:01:00Z",
        payload: {truck_id: 17, excavator_id: 22, dump_point_id: 5, free_bucket_acceptance_local_id: "select-one-load"},
    };
    let projected = controller.project([selected, loaded]);
    assert.equal(projected.active, true, "loaded but not yet unloaded: the mode stays on");
    assert.equal(projected.status, "used");
    assert.equal(projected.can_cancel, false);
    const completed = {
        event_id: "manual-complete-one", event_type: "driver.trip.manual_completed", sequence: 3, state: "pending",
        local_trip_id: "manual-load-one", occurred_at: "2026-09-28T10:20:00Z", payload: {truck_id: 17},
    };
    projected = controller.project([selected, loaded, completed]);
    assert.equal(projected.active, false, "completion of the bucket trip switches the mode off");
    assert.equal(projected.status, "used");
    assert.equal(controller.state().active, false);
    // Отклонённое завершение режим не гасит.
    const rejectedCompletion = Object.assign({}, completed, {event_id: "manual-complete-rejected", state: "conflict"});
    assert.equal(controller.project([selected, loaded, rejectedCompletion]).active, true);
});

test("a right the server already marked used switches off on the driver's unload of that trip", () => {
    const controller = createDriverFreeBucketController({
        shell: shell(),
        storage: storage(),
        window: {localStorage: storage()},
        outbox: {enqueue() { return Promise.resolve({}); }},
    });
    controller.installCatalog(serverCatalog());
    controller.installState({
        active: true, status: "used", can_cancel: false, acceptance_id: 9, selection: item(),
        sync_mode: "server", version: 40, generated_at: "2026-09-28T10:05:00Z",
    });
    assert.equal(controller.project([]).active, true, "loaded by the excavator, not yet unloaded");
    const projected = controller.project([{
        event_id: "unload-9", event_type: "driver.trip.unloaded", sequence: 7, state: "pending",
        trip_id: 451, occurred_at: "2026-09-28T10:25:00Z", payload: {trip_id: 451},
    }]);
    assert.equal(projected.active, false);
    assert.equal(projected.status, "used");
});

test("missing or invalid server catalog keeps last-good snapshot", () => {
    const localStorage = storage();
    const controller = createDriverFreeBucketController({
        shell: shell(),
        storage: localStorage,
        window: {localStorage, navigator: {onLine: true}},
        now: Date.parse("2026-09-14T03:10:00Z"),
    });
    controller.installCatalog(serverCatalog());
    assert.equal(controller.installCatalog(null).excavators[0].id, 22);
    assert.equal(controller.installCatalog({schema: "wrong", complete: true, excavators: []}).excavators[0].id, 22);
    assert.equal(controller.catalog().stale, true);
    assert.equal(isAuthoritativeCatalog(null), false);
});

test("authoritative complete empty catalog clears the last-good snapshot", () => {
    const localStorage = storage();
    const controller = createDriverFreeBucketController({
        shell: shell(), storage: localStorage,
        window: {localStorage, navigator: {onLine: true}},
        now: Date.parse("2026-09-14T03:10:00Z"),
    });
    controller.installCatalog(serverCatalog());
    const cleared = controller.installCatalog(serverCatalog({version: 13, generated_at: "2026-09-14T03:05:00Z", excavators: []}));
    assert.equal(cleared.complete, true);
    assert.deepEqual(cleared.excavators, []);
    assert.deepEqual(controller.installCatalog(null).excavators, []);
});

test("aged online catalog is marked stale", () => {
    const localStorage = storage();
    const now = Date.parse("2026-09-14T05:01:00Z");
    const controller = createDriverFreeBucketController({
        shell: shell(), storage: localStorage,
        window: {localStorage, navigator: {onLine: true}},
        now,
    });
    assert.equal(catalogIsStale(serverCatalog(), {navigator: {onLine: true}}, Date.parse("2026-09-14T03:59:59Z")), false);
    assert.equal(catalogIsStale(serverCatalog(), {navigator: {onLine: true}}, now), true);
    assert.equal(controller.installCatalog(serverCatalog()).stale, true);
});

test("unavailable tile keeps its status after an unselected state render", () => {
    const unavailable = item({available: false});
    assert.equal(tileStatusLabel(unavailable, false), "Недоступно");
    assert.equal(tileStatusLabel(unavailable, true), "Выбран");
    assert.equal(tileStatusLabel(unavailable, false), "Недоступно");
});

test("technical review state is never shown as a field-worker problem", () => {
    const source = fs.readFileSync(path.join(__dirname, "../driver-free-bucket-v1.js"), "utf8");
    assert.doesNotMatch(source, /НУЖНА СВЕРКА/);
    assert.doesNotMatch(source, /Не подтверждено/);
    assert.doesNotMatch(source, /classList\.toggle\("is-review"/);
});

test("browser lifecycle drops the controller for disabled and detached shells", () => {
    const previousDocument = global.document;
    const previousStorage = global.localStorage;
    const enabled = shell();
    const disabled = shell();
    disabled.dataset.driverFreeBucketEnabled = "false";
    const replacement = shell();
    let active = enabled;
    global.document = {
        querySelector() { return active; },
        getElementById() { return null; },
    };
    global.localStorage = storage();
    try {
        assert.ok(global.DriverFreeBucket.bind({shell: enabled}));
        active = disabled;
        assert.equal(global.DriverFreeBucket.renderProjection(disabled, []), null);
        active = replacement;
        assert.equal(global.DriverFreeBucket.renderProjection(enabled, []), null);
    } finally {
        global.document = previousDocument;
        global.localStorage = previousStorage;
    }
});

test("template always renders a hidden cancel control for restored offline selection", () => {
    const template = driverScreenSource();
    assert.match(template, /data-driver-free-bucket-remove\{% if not driver_free_bucket_state\.can_cancel %\} hidden/);
    assert.doesNotMatch(template, /\{% if driver_free_bucket_state\.can_cancel %\}[\s\S]{0,160}data-driver-free-bucket-remove/);
});

test("free-bucket projection keeps the dial label short and renders a compact mode chip", () => {
    const source = fs.readFileSync(path.join(__dirname, "../driver-free-bucket-v1.js"), "utf8");
    const styles = fs.readFileSync(path.join(__dirname, "../../css/driver-free-bucket-v1.css"), "utf8");
    const template = driverScreenSource();
    assert.match(source, /setDialLabel\(item\.label\)/);
    assert.doesNotMatch(source, /setDialLabel\("Свободный ковш · " \+ item\.label\)/);
    assert.match(source, /chip\.textContent = active \? "Свободный ковш · " \+ state\.selection\.label : ""/);
    // Плашки режима над барабаном больше нет — признак режима сама угловая кнопка.
    assert.doesNotMatch(template, /data-driver-free-bucket-chip/);
    assert.match(styles, /\.driver-free-bucket-sheet\s*\{[\s\S]*?z-index:\s*170;/);
    assert.match(source, /node\.dataset\.driverDialRaw = label/);
    assert.match(source, /scheduleDriverDialLabelFit\(true\)/);
    assert.match(source, /target\.focus\(\{ preventScroll: true \}\)/);
    assert.match(template, /window\.scheduleDriverDialLabelFit = scheduleDriverDialLabelFit/);
});

test("free-bucket tiles show only the excavator number and status, no place/rock/point text", () => {
    const source = fs.readFileSync(path.join(__dirname, "../driver-free-bucket-v1.js"), "utf8");
    const styles = fs.readFileSync(path.join(__dirname, "../../css/driver-free-bucket-v1.css"), "utf8");
    const template = driverScreenSource();
    // Горизонт/блок/порода/точка и «Не заполнено: …» мешали разглядеть сам номер —
    // убраны из отображения (26.09.2026), но остаются в data-атрибутах для выбора.
    assert.match(template, /<strong class="driver-free-bucket-tile-number">\{\{ excavator\.label \}\}<\/strong>/);
    assert.doesNotMatch(template, /driver-free-bucket-tile-place/);
    assert.doesNotMatch(template, /driver-free-bucket-tile-meta/);
    assert.doesNotMatch(template, /driver-free-bucket-tile-missing/);
    assert.match(template, /data-loading-horizon="\{\{ excavator\.loading_horizon \}\}"/);
    assert.match(source, /'<strong class="driver-free-bucket-tile-number"><\/strong>'/);
    assert.doesNotMatch(source, /driver-free-bucket-tile-place/);
    assert.doesNotMatch(source, /driver-free-bucket-tile-meta/);
    assert.doesNotMatch(source, /driver-free-bucket-tile-missing/);
    assert.match(styles, /\.driver-free-bucket-tile-number\s*\{/);
});
