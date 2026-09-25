"use strict";

const test = require("node:test");
const {driverScreenSource} = require("./driver-screen-source");
const assert = require("node:assert/strict");

const {
    createDriverFreeBucketController,
    catalogIsStale,
    displaySnapshot,
    isAuthoritativeCatalog,
    normalizeCatalog,
    resolveInstalledState,
    tileStatusLabel,
} = require("../driver-free-bucket-v1.js");
const fs = require("node:fs");
const path = require("node:path");

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
    assert.equal(tileStatusLabel(unavailable, false, {sync_mode: "confirmed"}), "Недоступно");
    assert.equal(tileStatusLabel(unavailable, true, {sync_mode: "review"}), "Не подтверждено");
    assert.equal(tileStatusLabel(unavailable, false, {sync_mode: "confirmed"}), "Недоступно");
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
    assert.match(template, /data-driver-free-bucket-chip/);
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
