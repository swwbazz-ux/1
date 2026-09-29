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

test("an old rejected selection in the review queue does not switch off the live bucket after its load is confirmed", () => {
    // Боевой Infinix 29.09.2026 (v367): в очереди «на сверке» лежал старый
    // отклонённый выбор. Пока новые выбор и погрузка ждали отправки, ковш светился;
    // как только они подтверждались и уходили из очереди, старый отказ откатывал
    // проекцию к серверному состоянию до выбора — ковш гас через полсекунды после
    // погрузки, круг становился синим.
    const controller = createDriverFreeBucketController({
        shell: shell(),
        storage: storage(),
        window: {localStorage: storage()},
        outbox: {enqueue() { return Promise.resolve({}); }},
    });
    controller.installCatalog(serverCatalog());
    controller.installState({active: false});
    const oldRejected = {
        event_id: "old-rejected-select", event_type: "driver.free_bucket.selected", sequence: 1, state: "conflict",
        last_error: {code: "free_bucket_not_available"}, occurred_at: "2026-09-29T10:00:00Z",
        payload: {truck_id: 17, excavator_id: 22, catalog_version: 12},
    };
    const selected = {
        event_id: "select-live", event_type: "driver.free_bucket.selected", sequence: 5, state: "pending",
        occurred_at: "2026-09-29T10:40:00Z", payload: {truck_id: 17, excavator_id: 22, catalog_version: 12},
    };
    const loaded = {
        event_id: "load-live", event_type: "driver.trip.loaded", sequence: 6, state: "pending",
        local_trip_id: "load-live", occurred_at: "2026-09-29T10:41:00Z",
        payload: {truck_id: 17, excavator_id: 22, dump_point_id: 5, free_bucket_acceptance_local_id: "select-live"},
    };
    assert.equal(controller.project([oldRejected, selected, loaded]).active, true);
    // Выбор и погрузка подтверждены и ушли из очереди — остался только старый отказ.
    const projected = controller.project([oldRejected]);
    assert.equal(projected.active, true, "the live bucket stays on until its trip is unloaded");
    assert.equal(projected.status, "used");
    assert.equal(projected.acceptance_local_id, "select-live");
    // Свой же отклонённый выбор по-прежнему гасит местный ковш.
    const fresh = createDriverFreeBucketController({
        shell: shell(),
        storage: storage(),
        window: {localStorage: storage()},
        outbox: {enqueue() { return Promise.resolve({}); }},
    });
    fresh.installCatalog(serverCatalog());
    fresh.installState({active: false});
    assert.equal(fresh.project([selected]).active, true);
    assert.equal(fresh.project([Object.assign({}, selected, {state: "conflict"})]).active, false);
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
