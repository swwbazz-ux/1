(function (root) {
    "use strict";

    var DB_NAME = "field-offline-events-v1";
    var STORE_NAME = "events";
    var META_STORE = "meta";
    var FORMAT_VERSION = 1;
    var TERMINAL_STATES = new Set(["conflict", "auth_required", "invalid"]);

    function nowIso() { return new Date().toISOString(); }
    function number(value) {
        var parsed = Number(value);
        return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
    }
    function randomId(prefix) {
        var uuid = root.crypto && typeof root.crypto.randomUUID === "function"
            ? root.crypto.randomUUID()
            : Date.now().toString(36) + "-" + Math.random().toString(36).slice(2) + "-" + Math.random().toString(36).slice(2);
        return (prefix || "event") + ":" + uuid;
    }
    function clone(value) { return JSON.parse(JSON.stringify(value)); }

    function localRepository(storage, accessId) {
        var key = "driver-offline-events-v2:" + accessId;
        var metaKey = key + ":meta";
        function read() {
            var value = JSON.parse(storage.getItem(key) || "[]");
            if (!Array.isArray(value)) throw new Error("offline_store_corrupt");
            return value;
        }
        return {
            kind: "localStorage",
            list: async function () { return clone(read()); },
            put: async function (event) {
                var items = read();
                var index = items.findIndex(function (item) { return item.event_id === event.event_id; });
                if (index >= 0) items[index] = clone(event); else items.push(clone(event));
                storage.setItem(key, JSON.stringify(items));
                return clone(event);
            },
            remove: async function (eventId) {
                storage.setItem(key, JSON.stringify(read().filter(function (item) { return item.event_id !== eventId; })));
            },
            getMeta: async function (name) {
                var meta = JSON.parse(storage.getItem(metaKey) || "{}");
                return meta[name];
            },
            setMeta: async function (name, value) {
                var meta = JSON.parse(storage.getItem(metaKey) || "{}");
                meta[name] = value;
                storage.setItem(metaKey, JSON.stringify(meta));
            }
        };
    }

    function indexedRepository(indexedDB) {
        var database = new Promise(function (resolve, reject) {
            var request = indexedDB.open(DB_NAME, 1);
            request.onupgradeneeded = function () {
                var db = request.result;
                if (!db.objectStoreNames.contains(STORE_NAME)) {
                    var events = db.createObjectStore(STORE_NAME, {keyPath: "event_id"});
                    events.createIndex("access_sequence", ["access_id", "sequence"], {unique: false});
                }
                if (!db.objectStoreNames.contains(META_STORE)) db.createObjectStore(META_STORE);
            };
            request.onsuccess = function () { resolve(request.result); };
            request.onerror = function () { reject(request.error || new Error("indexeddb_open_failed")); };
        });
        function request(store, mode, operation) {
            return database.then(function (db) {
                return new Promise(function (resolve, reject) {
                    var transaction = db.transaction(store, mode);
                    var value;
                    transaction.oncomplete = function () { resolve(value); };
                    transaction.onabort = transaction.onerror = function () {
                        reject(transaction.error || new Error("indexeddb_transaction_failed"));
                    };
                    value = operation(transaction.objectStore(store));
                    if (value && typeof value.onsuccess !== "undefined") {
                        value.onsuccess = function () { value = value.result; };
                        value.onerror = function () { transaction.abort(); };
                    }
                });
            });
        }
        return {
            kind: "indexedDB",
            list: function () { return request(STORE_NAME, "readonly", function (store) { return store.getAll(); }); },
            put: function (event) { return request(STORE_NAME, "readwrite", function (store) { store.put(clone(event)); return clone(event); }); },
            remove: function (eventId) { return request(STORE_NAME, "readwrite", function (store) { store.delete(eventId); }); },
            getMeta: function (name) { return request(META_STORE, "readonly", function (store) { return store.get(name); }); },
            setMeta: function (name, value) { return request(META_STORE, "readwrite", function (store) { store.put(value, name); }); }
        };
    }

    async function defaultRepository(options) {
        if (options.repository) return options.repository;
        if (options.indexedDB) {
            try {
                var primary = indexedRepository(options.indexedDB);
                await primary.getMeta("probe");
                return primary;
            } catch (error) {}
        }
        if (!options.localStorage) throw new Error("durable_storage_unavailable");
        return localRepository(options.localStorage, options.accessId);
    }

    function backoff(attempt) {
        return Math.min(5 * 60 * 1000, 5000 * Math.pow(2, Math.min(6, Math.max(0, attempt - 1))));
    }

    function createDriverOfflineOutbox(options) {
        options = options || {};
        var accessId = String(options.accessId || "").trim();
        if (!accessId) throw new Error("driver_access_required");
        var repoPromise = defaultRepository(options);
        var running = null;
        var timer = null;
        var enqueueChain = Promise.resolve();

        function context() {
            return typeof options.context === "function" ? (options.context() || {}) : (options.context || {});
        }
        async function sequence(repo) {
            var current = Number(await repo.getMeta("sequence:" + accessId)) || 0;
            current += 1;
            await repo.setMeta("sequence:" + accessId, current);
            return current;
        }
        async function listAll() {
            var repo = await repoPromise;
            var items = await repo.list();
            return items.filter(function (item) { return String(item.access_id) === accessId; })
                .sort(function (a, b) { return Number(a.sequence) - Number(b.sequence); });
        }
        async function publish() {
            var items = await listAll();
            if (typeof options.onState === "function") {
                options.onState({
                    pending: items.filter(function (item) { return !TERMINAL_STATES.has(item.state); }).length,
                    review: items.filter(function (item) { return TERMINAL_STATES.has(item.state); }).length,
                    sending: !!running,
                    storage: (await repoPromise).kind,
                    events: clone(items)
                });
            }
            return items;
        }
        async function enqueueOne(spec) {
            var repo = await repoPromise;
            var ctx = context();
            var occurredAt = String(spec.occurred_at || nowIso());
            var deviceId = String(spec.device_id || ctx.deviceId || await repo.getMeta("device_id") || "");
            if (!deviceId) deviceId = randomId("install");
            await repo.setMeta("device_id", deviceId);
            var requestedId = String(spec.event_id || randomId("driver"));
            var existing = (await listAll()).find(function (item) { return item.event_id === requestedId; });
            if (existing) {
                if (existing.event_type !== String(spec.event_type || "")
                        || JSON.stringify(existing.payload || {}) !== JSON.stringify(spec.payload || {})) {
                    throw new Error("offline_event_id_reused");
                }
                return clone(existing);
            }
            var event = {
                event_id: requestedId,
                event_type: String(spec.event_type || ""),
                format_version: FORMAT_VERSION,
                actor_id: number(spec.actor_id || ctx.actorId),
                access_id: number(spec.access_id || ctx.accessId || accessId),
                role_code: "driver",
                device_id: deviceId,
                shift_id: number(spec.shift_id || ctx.shiftId),
                equipment_id: number(spec.equipment_id || ctx.equipmentId),
                trip_id: number(spec.trip_id),
                local_trip_id: spec.local_trip_id ? String(spec.local_trip_id) : null,
                occurred_at: occurredAt,
                sequence: await sequence(repo),
                depends_on: Array.isArray(spec.depends_on) ? spec.depends_on.map(String) : [],
                payload: clone(spec.payload || {}),
                state: "pending",
                attempt_count: 0,
                next_retry_at: 0,
                last_error: null,
                created_at: occurredAt,
                updated_at: nowIso()
            };
            if (!event.event_type || !event.device_id || !event.shift_id || !event.equipment_id) {
                throw new Error("offline_event_context_incomplete");
            }
            await repo.put(event); // UI may change only after this resolves.
            await publish();
            return clone(event);
        }
        function enqueue(spec) {
            var operation = enqueueChain.then(function () { return enqueueOne(spec); });
            enqueueChain = operation.catch(function () {});
            return operation;
        }
        async function update(event, patch) {
            var repo = await repoPromise;
            await repo.put(Object.assign({}, event, patch, {updated_at: nowIso()}));
        }
        function schedule(delay) {
            if (timer || typeof root.setTimeout !== "function") return;
            timer = root.setTimeout(function () {
                timer = null;
                flush().catch(function () {});
            }, Math.max(1000, delay || 1000));
            if (timer && typeof timer.unref === "function") timer.unref();
        }
        async function applyResults(sent, response) {
            var repo = await repoPromise;
            var results = Array.isArray(response && response.results) ? response.results : [];
            var byId = new Map(results.map(function (result) { return [String(result.event_id || ""), result]; }));
            for (var event of sent) {
                var result = byId.get(event.event_id);
                if (!result) {
                    var missingAttempt = Number(event.attempt_count || 0) + 1;
                    await update(event, {state: "pending", attempt_count: missingAttempt, next_retry_at: Date.now() + backoff(missingAttempt), last_error: {code: "missing_ack", message: "Сервер не подтвердил событие."}});
                    continue;
                }
                var status = String(result.status || "invalid");
                if (status === "accepted" || status === "deduplicated") {
                    await repo.remove(event.event_id);
                    if (typeof options.onConfirmed === "function") await options.onConfirmed(clone(event), clone(result));
                } else if (TERMINAL_STATES.has(status)) {
                    await update(event, {state: status, next_retry_at: 0, last_error: {code: result.code || status, message: result.message || "Требуется сверка."}, server_received_at: result.server_received_at || null});
                    if (typeof options.onReview === "function") options.onReview(clone(event), clone(result));
                } else {
                    var attempt = Number(event.attempt_count || 0) + 1;
                    await update(event, {state: "pending", attempt_count: attempt, next_retry_at: Date.now() + backoff(attempt), last_error: {code: result.code || "retry", message: result.message || "Сервер временно недоступен."}});
                }
            }
        }
        async function doFlush() {
            var items = await listAll();
            var due = items.filter(function (item) {
                return item.state === "pending" && Number(item.next_retry_at || 0) <= Date.now();
            }).slice(0, Number(options.batchSize) || 20);
            if (!due.length) {
                var future = items.filter(function (item) { return item.state === "pending"; })
                    .map(function (item) { return Number(item.next_retry_at || 0); }).filter(Boolean).sort()[0];
                if (future) schedule(Math.max(1000, future - Date.now()));
                return publish();
            }
            await publish();
            try {
                var ctx = context();
                var response = await options.send({
                    protocol_version: FORMAT_VERSION,
                    format_version: FORMAT_VERSION,
                    role_code: "driver",
                    actor_id: number(ctx.actorId),
                    access_id: number(ctx.accessId || accessId),
                    device_id: String(ctx.deviceId || due[0].device_id || ""),
                    events: due.map(function (event) {
                        var copy = clone(event);
                        delete copy.state; delete copy.attempt_count; delete copy.next_retry_at;
                        delete copy.last_error; delete copy.updated_at;
                        return copy;
                    })
                });
                await applyResults(due, response || {});
            } catch (error) {
                for (var event of due) {
                    var attempt = Number(event.attempt_count || 0) + 1;
                    await update(event, {state: "pending", attempt_count: attempt, next_retry_at: Date.now() + backoff(attempt), last_error: {code: "network", message: "Сервер временно недоступен."}});
                }
            }
            var remaining = await publish();
            var next = remaining.filter(function (event) { return event.state === "pending"; })
                .map(function (event) { return Number(event.next_retry_at || 0); }).filter(Boolean).sort()[0];
            if (next) schedule(Math.max(1000, next - Date.now()));
            return remaining;
        }
        function flush() {
            if (running) return running;
            running = doFlush().finally(function () { running = null; publish().catch(function () {}); });
            return running;
        }
        async function migrateLegacyUnload() {
            if (!options.localStorage) return;
            var legacyKey = "driver-unload-outbox-v1:" + accessId;
            var raw = options.localStorage.getItem(legacyKey);
            if (!raw) return;
            var legacy = JSON.parse(raw);
            if (!Array.isArray(legacy)) return;
            for (var item of legacy) {
                await enqueue({
                    event_id: item.client_action_id,
                    event_type: "driver.trip.unloaded",
                    trip_id: item.trip_id,
                    occurred_at: item.occurred_at,
                    payload: {trip_id: number(item.trip_id)}
                });
            }
            options.localStorage.removeItem(legacyKey);
        }
        function bindLifecycle() {
            if (!root.addEventListener) return;
            root.addEventListener("online", function () { flush().catch(function () {}); });
            root.addEventListener("pageshow", function () { flush().catch(function () {}); });
            if (root.document && root.document.addEventListener) {
                root.document.addEventListener("visibilitychange", function () {
                    if (!root.document.hidden) flush().catch(function () {});
                });
            }
        }
        async function initialize() {
            await migrateLegacyUnload();
            bindLifecycle();
            await publish();
            return flush();
        }
        return {initialize: initialize, enqueue: enqueue, flush: flush, pending: listAll, publish: publish};
    }

    root.createDriverOfflineOutbox = createDriverOfflineOutbox;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = {createDriverOfflineOutbox: createDriverOfflineOutbox, localRepository: localRepository, backoff: backoff};
    }
})(typeof window !== "undefined" ? window : globalThis);
