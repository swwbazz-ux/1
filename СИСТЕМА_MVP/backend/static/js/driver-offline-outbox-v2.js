(function (root) {
    "use strict";

    var DB_NAME = "field-offline-events-v1";
    var STORE_NAME = "events";
    var META_STORE = "meta";
    var FORMAT_VERSION = 1;
    var TERMINAL_STATES = new Set(["conflict", "auth_required", "invalid"]);
    var SUPPORTED_TYPES = new Set([
        "driver.trip.unloaded",
        "driver.trip.dump_point_changed",
        "driver.trip.loaded",
        "driver.free_bucket.selected",
        "driver.free_bucket.cancelled",
        "driver.downtime.started",
        "driver.downtime.ended",
        "driver.shift.closed"
    ]);
    var IMMUTABLE_FIELDS = [
        "event_id", "event_type", "format_version", "actor_id", "access_id",
        "role_code", "device_id", "shift_id", "equipment_id", "trip_id",
        "local_trip_id", "local_downtime_id", "occurred_at", "depends_on", "payload",
        "context_snapshot"
    ];

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
    function canonical(value) {
        if (Array.isArray(value)) return value.map(canonical);
        if (value && typeof value === "object") {
            return Object.keys(value).sort().reduce(function (result, key) {
                result[key] = canonical(value[key]);
                return result;
            }, {});
        }
        return value;
    }
    function sameIdentity(left, right) {
        return IMMUTABLE_FIELDS.every(function (field) {
            return JSON.stringify(canonical(left[field])) === JSON.stringify(canonical(right[field]));
        });
    }
    function identityRecord(event) {
        return IMMUTABLE_FIELDS.reduce(function (result, field) {
            result[field] = clone(event[field] === undefined ? null : event[field]);
            return result;
        }, {});
    }
    function validateEvent(event) {
        if (!SUPPORTED_TYPES.has(event.event_type)) throw new Error("offline_event_type_not_permitted");
        if (!event.actor_id || !event.access_id || !event.device_id || !event.shift_id || !event.equipment_id) {
            throw new Error("offline_event_context_incomplete");
        }
        if (!event.context_snapshot || typeof event.context_snapshot !== "object" || Array.isArray(event.context_snapshot)) {
            throw new Error("offline_event_snapshot_invalid");
        }
        if (event.event_type === "driver.trip.loaded") {
            if (event.trip_id || !event.local_trip_id || event.local_trip_id !== event.event_id) {
                throw new Error("offline_manual_trip_identity_invalid");
            }
            if (
                !number(event.payload.truck_id)
                || !number(event.payload.excavator_id)
                || !number(event.payload.dump_point_id)
                || !number(event.payload.rock_type_id)
                || event.payload.manual_control !== true
            ) throw new Error("offline_manual_trip_context_incomplete");
            var assignmentId = number(event.payload.assignment_id);
            var acceptanceId = number(event.payload.free_bucket_acceptance_id);
            var acceptanceLocalId = String(event.payload.free_bucket_acceptance_local_id || "");
            if ((assignmentId ? 1 : 0) + ((acceptanceId || acceptanceLocalId) ? 1 : 0) !== 1) {
                throw new Error("offline_manual_trip_authority_ambiguous");
            }
            if (number(event.payload.truck_id) !== number(event.equipment_id)) {
                throw new Error("offline_manual_trip_truck_mismatch");
            }
        } else if (event.event_type.indexOf("driver.trip.") === 0 && !event.trip_id && !event.local_trip_id) {
            throw new Error("offline_trip_required");
        }
        if (event.event_type === "driver.trip.dump_point_changed" && !number(event.payload.dump_point_id)) {
            throw new Error("offline_dump_point_required");
        }
        if (event.event_type === "driver.free_bucket.selected") {
            if (!number(event.payload.truck_id) || !number(event.payload.excavator_id)) {
                throw new Error("offline_free_bucket_context_incomplete");
            }
            if (number(event.payload.truck_id) !== number(event.equipment_id)) {
                throw new Error("offline_free_bucket_truck_mismatch");
            }
            if (event.trip_id) throw new Error("offline_free_bucket_trip_not_permitted");
        }
        if (event.event_type === "driver.free_bucket.cancelled") {
            var acceptanceId = number(event.payload.free_bucket_acceptance_id);
            var localAcceptanceId = String(event.payload.free_bucket_acceptance_local_id || "");
            if (acceptanceId && localAcceptanceId) {
                throw new Error("offline_free_bucket_acceptance_ambiguous");
            }
            if (!acceptanceId && (!localAcceptanceId || event.depends_on.indexOf(localAcceptanceId) < 0)) {
                throw new Error("offline_free_bucket_acceptance_required");
            }
            if (acceptanceId && event.depends_on.length) {
                throw new Error("offline_free_bucket_server_reference_dependency");
            }
            if (event.trip_id) throw new Error("offline_free_bucket_trip_not_permitted");
        }
        if (event.event_type === "driver.downtime.started" && !number(event.payload.reason_id)) {
            throw new Error("offline_downtime_reason_required");
        }
        if (event.event_type === "driver.downtime.started" && event.local_downtime_id !== event.event_id) {
            throw new Error("offline_downtime_local_id_mismatch");
        }
        if (event.event_type === "driver.downtime.ended") {
            var serverId = number(event.payload.downtime_id || event.payload.downtime_event_id);
            var localId = String(event.payload.local_downtime_id || "");
            if (!serverId && (!localId || event.depends_on.indexOf(localId) < 0)) {
                throw new Error("offline_downtime_reference_required");
            }
        }
    }
    function createDriverManualLoadEvent(options) {
        options = options || {};
        var eventId = String(options.eventId || randomId("driver-manual-load"));
        var assignmentId = number(options.assignmentId);
        var acceptanceId = number(options.acceptanceId);
        var acceptanceLocalId = String(options.acceptanceLocalId || "");
        if ((assignmentId ? 1 : 0) + ((acceptanceId || acceptanceLocalId) ? 1 : 0) !== 1) {
            throw new Error("offline_manual_trip_authority_ambiguous");
        }
        var payload = {
            manual_control: true,
            truck_id: number(options.truckId),
            excavator_id: number(options.excavatorId),
            dump_point_id: number(options.dumpPointId),
            rock_type_id: number(options.rockTypeId),
            placement_id: number(options.placementId),
            placement_updated_at: options.placementUpdatedAt ? String(options.placementUpdatedAt) : null,
            loading_horizon: String(options.loadingHorizon || ""),
            loading_block: String(options.loadingBlock || ""),
            transport_distance_km: options.transportDistanceKm === "" || options.transportDistanceKm === null || options.transportDistanceKm === undefined
                ? null : String(options.transportDistanceKm),
            assignment_id: assignmentId,
            free_bucket_acceptance_id: acceptanceId,
            free_bucket_acceptance_local_id: acceptanceId ? null : (acceptanceLocalId || null)
        };
        var dependsOn = Array.isArray(options.dependsOn) ? options.dependsOn.map(String) : [];
        if (acceptanceLocalId && dependsOn.indexOf(acceptanceLocalId) < 0) dependsOn.push(acceptanceLocalId);
        return {
            event_id: eventId,
            event_type: "driver.trip.loaded",
            occurred_at: String(options.occurredAt || nowIso()),
            trip_id: null,
            local_trip_id: eventId,
            depends_on: dependsOn,
            context_snapshot: clone(options.contextSnapshot || {}),
            payload: payload
        };
    }
    function createDriverPointChangeEvent(options) {
        options = options || {};
        var tripId = number(options.tripId);
        var localTripId = String(options.localTripId || "");
        var pointId = number(options.pointId);
        if ((!tripId && !localTripId) || (tripId && localTripId) || !pointId) {
            throw new Error("offline_point_context_incomplete");
        }
        var previous = (Array.isArray(options.events) ? options.events : []).slice().reverse().find(function (event) {
            return event.event_type === "driver.trip.dump_point_changed"
                && !TERMINAL_STATES.has(String(event.state || "pending"))
                && (
                    (tripId && number(event.trip_id) === tripId)
                    || (localTripId && String(event.local_trip_id || "") === localTripId)
                );
        });
        var expected = previous
            ? number(previous.payload && previous.payload.dump_point_id)
            : number(options.currentPointId);
        return {
            event_id: randomId("change-unload-point"),
            event_type: "driver.trip.dump_point_changed",
            trip_id: tripId,
            local_trip_id: localTripId || null,
            depends_on: previous
                ? [previous.event_id]
                : (localTripId ? [String(options.loadEventId || localTripId)] : []),
            payload: {
                dump_point_id: pointId,
                expected_actual_dump_point_id: expected
            },
            context_snapshot: {
                selected_dump_point_id: pointId,
                selected_dump_point_name: String(options.pointName || "")
            }
        };
    }
    function createDriverFreeBucketSelectedEvent(options) {
        options = options || {};
        var truckId = number(options.truckId);
        var excavatorId = number(options.excavatorId);
        if (!truckId || !excavatorId) throw new Error("offline_free_bucket_context_incomplete");
        var payload = {
            truck_id: truckId,
            excavator_id: excavatorId
        };
        if (options.catalogVersion !== undefined && options.catalogVersion !== null && options.catalogVersion !== "") {
            payload.catalog_version = Number(options.catalogVersion) || 0;
        }
        var generatedAt = options.catalogGeneratedAt || options.generatedAt;
        if (generatedAt) payload.catalog_generated_at = String(generatedAt);
        return {
            event_id: String(options.eventId || randomId("driver-free-bucket-select")),
            event_type: "driver.free_bucket.selected",
            occurred_at: String(options.occurredAt || nowIso()),
            trip_id: null,
            depends_on: [],
            context_snapshot: clone(options.contextSnapshot || {}),
            payload: payload
        };
    }
    function createDriverFreeBucketCancelledEvent(options) {
        options = options || {};
        var acceptanceId = number(options.acceptanceId);
        var localAcceptanceId = String(options.localAcceptanceId || options.pendingSelectionId || "");
        if (!acceptanceId && !localAcceptanceId) throw new Error("offline_free_bucket_acceptance_required");
        return {
            event_id: String(options.eventId || randomId("driver-free-bucket-cancel")),
            event_type: "driver.free_bucket.cancelled",
            occurred_at: String(options.occurredAt || nowIso()),
            trip_id: null,
            depends_on: acceptanceId ? [] : [localAcceptanceId],
            payload: {
                free_bucket_acceptance_id: acceptanceId,
                free_bucket_acceptance_local_id: acceptanceId ? null : localAcceptanceId
            }
        };
    }
    function isDriverSyncAuthResponse(response, body, baseUrl) {
        response = response || {};
        if (response.status === 401 || response.status === 403) return true;
        var responseUrl;
        try { responseUrl = new URL(String(response.url || baseUrl || "http://localhost/"), baseUrl || "http://localhost/"); }
        catch (error) { responseUrl = {pathname: ""}; }
        var isLoginPath = responseUrl.pathname === "/" || /\/login\/?$/.test(responseUrl.pathname);
        var contentType = response.headers && typeof response.headers.get === "function"
            ? String(response.headers.get("Content-Type") || "")
            : "";
        var isLoginHtml = /text\/html/i.test(contentType)
            && (/data-mobile-role-login/.test(String(body || "")) || /data-validated-login/.test(String(body || "")));
        return (response.redirected === true && isLoginPath) || isLoginHtml;
    }
    function createDriverDowntimeEndEvent(options) {
        options = options || {};
        var pendingStartId = String(options.pendingStartId || "");
        var serverId = number(options.serverId);
        if (!pendingStartId && !serverId) throw new Error("offline_downtime_reference_required");
        return {
            event_id: String(options.eventId || randomId("driver-downtime-close")),
            event_type: "driver.downtime.ended",
            occurred_at: String(options.occurredAt || nowIso()),
            depends_on: pendingStartId ? [pendingStartId] : [],
            local_downtime_id: pendingStartId || null,
            context_snapshot: clone(options.contextSnapshot || {}),
            payload: {
                downtime_id: serverId,
                local_downtime_id: pendingStartId || null
            }
        };
    }

    function selectDriverDowntimeProjection(events) {
        return (Array.isArray(events) ? events : [])
            .filter(function (event) {
                return event
                    && (event.event_type === "driver.downtime.started" || event.event_type === "driver.downtime.ended")
                    && !TERMINAL_STATES.has(String(event.state || "pending"));
            })
            .slice()
            .sort(function (left, right) { return Number(left.sequence) - Number(right.sequence); })
            .pop() || null;
    }

    function downtimeProjectionReceiptKey(shiftId, equipmentId) {
        shiftId = number(shiftId);
        equipmentId = number(equipmentId);
        return shiftId && equipmentId
            ? "downtime-projection:" + shiftId + ":" + equipmentId
            : "";
    }

    function manualTripProjectionReceiptKey(shiftId, equipmentId) {
        shiftId = number(shiftId);
        equipmentId = number(equipmentId);
        return shiftId && equipmentId ? "manual-trip-projection:" + shiftId + ":" + equipmentId : "";
    }

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
            get: async function (eventId) {
                var value = read().find(function (item) { return item.event_id === eventId; });
                return value ? clone(value) : null;
            },
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
            get: function (eventId) { return request(STORE_NAME, "readonly", function (store) { return store.get(eventId); }); },
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
        var drainRequested = false;
        var callbacks = {
            onState: options.onState,
            onConfirmed: options.onConfirmed,
            onReview: options.onReview
        };
        var contextProvider = options.context;

        function context() {
            return typeof contextProvider === "function" ? (contextProvider() || {}) : (contextProvider || {});
        }
        async function sequence(repo, actorId, deviceId) {
            var stableKey = "sequence:driver:" + String(actorId || "") + ":" + String(deviceId || "");
            var legacyKey = "sequence:" + accessId;
            var fallbackKey = "driver-offline-sequence:" + stableKey;
            var fallbackValue = 0;
            if (options.localStorage) {
                try { fallbackValue = Number(options.localStorage.getItem(fallbackKey)) || 0; }
                catch (error) { fallbackValue = 0; }
            }
            var current = Math.max(
                Number(await repo.getMeta(stableKey)) || 0,
                Number(await repo.getMeta(legacyKey)) || 0,
                fallbackValue
            );
            current += 1;
            await repo.setMeta(stableKey, current);
            await repo.setMeta(legacyKey, current);
            if (options.localStorage) {
                try { options.localStorage.setItem(fallbackKey, String(current)); } catch (error) {}
            }
            return current;
        }
        /* Записи «на сверке» (conflict / invalid / auth_required) — конечные: сервер
           их уже отклонил и сообщил об этом всплывающим сообщением. Раньше они
           лежали в хранилище вечно: на боевом телефоне 20.09.2026 нашлись восемь
           отклонённых стартов простоя трёхдневной давности, из-за которых подпись
           связи навсегда показывала «Не подтверждено». Старше суток — убираем. */
        var reviewRetentionMs = Number(options.reviewRetentionMs) > 0 ? Number(options.reviewRetentionMs) : 24 * 60 * 60 * 1000;
        async function listAll() {
            var repo = await repoPromise;
            var items = await repo.list();
            var mine = items.filter(function (item) { return String(item.access_id) === accessId; });
            var now = Date.now();
            var kept = [];
            for (var item of mine) {
                var stamp = Date.parse(item.updated_at || item.occurred_at || "") || 0;
                if (TERMINAL_STATES.has(item.state) && stamp && now - stamp > reviewRetentionMs) {
                    try { await repo.remove(item.event_id); } catch (error) {}
                    continue;
                }
                kept.push(item);
            }
            return kept.sort(function (a, b) { return Number(a.sequence) - Number(b.sequence); });
        }
        async function publish() {
            var items = await listAll();
            if (typeof callbacks.onState === "function") {
                try {
                    callbacks.onState({
                        pending: items.filter(function (item) { return !TERMINAL_STATES.has(item.state); }).length,
                        review: items.filter(function (item) { return TERMINAL_STATES.has(item.state); }).length,
                        sending: !!running,
                        storage: (await repoPromise).kind,
                        events: clone(items)
                    });
                } catch (error) {
                    // The event is already durable. A rendering error must not be reported as a storage failure.
                }
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
            var actorId = number(spec.actor_id || ctx.actorId);
            var eventAccessId = number(spec.access_id || ctx.accessId || accessId);
            var contextSnapshot = clone(spec.context_snapshot || {});
            contextSnapshot.actor_id = number(ctx.actorId) || actorId;
            contextSnapshot.access_id = number(ctx.accessId || accessId) || eventAccessId;
            contextSnapshot.role_code = "driver";
            var event = {
                event_id: requestedId,
                event_type: String(spec.event_type || ""),
                format_version: FORMAT_VERSION,
                actor_id: actorId,
                access_id: eventAccessId,
                role_code: "driver",
                device_id: deviceId,
                shift_id: number(spec.shift_id || ctx.shiftId),
                equipment_id: number(spec.equipment_id || ctx.equipmentId),
                trip_id: number(spec.trip_id),
                local_trip_id: spec.local_trip_id ? String(spec.local_trip_id) : null,
                local_downtime_id: spec.local_downtime_id
                    ? String(spec.local_downtime_id)
                    : (String(spec.event_type || "") === "driver.downtime.started"
                        ? requestedId
                        : (spec.payload && spec.payload.local_downtime_id ? String(spec.payload.local_downtime_id) : null)),
                occurred_at: occurredAt,
                sequence: null,
                depends_on: Array.isArray(spec.depends_on) ? spec.depends_on.map(String) : [],
                payload: clone(spec.payload || {}),
                context_snapshot: contextSnapshot,
                state: "pending",
                attempt_count: 0,
                next_retry_at: 0,
                last_error: null,
                created_at: occurredAt,
                updated_at: nowIso()
            };
            validateEvent(event);
            if (String(event.access_id) !== accessId) throw new Error("offline_event_access_mismatch");
            var existing = typeof repo.get === "function"
                ? await repo.get(requestedId)
                : (await repo.list()).find(function (item) { return item.event_id === requestedId; });
            if (existing) {
                if (!sameIdentity(existing, event)) throw new Error("offline_event_id_reused");
                return clone(existing);
            }
            var acknowledgedIdentity = await repo.getMeta("event-identity:" + requestedId);
            if (acknowledgedIdentity) {
                if (!sameIdentity(acknowledgedIdentity, event)) throw new Error("offline_event_id_reused");
                return Object.assign(clone(acknowledgedIdentity), {state: "confirmed"});
            }
            event.sequence = await sequence(repo, actorId, deviceId);
            await repo.put(event); // UI may change only after this resolves.
            await publish();
            return clone(event);
        }
        function enqueue(spec) {
            var operation = enqueueChain.then(function () { return enqueueOne(spec); });
            enqueueChain = operation.catch(function () {});
            return operation.then(function (event) {
                drainRequested = true;
                if (!running) schedule(0);
                return event;
            });
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
            var confirmed = [];
            var review = [];
            for (var event of sent) {
                var result = byId.get(event.event_id);
                if (!result) {
                    var missingAttempt = Number(event.attempt_count || 0) + 1;
                    await update(event, {state: "pending", attempt_count: missingAttempt, next_retry_at: Date.now() + backoff(missingAttempt), last_error: {code: "missing_ack", message: "Сервер не подтвердил событие."}});
                    continue;
                }
                var status = String(result.status || "invalid");
                if (status === "accepted" || status === "deduplicated") {
                    await repo.setMeta("event-identity:" + event.event_id, identityRecord(event));
                    if (result.server_ids) {
                        await repo.setMeta("server-map:" + event.event_id, clone(result.server_ids));
                    }
                    var downtimeReceiptKey = downtimeProjectionReceiptKey(event.shift_id, event.equipment_id);
                    if (downtimeReceiptKey && (event.event_type === "driver.downtime.started" || event.event_type === "driver.downtime.ended")) {
                        await repo.setMeta(downtimeReceiptKey, {
                            event_id: event.event_id,
                            event_type: event.event_type,
                            shift_id: number(event.shift_id),
                            equipment_id: number(event.equipment_id),
                            occurred_at: event.occurred_at,
                            confirmed_at: String(result.server_received_at || event.updated_at || event.occurred_at),
                            payload: clone(event.payload || {}),
                            projection: clone(
                                event.context_snapshot
                                && event.context_snapshot.downtime_projection
                                || null
                            ),
                            server_ids: clone(result.server_ids || null)
                        });
                    }
                    var manualReceiptKey = manualTripProjectionReceiptKey(event.shift_id, event.equipment_id);
                    if (manualReceiptKey && event.event_type === "driver.trip.loaded") {
                        await repo.setMeta(manualReceiptKey, {
                            event_id: event.event_id,
                            event_type: event.event_type,
                            local_trip_id: event.local_trip_id,
                            shift_id: number(event.shift_id),
                            equipment_id: number(event.equipment_id),
                            occurred_at: event.occurred_at,
                            confirmed_at: String(result.server_received_at || event.updated_at || event.occurred_at),
                            payload: clone(event.payload || {}),
                            context_snapshot: clone(event.context_snapshot || {}),
                            server_ids: clone(result.server_ids || null),
                            trip_origin: String(result.trip_origin || "driver_manual"),
                            version: number(result.server_version || result.version)
                        });
                    }
                    if (manualReceiptKey && event.event_type === "driver.trip.unloaded") {
                        var activeManualReceipt = await repo.getMeta(manualReceiptKey);
                        var activeManualTripId = number(
                            activeManualReceipt
                            && activeManualReceipt.server_ids
                            && activeManualReceipt.server_ids.trip_id
                        );
                        if (
                            (event.trip_id && number(event.trip_id) === activeManualTripId)
                            || (
                                event.local_trip_id
                                && String(event.local_trip_id) === String(activeManualReceipt && activeManualReceipt.local_trip_id || "")
                            )
                        ) await repo.setMeta(manualReceiptKey, null);
                    }
                    await repo.remove(event.event_id);
                    confirmed.push([clone(event), clone(result)]);
                } else if (TERMINAL_STATES.has(status)) {
                    await update(event, {
                        state: status,
                        next_retry_at: 0,
                        auth_generation: status === "auth_required" ? String(context().authGeneration || "") : null,
                        last_error: {code: result.code || status, message: result.message || "Требуется сверка."},
                        server_received_at: result.server_received_at || null
                    });
                    review.push([clone(event), clone(result)]);
                } else {
                    var attempt = Number(event.attempt_count || 0) + 1;
                    await update(event, {state: "pending", attempt_count: attempt, next_retry_at: Date.now() + backoff(attempt), last_error: {code: result.code || "retry", message: result.message || "Сервер временно недоступен."}});
                }
            }
            return {confirmed: confirmed, review: review};
        }
        async function doFlush() {
            while (true) {
                drainRequested = false;
                var items = await listAll();
                var allDue = items.filter(function (item) {
                    return item.state === "pending" && Number(item.next_retry_at || 0) <= Date.now();
                });
                var batchDeviceId = allDue.length ? String(allDue[0].device_id || "") : "";
                var due = allDue.filter(function (item) {
                    return String(item.device_id || "") === batchDeviceId;
                }).slice(0, Number(options.batchSize) || 20);
                if (!due.length) {
                    var future = items.filter(function (item) { return item.state === "pending"; })
                        .map(function (item) { return Number(item.next_retry_at || 0); }).filter(Boolean).sort()[0];
                    if (future) schedule(Math.max(1000, future - Date.now()));
                    if (drainRequested) continue;
                    return publish();
                }
                await publish();
                var callbackBatch = {confirmed: [], review: []};
                var response;
                try {
                    response = await options.send({
                        protocol_version: FORMAT_VERSION,
                        format_version: FORMAT_VERSION,
                        role_code: "driver",
                        actor_id: due[0].actor_id,
                        access_id: due[0].access_id,
                        device_id: due[0].device_id,
                        events: due.map(function (event) {
                            var copy = clone(event);
                            delete copy.state; delete copy.attempt_count; delete copy.next_retry_at;
                            delete copy.last_error; delete copy.updated_at;
                            delete copy.auth_generation;
                            return copy;
                        })
                    });
                } catch (error) {
                    for (var event of due) {
                        var attempt = Number(event.attempt_count || 0) + 1;
                        await update(event, {state: "pending", attempt_count: attempt, next_retry_at: Date.now() + backoff(attempt), last_error: {code: "network", message: "Сервер временно недоступен."}});
                    }
                    await publish();
                    continue;
                }
                callbackBatch = await applyResults(due, response || {});
                await publish();
                for (var confirmed of callbackBatch.confirmed) {
                    if (typeof callbacks.onConfirmed === "function") {
                        try { Promise.resolve(callbacks.onConfirmed(confirmed[0], confirmed[1])).catch(function () {}); } catch (error) {}
                    }
                }
                for (var flagged of callbackBatch.review) {
                    if (typeof callbacks.onReview === "function") {
                        try { Promise.resolve(callbacks.onReview(flagged[0], flagged[1])).catch(function () {}); } catch (error) {}
                    }
                }
            }
        }
        function flush() {
            if (running) return running;
            running = doFlush().finally(function () {
                running = null;
                publish().catch(function () {});
                if (drainRequested) schedule(0);
            });
            return running;
        }
        async function getServerMapping(eventId) {
            var repo = await repoPromise;
            return clone(await repo.getMeta("server-map:" + String(eventId || "")) || null);
        }
        async function getDowntimeProjectionReceipt(shiftId, equipmentId) {
            var key = downtimeProjectionReceiptKey(shiftId, equipmentId);
            if (!key) return null;
            var repo = await repoPromise;
            return clone(await repo.getMeta(key) || null);
        }
        async function getManualTripProjectionReceipt(shiftId, equipmentId) {
            var key = manualTripProjectionReceiptKey(shiftId, equipmentId);
            if (!key) return null;
            var repo = await repoPromise;
            return clone(await repo.getMeta(key) || null);
        }
        async function resumeAuthRequired(authGeneration) {
            authGeneration = String(authGeneration || "");
            if (!authGeneration) return publish();
            var items = await listAll();
            for (var item of items) {
                if (item.state === "auth_required" && String(item.auth_generation || "") !== authGeneration) {
                    await update(item, {state: "pending", attempt_count: 0, next_retry_at: 0, last_error: null, auth_generation: null});
                    drainRequested = true;
                }
            }
            return publish();
        }
        function setBindings(bindings) {
            bindings = bindings || {};
            if (Object.prototype.hasOwnProperty.call(bindings, "context")) contextProvider = bindings.context;
            if (Object.prototype.hasOwnProperty.call(bindings, "onState")) callbacks.onState = bindings.onState;
            if (Object.prototype.hasOwnProperty.call(bindings, "onConfirmed")) callbacks.onConfirmed = bindings.onConfirmed;
            if (Object.prototype.hasOwnProperty.call(bindings, "onReview")) callbacks.onReview = bindings.onReview;
            return publish();
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
            await resumeAuthRequired(context().authGeneration);
            await publish();
            return flush();
        }
        return {
            initialize: initialize,
            enqueue: enqueue,
            flush: flush,
            pending: listAll,
            publish: publish,
            setBindings: setBindings,
            resumeAuthRequired: resumeAuthRequired,
            getServerMapping: getServerMapping,
            getDowntimeProjectionReceipt: getDowntimeProjectionReceipt,
            getManualTripProjectionReceipt: getManualTripProjectionReceipt
        };
    }

    root.createDriverOfflineOutbox = createDriverOfflineOutbox;
    root.createDriverManualLoadEvent = createDriverManualLoadEvent;
    root.createDriverPointChangeEvent = createDriverPointChangeEvent;
    root.createDriverFreeBucketSelectedEvent = createDriverFreeBucketSelectedEvent;
    root.createDriverFreeBucketCancelledEvent = createDriverFreeBucketCancelledEvent;
    root.isDriverSyncAuthResponse = isDriverSyncAuthResponse;
    root.createDriverDowntimeEndEvent = createDriverDowntimeEndEvent;
    root.selectDriverDowntimeProjection = selectDriverDowntimeProjection;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = {
            createDriverOfflineOutbox: createDriverOfflineOutbox,
            localRepository: localRepository,
            indexedRepository: indexedRepository,
            createDriverPointChangeEvent: createDriverPointChangeEvent,
            createDriverManualLoadEvent: createDriverManualLoadEvent,
            createDriverFreeBucketSelectedEvent: createDriverFreeBucketSelectedEvent,
            createDriverFreeBucketCancelledEvent: createDriverFreeBucketCancelledEvent,
            isDriverSyncAuthResponse: isDriverSyncAuthResponse,
            createDriverDowntimeEndEvent: createDriverDowntimeEndEvent,
            selectDriverDowntimeProjection: selectDriverDowntimeProjection,
            backoff: backoff
        };
    }
})(typeof window !== "undefined" ? window : globalThis);
