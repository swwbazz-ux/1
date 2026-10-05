(function (root) {
    "use strict";

    var DB_NAME = "copper-excavator-local-shifts-v1";
    var DB_VERSION = 1;
    var STORE_NAME = "states";
    var STORAGE_PREFIX = "excavator-local-shift-v1:";
    var LOAD_TYPES = ["excavator.trip.loaded", "excavator.free_bucket.loaded"];

    function clone(value) {
        return JSON.parse(JSON.stringify(value));
    }

    function canonical(value) {
        if (Array.isArray(value)) return value.map(canonical);
        if (value && typeof value === "object") {
            return Object.keys(value).sort().reduce(function (result, key) {
                if (typeof value[key] !== "undefined") result[key] = canonical(value[key]);
                return result;
            }, {});
        }
        return value;
    }

    function sameEvent(left, right) {
        return JSON.stringify(canonical(left)) === JSON.stringify(canonical(right));
    }

    function requestPromise(request) {
        return new Promise(function (resolve, reject) {
            request.onsuccess = function () { resolve(request.result); };
            request.onerror = function () { reject(request.error || new Error("IndexedDB unavailable")); };
        });
    }

    function indexedAdapter(indexedDB, storageId) {
        var dbPromise = new Promise(function (resolve, reject) {
            var request;
            try { request = indexedDB.open(DB_NAME, DB_VERSION); } catch (error) { reject(error); return; }
            request.onupgradeneeded = function () {
                if (!request.result.objectStoreNames.contains(STORE_NAME)) {
                    request.result.createObjectStore(STORE_NAME, {keyPath: "storage_id"});
                }
            };
            request.onsuccess = function () { resolve(request.result); };
            request.onerror = function () { reject(request.error || new Error("IndexedDB unavailable")); };
        });

        function transaction(mode, operation) {
            return dbPromise.then(function (db) {
                return new Promise(function (resolve, reject) {
                    var tx;
                    try { tx = db.transaction(STORE_NAME, mode); } catch (error) { reject(error); return; }
                    var result;
                    try { result = operation(tx.objectStore(STORE_NAME)); } catch (error) { reject(error); return; }
                    tx.oncomplete = function () { resolve(result); };
                    tx.onerror = function () { reject(tx.error || new Error("IndexedDB write failed")); };
                    tx.onabort = function () { reject(tx.error || new Error("IndexedDB write aborted")); };
                });
            });
        }

        return {
            kind: "indexedDB",
            read: function () {
                return dbPromise.then(function (db) {
                    return requestPromise(db.transaction(STORE_NAME, "readonly").objectStore(STORE_NAME).get(storageId));
                }).then(function (row) { return row ? clone(row.state) : null; });
            },
            write: function (state) {
                return transaction("readwrite", function (store) {
                    store.put({storage_id: storageId, state: clone(state)});
                });
            }
        };
    }

    function localStorageAdapter(storage, storageId) {
        if (!storage) throw new Error("localStorage unavailable");
        var key = STORAGE_PREFIX + storageId;
        return {
            kind: "localStorage",
            read: function () {
                return Promise.resolve().then(function () {
                    var raw = storage.getItem(key);
                    return raw ? JSON.parse(raw) : null;
                });
            },
            write: function (state) {
                return Promise.resolve().then(function () {
                    storage.setItem(key, JSON.stringify(state));
                });
            }
        };
    }

    function createStorage(options, storageId) {
        if (options.adapter) return Promise.resolve(options.adapter);
        var fallback;
        try { fallback = localStorageAdapter(options.localStorage, storageId); } catch (error) { fallback = null; }
        if (!options.indexedDB) {
            if (!fallback) return Promise.reject(new Error("Локальное хранилище недоступно."));
            return fallback.read().then(function () { return fallback; }).catch(function () {
                throw new Error("Локальное хранилище недоступно.");
            });
        }
        var indexed = indexedAdapter(options.indexedDB, storageId);
        return indexed.read().then(function () {
            if (!fallback) return indexed;
            return {
                kind: "indexedDB+localStorage-fallback",
                read: function () {
                    return Promise.allSettled([indexed.read(), fallback.read()]).then(function (results) {
                        var values = results.filter(function (result) {
                            return result.status === "fulfilled" && result.value;
                        }).map(function (result) { return result.value; });
                        if (!values.length) {
                            if (results.every(function (result) { return result.status === "rejected"; })) {
                                throw new Error("Локальное хранилище недоступно.");
                            }
                            return null;
                        }
                        values.sort(function (left, right) {
                            return Number(right.revision || 0) - Number(left.revision || 0)
                                || Date.parse(right.updated_at || "") - Date.parse(left.updated_at || "");
                        });
                        return clone(values[0]);
                    });
                },
                write: function (state) {
                    return Promise.allSettled([
                        indexed.write(state),
                        fallback.write(state)
                    ]).then(function (results) {
                        if (results.some(function (result) { return result.status === "fulfilled"; })) return;
                        throw new Error("Локальное хранилище недоступно.");
                    });
                }
            };
        }).catch(function () {
            if (!fallback) throw new Error("Локальное хранилище недоступно.");
            return fallback.read().then(function () { return fallback; }).catch(function () {
                throw new Error("Локальное хранилище недоступно.");
            });
        });
    }

    function emptyState(identity) {
        return {
            schema_version: 1,
            identity: clone(identity),
            current_local_shift_id: "",
            next_sequence: 1,
            shifts: []
        };
    }

    function eventLocalShiftId(event) {
        return String(event && (event.local_shift_id || (event.payload || {}).local_shift_id) || "");
    }

    function findShift(state, localShiftId) {
        return (state.shifts || []).find(function (shift) {
            return String(shift.local_shift_id) === String(localShiftId || "");
        }) || null;
    }

    function eventEntry(shift, eventId) {
        return (shift.events || []).find(function (entry) {
            return String(entry.event && entry.event.event_id || "") === String(eventId || "");
        }) || null;
    }

    function volumeNumber(value) {
        if (value === null || value === undefined || value === "") return null;
        var parsed = Number(String(value).replace(",", "."));
        return Number.isFinite(parsed) ? parsed : null;
    }

    function loadFacts(shift) {
        var cancelled = {};
        (shift.events || []).forEach(function (entry) {
            if (!entry || !entry.event || entry.event.event_type !== "excavator.trip.loaded.cancelled") return;
            var payload = entry.event.payload || {};
            [payload.source_load_event_id, payload.load_event_id, payload.local_trip_id, entry.event.local_trip_id]
                .filter(Boolean).forEach(function (value) { cancelled[String(value)] = true; });
        });
        return (shift.events || []).filter(function (entry) {
            return entry && entry.event && LOAD_TYPES.indexOf(entry.event.event_type) >= 0;
        }).map(function (entry) {
            var event = entry.event;
            var payload = event.payload || {};
            return {
                event_id: event.event_id,
                local_trip_id: event.local_trip_id || payload.local_trip_id || "",
                occurred_at: event.occurred_at,
                truck_id: payload.truck_id || null,
                truck_number: payload.truck_number || "",
                fleet_code: payload.local_fleet_code || "unknown",
                dump_point_id: payload.dump_point_id || null,
                dump_point: payload.dump_point_name || "Точка не определена",
                volume_m3: volumeNumber(payload.local_volume_m3),
                server_trip_id: entry.server_ids && entry.server_ids.trip_id || null,
                cancelled: Boolean(cancelled[String(event.event_id)] || cancelled[String(event.local_trip_id || "")])
            };
        });
    }

    function twoDigits(value) {
        return String(value).padStart(2, "0");
    }

    function localHourPayload(code, title, start, end) {
        return {
            code: code,
            title: title,
            period: {
                start: start.toISOString(),
                end: end.toISOString(),
                label: twoDigits(start.getHours()) + ":00–" + twoDigits(end.getHours()) + ":00"
            },
            rows: [],
            totals: {belaz: 0, nhl: 0, trip_count: 0, volume_m3: 0},
            source_trip_count: 0,
            source_trip_ids: [],
            unclassified_trip_count: 0,
            unknown_volume_trip_count: 0,
            is_empty: true
        };
    }

    function emptyHourlyReport(shift, capturedAt) {
        var now = new Date(capturedAt || Date.now());
        var currentStart = new Date(now.getTime());
        currentStart.setMinutes(0, 0, 0);
        var previousStart = new Date(currentStart.getTime() - 3600000);
        return {
            schema_version: 2,
            generated_at: now.toISOString(),
            freshness_label: "Данные на " + twoDigits(now.getHours()) + ":" + twoDigits(now.getMinutes()),
            excavator: {id: shift ? shift.equipment_id : null, name: "Экскаватор"},
            work_date: now.toLocaleDateString("ru-RU", {day: "numeric", month: "long"}),
            hours: [
                localHourPayload("current", "Текущий час", currentStart, now),
                localHourPayload("previous", "Предыдущий час", previousStart, currentStart)
            ],
            data_quality: {unclassified_trip_count: 0, unknown_dump_point_trip_count: 0, complete: true}
        };
    }

    function rebasedHourlyReport(serverPayload, shift, capturedAt) {
        var target = emptyHourlyReport(shift, capturedAt);
        if (!serverPayload || serverPayload.schema_version !== 2 || !Array.isArray(serverPayload.hours)) {
            return target;
        }
        target.excavator = clone(serverPayload.excavator || target.excavator);
        target.work_date = target.work_date || serverPayload.work_date;
        target.data_quality = clone(serverPayload.data_quality || target.data_quality);
        target.server_generated_at = serverPayload.generated_at || "";
        target.server_coverage_until = serverPayload.generated_at || "";
        target.hours = target.hours.map(function (wanted) {
            var wantedStart = Date.parse(wanted.period.start || "");
            var source = serverPayload.hours.find(function (hour) {
                return Date.parse(hour && hour.period && hour.period.start || "") === wantedStart;
            });
            if (!source) return wanted;
            var merged = clone(source);
            merged.code = wanted.code;
            merged.title = wanted.title;
            merged.period = clone(wanted.period);
            merged.rows = Array.isArray(merged.rows) ? merged.rows : [];
            merged.source_trip_ids = Array.isArray(merged.source_trip_ids) ? merged.source_trip_ids : [];
            merged.source_event_ids = Array.isArray(merged.source_event_ids) ? merged.source_event_ids : [];
            return merged;
        });
        target.generated_at = new Date(capturedAt || Date.now()).toISOString();
        target.freshness_label = serverPayload.freshness_label || target.freshness_label;
        return target;
    }

    function mergeFact(hour, fact, direction) {
        var fleet = fact.fleet_code === "belaz" || fact.fleet_code === "nhl" ? fact.fleet_code : "unknown";
        hour.source_trip_count = Math.max(0, Number(hour.source_trip_count || 0) + direction);
        hour.is_empty = hour.source_trip_count === 0;
        hour.totals = hour.totals || {belaz: 0, nhl: 0, trip_count: 0};
        if (fleet === "unknown") {
            hour.unclassified_trip_count = Math.max(0, Number(hour.unclassified_trip_count || 0) + direction);
        } else {
            var pointId = fact.dump_point_id === null || fact.dump_point_id === "" ? null : Number(fact.dump_point_id);
            var row = (hour.rows || []).find(function (item) {
                return String(item.dump_point_id || "") === String(pointId || "");
            });
            if (!row && direction > 0) {
                row = {dump_point_id: pointId, dump_point: fact.dump_point, belaz: 0, nhl: 0};
                hour.rows.push(row);
            }
            if (row) {
                row[fleet] = Math.max(0, Number(row[fleet] || 0) + direction);
                if (!row.belaz && !row.nhl) hour.rows = hour.rows.filter(function (item) { return item !== row; });
            }
            hour.totals[fleet] = Math.max(0, Number(hour.totals[fleet] || 0) + direction);
            hour.totals.trip_count = Math.max(0, Number(hour.totals.trip_count || 0) + direction);
        }
        var volume = volumeNumber(fact.volume_m3);
        if (volume === null) {
            hour.unknown_volume_trip_count = Math.max(0, Number(hour.unknown_volume_trip_count || 0) + direction);
        } else {
            hour.totals.volume_m3 = Math.max(0, Number(hour.totals.volume_m3 || 0) + (direction * volume));
        }
    }

    function mergeHourlyReport(serverPayload, shift, facts, capturedAt) {
        var payload = rebasedHourlyReport(serverPayload, shift, capturedAt);
        var coveredTrips = {};
        var coveredEvents = {};
        (payload.hours || []).forEach(function (hour) {
            hour.rows = Array.isArray(hour.rows) ? hour.rows : [];
            (hour.source_trip_ids || []).forEach(function (id) { coveredTrips[String(id)] = hour; });
            (hour.source_event_ids || []).forEach(function (id) { coveredEvents[String(id)] = hour; });
        });
        (facts || []).forEach(function (fact) {
            var coveredHour = coveredEvents[String(fact.event_id || "")]
                || (fact.server_trip_id ? coveredTrips[String(fact.server_trip_id)] : null);
            if (coveredHour) {
                if (fact.cancelled) mergeFact(coveredHour, fact, -1);
                return;
            }
            if (fact.cancelled) return;
            var occurred = Date.parse(fact.occurred_at || "");
            var hour = (payload.hours || []).find(function (candidate) {
                var start = Date.parse(candidate.period && candidate.period.start || "");
                var end = Date.parse(candidate.period && candidate.period.end || "");
                return Number.isFinite(occurred) && occurred >= start && occurred < end;
            });
            if (hour) mergeFact(hour, fact, 1);
        });
        payload.local_projection = {
            local_shift_id: shift ? shift.local_shift_id : "",
            retained_event_count: facts ? facts.length : 0,
            generated_at: new Date(capturedAt || Date.now()).toISOString()
        };
        return payload;
    }

    function mergeShiftSummary(serverProjection, shift) {
        var projection = serverProjection || {};
        var coveredTrips = {};
        var coveredEvents = {};
        (projection.source_trip_ids || []).forEach(function (id) { coveredTrips[String(id)] = true; });
        (projection.source_event_ids || []).forEach(function (id) { coveredEvents[String(id)] = true; });
        var result = {
            trip_count: Math.max(0, Number(projection.trip_count || 0)),
            volume_m3: Math.max(0, Number(projection.volume_m3 || 0)),
            unknown_volume_trip_count: 0,
            local_shift_id: shift ? shift.local_shift_id : ""
        };
        (shift ? loadFacts(shift) : []).forEach(function (fact) {
            var isCovered = Boolean(
                coveredEvents[String(fact.event_id || "")]
                || (fact.server_trip_id && coveredTrips[String(fact.server_trip_id)])
            );
            var direction = fact.cancelled ? -1 : 1;
            if (!isCovered && fact.cancelled) return;
            if (isCovered && !fact.cancelled) return;
            result.trip_count = Math.max(0, result.trip_count + direction);
            var volume = volumeNumber(fact.volume_m3);
            if (volume === null) {
                result.unknown_volume_trip_count = Math.max(
                    0,
                    result.unknown_volume_trip_count + direction
                );
            } else {
                result.volume_m3 = Math.max(0, result.volume_m3 + (direction * volume));
            }
        });
        return result;
    }

    function createLedger(options) {
        options = options || {};
        var identity = {
            access_id: Number(options.accessId || 0),
            actor_id: Number(options.actorId || 0),
            role_code: String(options.roleCode || "excavator_operator"),
            device_id: String(options.deviceId || "")
        };
        var storageId = [identity.role_code, identity.access_id, identity.device_id].join(":");
        var storagePromise = createStorage(options, storageId);
        var state = null;
        var committedState = null;
        var mutationTail = Promise.resolve();
        var readyPromise = null;
        var outbox = options.outbox || null;

        function persist() {
            return storagePromise.then(function (storage) {
                state.revision = Number((committedState || {}).revision || 0) + 1;
                state.updated_at = new Date().toISOString();
                return storage.write(clone(state)).then(function () {
                    committedState = clone(state);
                    // A detached UI observer cannot reverse a durable commit.
                    if (typeof options.onChange === "function") {
                        try { options.onChange(clone(committedState)); } catch (error) { /* saved */ }
                    }
                    return clone(state);
                });
            });
        }

        function validIdentity(stored) {
            if (!stored || !stored.identity) return false;
            return Number(stored.identity.access_id || 0) === identity.access_id
                && Number(stored.identity.actor_id || 0) === identity.actor_id
                && String(stored.identity.role_code || "") === identity.role_code
                && String(stored.identity.device_id || "") === identity.device_id;
        }

        function applyConfirmation(event, result) {
            var shift = findShift(state, eventLocalShiftId(event));
            if (!shift) return false;
            var entry = eventEntry(shift, event.event_id);
            if (!entry || !sameEvent(entry.event, event)) return false;
            entry.delivery_state = "confirmed";
            entry.confirmed_at = new Date().toISOString();
            entry.server_ids = clone(result && result.server_ids || {});
            entry.server_result = clone(result || {});
            if (event.event_type === "excavator.shift.opened" && entry.server_ids.shift_id) {
                shift.server_shift_id = Number(entry.server_ids.shift_id);
                shift.server_opened_at = result.effective_occurred_at || result.opened_at || "";
            }
            if (event.event_type === "excavator.shift.closed") shift.server_closed = true;
            return true;
        }

        function mutate(operation) {
            var result = mutationTail.then(ready).then(function () {
                state = clone(committedState);
                return operation();
            });
            mutationTail = result.then(function () {}, function () {
                state = committedState ? clone(committedState) : null;
            });
            return result;
        }

        function deliver(event) {
            if (!outbox || typeof outbox.queue !== "function") return Promise.resolve();
            return Promise.resolve().then(function () { return outbox.queue(clone(event)); }).then(function (queued) {
                if (queued && queued.sync_state === "confirmed" && queued.server_result) {
                    return confirm(event, queued.server_result);
                }
            }).catch(function () {
                // Exact envelope remains in the ledger for another attempt.
            });
        }

        function recoverDelivery() {
            var entries = [];
            ((committedState || {}).shifts || []).forEach(function (shift) {
                (shift.events || []).forEach(function (entry) {
                    if (entry.delivery_state !== "confirmed") entries.push(clone(entry.event));
                });
            });
            // Receipts and delivery must never hold local startup or each other.
            var deliveries = entries.sort(function (left, right) {
                return Number(left.sequence || 0) - Number(right.sequence || 0);
            }).map(deliver);
            if (outbox && typeof outbox.confirmed === "function") {
                deliveries.push(Promise.resolve().then(function () { return outbox.confirmed(); }).then(function (items) {
                    return Promise.all((items || []).filter(function (item) {
                        return item && item.event;
                    }).map(function (item) { return confirm(item.event, item.result || {}); }));
                }).catch(function () {}));
            }
            return Promise.all(deliveries);
        }

        function ready() {
            if (readyPromise) return readyPromise;
            readyPromise = storagePromise.then(function (storage) { return storage.read(); }).then(function (stored) {
                if (stored && !validIdentity(stored)) throw new Error("Локальный журнал принадлежит другому доступу.");
                if (stored && (stored.schema_version !== 1 || !Array.isArray(stored.shifts))) {
                    throw new Error("Локальный журнал повреждён или имеет другую версию.");
                }
                state = stored || emptyState(identity);
                committedState = clone(state);
                recoverDelivery().catch(function () {});
                return clone(committedState);
            });
            return readyPromise;
        }

        function currentShift() {
            return state ? findShift(state, state.current_local_shift_id) : null;
        }

        function ensureImportedServerShift(serverShift) {
            if (!serverShift || !serverShift.id) return Promise.resolve(null);
            var serverId = Number(serverShift.id);
            var existing = (state.shifts || []).find(function (shift) {
                return Number(shift.server_shift_id || 0) === serverId;
            });
            if (existing) {
                return Promise.resolve(clone(existing));
            }
            var localCurrent = currentShift();
            if (localCurrent && localCurrent.status === "open" && !localCurrent.server_shift_id) {
                // A late HTML/realtime snapshot may still describe the shift
                // that was open before this device created its durable local
                // shift.  It is evidence to reconcile later, not permission
                // to replace the phone's newer working context.
                return Promise.resolve(localCurrent);
            }
            var localId = "server-shift:" + serverId;
            var imported = {
                local_shift_id: localId,
                open_event_id: "",
                equipment_id: Number(serverShift.equipment_id || 0),
                opened_at: serverShift.opened_at || new Date().toISOString(),
                readings: {},
                status: "open",
                server_shift_id: serverId,
                imported: true,
                events: []
            };
            state.shifts.push(imported);
            state.current_local_shift_id = localId;
            return persist().then(function () { return imported; });
        }

        function recordEvent(event) {
            if (!event || !event.event_id || !validIdentity({identity: event})) {
                return Promise.reject(new Error("Действие не принадлежит текущему доступу."));
            }
            var duplicate = null;
            state.shifts.some(function (candidate) {
                duplicate = eventEntry(candidate, event.event_id);
                return Boolean(duplicate);
            });
            if (duplicate) {
                if (!sameEvent(duplicate.event, event)) return Promise.reject(new Error("Локальный ID уже занят другим действием."));
                return Promise.resolve(clone(duplicate));
            }
            var localShiftId = eventLocalShiftId(event);
            if (event.local_shift_id && (event.payload || {}).local_shift_id
                && String(event.local_shift_id) !== String(event.payload.local_shift_id)) {
                return Promise.reject(new Error("Локальный ID смены не совпадает."));
            }
            var shift = findShift(state, localShiftId);
            if (!Number.isSafeInteger(event.sequence) || event.sequence < Number(state.next_sequence || 1)) {
                return Promise.reject(new Error("Порядок действий в локальном журнале нарушен."));
            }
            if (event.event_type === "excavator.shift.opened") {
                if (!localShiftId || localShiftId !== event.event_id) {
                    return Promise.reject(new Error("Открытие смены должно иметь устойчивый локальный ID."));
                }
                var active = currentShift();
                if (active && active.status === "open") {
                    return Promise.reject(new Error("Текущая смена уже открыта на телефоне."));
                }
                shift = shift || {
                    local_shift_id: localShiftId,
                    open_event_id: event.event_id,
                    equipment_id: Number(event.equipment_id || (event.payload || {}).excavator_id || 0),
                    opened_at: event.occurred_at,
                    readings: {
                        fuel: (event.payload || {}).fuel,
                        fuel_percent: (event.payload || {}).fuel_percent,
                        engine_hours: (event.payload || {}).engine_hours
                    },
                    status: "open",
                    server_shift_id: null,
                    events: []
                };
                if (!findShift(state, localShiftId)) state.shifts.push(shift);
                state.current_local_shift_id = localShiftId;
            }
            if (!shift) return Promise.reject(new Error("Локальная смена для действия не найдена."));
            if (shift.status === "closed") return Promise.reject(new Error("Локальная смена уже закрыта."));
            var entry = {event: clone(event), delivery_state: "awaiting_outbox", saved_at: new Date().toISOString()};
            shift.events.push(entry);
            state.next_sequence = Math.max(Number(state.next_sequence || 1), Number(event.sequence || 0) + 1);
            if (event.event_type === "excavator.shift.closed") {
                shift.status = "closed";
                shift.closed_at = event.occurred_at;
                shift.close_event_id = event.event_id;
            }
            return persist().then(function () { return clone(entry); });
        }

        function recordAndQueue(event) {
            // Freeze the caller's envelope before the first asynchronous boundary.
            var savedEvent = clone(event);
            return mutate(function () { return recordEvent(savedEvent); }).then(function () {
                deliver(savedEvent);
                return clone(savedEvent);
            });
        }

        function confirm(event, result) {
            var savedEvent = clone(event);
            var savedResult = clone(result || {});
            return mutate(function () {
                if (!applyConfirmation(savedEvent, savedResult)) return false;
                return persist().then(function () { return true; });
            });
        }

        function snapshot() {
            return ready().then(function () { return clone(committedState); });
        }

        function nextSequence() {
            return ready().then(function () {
                var maximum = Number(committedState.next_sequence || 1) - 1;
                (committedState.shifts || []).forEach(function (shift) {
                    (shift.events || []).forEach(function (entry) {
                        maximum = Math.max(maximum, Number(entry && entry.event && entry.event.sequence || 0));
                    });
                });
                return maximum + 1;
            });
        }

        function events() {
            return ready().then(function () {
                var result = [];
                (committedState.shifts || []).forEach(function (shift) {
                    (shift.events || []).forEach(function (entry) {
                        if (entry && entry.event) result.push(clone(entry.event));
                    });
                });
                return result;
            });
        }

        function facts(localShiftId) {
            return ready().then(function () {
                var shift = findShift(committedState, localShiftId || committedState.current_local_shift_id);
                return shift ? loadFacts(shift) : [];
            });
        }

        function workContext(localShiftId) {
            return ready().then(function () {
                var shift = findShift(committedState, localShiftId || committedState.current_local_shift_id);
                if (!shift) return null;
                var entry = (shift.events || []).filter(function (item) {
                    return item && item.event && item.event.event_type === "excavator.work_context.changed";
                }).sort(function (left, right) {
                    return Number(right.event.sequence || 0) - Number(left.event.sequence || 0);
                })[0];
                return entry ? clone(entry.event.payload || {}) : null;
            });
        }

        function getEvent(eventId) {
            return ready().then(function () {
                var found = null;
                (committedState.shifts || []).some(function (shift) {
                    var entry = eventEntry(shift, eventId);
                    if (!entry) return false;
                    found = clone(entry.event);
                    return true;
                });
                return found;
            });
        }

        function storageKind() {
            return storagePromise.then(function (storage) { return storage.kind || "custom"; });
        }

        function hourlyReport(serverPayload, capturedAt, localShiftId) {
            return ready().then(function () {
                var shift = findShift(committedState, localShiftId || committedState.current_local_shift_id);
                return mergeHourlyReport(serverPayload, shift, shift ? loadFacts(shift) : [], capturedAt);
            });
        }

        function shiftSummary(serverProjection, localShiftId) {
            return ready().then(function () {
                var shift = findShift(committedState, localShiftId || committedState.current_local_shift_id);
                return mergeShiftSummary(serverProjection, shift);
            });
        }

        return {
            ready: ready,
            currentShift: function () {
                var shift = committedState ? findShift(committedState, committedState.current_local_shift_id) : null;
                return shift ? clone(shift) : null;
            },
            ensureImportedServerShift: function (value) {
                var savedValue = value ? clone(value) : null;
                return mutate(function () { return ensureImportedServerShift(savedValue); });
            },
            recoverDelivery: function () { return ready().then(recoverDelivery); },
            recordAndQueue: recordAndQueue,
            confirm: confirm,
            snapshot: snapshot,
            nextSequence: nextSequence,
            events: events,
            facts: facts,
            workContext: workContext,
            getEvent: getEvent,
            hourlyReport: hourlyReport,
            shiftSummary: shiftSummary,
            storageKind: storageKind
        };
    }

    root.createExcavatorLocalShiftLedger = createLedger;
    if (typeof module !== "undefined") module.exports = createLedger;
})(typeof window !== "undefined" ? window : globalThis);
