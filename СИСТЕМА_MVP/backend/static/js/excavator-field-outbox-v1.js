(function (root) {
    "use strict";

    var DB_NAME = "copper-field-actions-v1";
    var DB_VERSION = 1;
    var STORE_NAME = "events";
    var LEGACY_PREFIX = "excavator-field-outbox-v1:";
    var RETRY_BASE_MS = 2000;
    var RETRY_MAX_MS = 60000;
    var DEFAULT_BATCH_SIZE = 25;

    function clone(value) {
        return JSON.parse(JSON.stringify(value));
    }

    function compareEvents(left, right) {
        return Number(left.sequence || 0) - Number(right.sequence || 0)
            || String(left.event_id || "").localeCompare(String(right.event_id || ""));
    }

    function wireEvent(event) {
        var value = clone(event);
        delete value.sync_state;
        delete value.attempt_count;
        delete value.next_retry_at;
        delete value.last_error;
        delete value.last_error_code;
        return value;
    }

    function canonicalValue(value) {
        if (Array.isArray(value)) return value.map(canonicalValue);
        if (value && typeof value === "object") {
            return Object.keys(value).sort().reduce(function (result, key) {
                if (typeof value[key] !== "undefined") result[key] = canonicalValue(value[key]);
                return result;
            }, {});
        }
        return value;
    }

    function sameWireEvent(left, right) {
        return JSON.stringify(canonicalValue(wireEvent(left))) ===
            JSON.stringify(canonicalValue(wireEvent(right)));
    }

    function createLocalStorageAdapter(storage, queueKey) {
        var key = LEGACY_PREFIX + queueKey;
        function read() {
            var parsed = JSON.parse(storage.getItem(key) || "[]");
            if (!Array.isArray(parsed)) throw new Error("Повреждена локальная очередь погрузок.");
            return parsed.sort(compareEvents);
        }
        return {
            kind: "localStorage",
            list: function () { return Promise.resolve(read()); },
            put: function (event) {
                var events = read();
                var index = events.findIndex(function (item) { return item.event_id === event.event_id; });
                if (index >= 0) events[index] = clone(event); else events.push(clone(event));
                storage.setItem(key, JSON.stringify(events.sort(compareEvents)));
                return Promise.resolve(clone(event));
            },
            remove: function (eventId) {
                storage.setItem(key, JSON.stringify(read().filter(function (item) {
                    return item.event_id !== eventId;
                })));
                return Promise.resolve();
            },
            replace: function (events) {
                storage.setItem(key, JSON.stringify(events.slice().sort(compareEvents)));
                return Promise.resolve();
            }
        };
    }

    function openIndexedDb(indexedDB) {
        return new Promise(function (resolve, reject) {
            var request;
            try { request = indexedDB.open(DB_NAME, DB_VERSION); } catch (error) { reject(error); return; }
            request.onupgradeneeded = function () {
                var db = request.result;
                if (!db.objectStoreNames.contains(STORE_NAME)) {
                    var store = db.createObjectStore(STORE_NAME, {keyPath: "storage_id"});
                    store.createIndex("queue_key", "queue_key", {unique: false});
                }
            };
            request.onsuccess = function () { resolve(request.result); };
            request.onerror = function () { reject(request.error || new Error("IndexedDB недоступна.")); };
        });
    }

    function idbRequest(request) {
        return new Promise(function (resolve, reject) {
            request.onsuccess = function () { resolve(request.result); };
            request.onerror = function () { reject(request.error || new Error("Ошибка локального хранилища.")); };
        });
    }

    function createIndexedDbAdapter(indexedDB, queueKey) {
        var dbPromise = openIndexedDb(indexedDB);
        function transaction(mode, operation) {
            return dbPromise.then(function (db) {
                return new Promise(function (resolve, reject) {
                    var tx;
                    try { tx = db.transaction(STORE_NAME, mode); } catch (error) { reject(error); return; }
                    var result;
                    try { result = operation(tx.objectStore(STORE_NAME)); } catch (error) { reject(error); return; }
                    tx.oncomplete = function () { resolve(result); };
                    tx.onerror = function () { reject(tx.error || new Error("Ошибка локального хранилища.")); };
                    tx.onabort = function () { reject(tx.error || new Error("Запись в локальное хранилище отменена.")); };
                });
            });
        }
        return {
            kind: "indexedDB",
            list: function () {
                return dbPromise.then(function (db) {
                    var tx = db.transaction(STORE_NAME, "readonly");
                    var index = tx.objectStore(STORE_NAME).index("queue_key");
                    return idbRequest(index.getAll(queueKey)).then(function (rows) {
                        return rows.map(function (row) {
                            var event = clone(row.event);
                            return event;
                        }).sort(compareEvents);
                    });
                });
            },
            put: function (event) {
                var record = {storage_id: queueKey + ":" + event.event_id, queue_key: queueKey, event: clone(event)};
                return transaction("readwrite", function (store) { store.put(record); return clone(event); });
            },
            remove: function (eventId) {
                return transaction("readwrite", function (store) { store.delete(queueKey + ":" + eventId); });
            },
            replace: function (events) {
                return dbPromise.then(function (db) {
                    return new Promise(function (resolve, reject) {
                        var tx = db.transaction(STORE_NAME, "readwrite");
                        var store = tx.objectStore(STORE_NAME);
                        var index = store.index("queue_key");
                        var cursor = index.openCursor(queueKey);
                        cursor.onsuccess = function () {
                            var current = cursor.result;
                            if (current) { current.delete(); current.continue(); return; }
                            events.forEach(function (event) {
                                store.put({
                                    storage_id: queueKey + ":" + event.event_id,
                                    queue_key: queueKey,
                                    event: clone(event)
                                });
                            });
                        };
                        cursor.onerror = function () { reject(cursor.error); };
                        tx.oncomplete = function () { resolve(); };
                        tx.onerror = function () { reject(tx.error); };
                        tx.onabort = function () { reject(tx.error); };
                    });
                });
            }
        };
    }

    function createOutbox(options) {
        options = options || {};
        var queueKey = String(options.queueKey || options.accessId || "anonymous");
        var fallback = createLocalStorageAdapter(options.localStorage, queueKey);
        var adapterPromise = Promise.resolve(fallback);
        var running = null;
        var retryTimer = null;
        var storageError = null;
        var batchSize = Math.max(1, Number(options.batchSize || DEFAULT_BATCH_SIZE));

        if (options.indexedDB) {
            var indexed = createIndexedDbAdapter(options.indexedDB, queueKey);
            adapterPromise = indexed.list().then(function () {
                return fallback.list().then(function (legacy) {
                    if (!legacy.length) return indexed;
                    return indexed.replace(legacy).then(function () {
                        return fallback.replace([]).then(function () { return indexed; });
                    });
                });
            }).catch(function () { return fallback; });
        }

        function notify(events, extra) {
            if (typeof options.onChange !== "function") return;
            var summary = {
                total: events.length,
                pending: events.filter(function (event) { return event.sync_state === "pending" || event.sync_state === "syncing"; }).length,
                syncing: events.filter(function (event) { return event.sync_state === "syncing"; }).length,
                attention: events.filter(function (event) { return ["conflict", "invalid", "auth_required"].indexOf(event.sync_state) >= 0; }).length,
                storage_failed: !!storageError,
                storage_error: storageError ? String(storageError.message || storageError) : ""
            };
            options.onChange(summary, events.map(clone), extra || {});
        }

        function list() {
            return adapterPromise.then(function (adapter) { return adapter.list(); });
        }

        function persist(event) {
            return adapterPromise.then(function (adapter) { return adapter.put(event); }).catch(function (error) {
                storageError = error;
                return list().catch(function () { return []; }).then(function (events) {
                    notify(events, {reason: "storage_error"});
                    throw error;
                });
            });
        }

        function remove(eventId) {
            return adapterPromise.then(function (adapter) { return adapter.remove(eventId); });
        }

        function queue(event) {
            return list().then(function (events) {
                var existing = events.find(function (item) { return item.event_id === event.event_id; });
                if (existing) {
                    if (!sameWireEvent(existing, event)) {
                        throw new Error("Идентификатор события уже занят другим действием.");
                    }
                    return clone(existing);
                }
                var stored = Object.assign({}, clone(event), {
                    sync_state: "pending",
                    attempt_count: Number(event.attempt_count || 0),
                    next_retry_at: Number(event.next_retry_at || 0),
                    last_error: String(event.last_error || "")
                });
                return persist(stored).then(function () {
                    storageError = null;
                    return list().then(function (next) { notify(next, {reason: "queued", event: clone(stored)}); return clone(stored); });
                });
            });
        }

        function updateEvent(eventId, patch) {
            return list().then(function (events) {
                var event = events.find(function (item) { return item.event_id === eventId; });
                if (!event) return null;
                return persist(Object.assign({}, event, patch)).then(function (updated) { return updated; });
            });
        }

        function retryDelay(attemptCount) {
            return Math.min(RETRY_MAX_MS, RETRY_BASE_MS * Math.pow(2, Math.max(0, Number(attemptCount || 1) - 1)));
        }

        function scheduleRetry(events) {
            if (retryTimer) { root.clearTimeout(retryTimer); retryTimer = null; }
            var now = Date.now();
            var due = events.filter(function (event) {
                return event.sync_state === "pending" && Number(event.next_retry_at || 0) > now;
            }).map(function (event) { return Number(event.next_retry_at); }).sort(function (a, b) { return a - b; })[0];
            if (!due || documentHidden()) return;
            retryTimer = root.setTimeout(function () { retryTimer = null; flush(); }, Math.max(250, due - now));
            if (retryTimer && typeof retryTimer.unref === "function") retryTimer.unref();
        }

        function documentHidden() {
            return !!(root.document && root.document.hidden);
        }

        function markRetry(event, errorText) {
            var attempts = Number(event.attempt_count || 0) + 1;
            return updateEvent(event.event_id, {
                sync_state: "pending",
                attempt_count: attempts,
                next_retry_at: Date.now() + retryDelay(attempts),
                last_error: String(errorText || "Сервер временно недоступен")
            });
        }

        function eligible(events) {
            var now = Date.now();
            var blocked = Object.create(null);
            var batch = [];
            events.forEach(function (event) {
                if (["conflict", "invalid", "auth_required"].indexOf(event.sync_state) >= 0) blocked[event.event_id] = true;
            });
            var ordered = events.slice().sort(compareEvents);
            for (var index = 0; index < ordered.length; index += 1) {
                var event = ordered[index];
                if (event.sync_state !== "pending") continue;
                if (Number(event.next_retry_at || 0) > now) break;
                if ((event.depends_on || []).some(function (dependency) { return blocked[dependency]; })) {
                    blocked[event.event_id] = true;
                    continue;
                }
                batch.push(event);
                if (batch.length >= batchSize) break;
            }
            return batch;
        }

        function markTerminalDependencyConflicts(events) {
            var rejected = Object.create(null);
            events.forEach(function (event) {
                if (event.sync_state === "conflict" || event.sync_state === "invalid") {
                    rejected[event.event_id] = true;
                }
            });
            var affected = [];
            var ordered = events.slice().sort(compareEvents);
            var changed = true;
            while (changed) {
                changed = false;
                ordered.forEach(function (event) {
                    if (
                        event.sync_state === "pending"
                        && !rejected[event.event_id]
                        && (event.depends_on || []).some(function (dependency) { return rejected[dependency]; })
                    ) {
                        rejected[event.event_id] = true;
                        affected.push(event);
                        changed = true;
                    }
                });
            }
            var chain = Promise.resolve();
            affected.forEach(function (event) {
                chain = chain.then(function () {
                    var message = "Предыдущее связанное действие требует сверки.";
                    return updateEvent(event.event_id, {
                        sync_state: "conflict",
                        next_retry_at: 0,
                        last_error_code: "dependency_rejected",
                        last_error: message
                    }).then(function () {
                        if (typeof options.onAttention === "function") {
                            options.onAttention(clone(event), {
                                event_id: event.event_id,
                                status: "conflict",
                                code: "dependency_rejected",
                                message: message
                            });
                        }
                    });
                });
            });
            return chain.then(list);
        }

        function applyResults(sent, payload) {
            var byId = Object.create(null);
            ((payload && payload.results) || []).forEach(function (result) { byId[String(result.event_id || "")] = result; });
            var chain = Promise.resolve();
            sent.forEach(function (event) {
                chain = chain.then(function () {
                    var result = byId[event.event_id];
                    if (!result) return markRetry(event, "Сервер не подтвердил событие.");
                    var status = String(result.status || "retry");
                    if (status === "accepted" || status === "deduplicated") {
                        if (
                            event.event_type === "excavator.trip.loaded"
                            && !(result.server_ids && result.server_ids.trip_id)
                        ) {
                            return markRetry(event, "Сервер не вернул ID созданного рейса.");
                        }
                        return remove(event.event_id).then(list).then(function (remaining) {
                            notify(remaining, {reason: "confirmed", event: clone(event)});
                            if (typeof options.onConfirmed === "function") return options.onConfirmed(clone(event), clone(result));
                        });
                    }
                    if (status === "conflict" || status === "invalid" || status === "auth_required") {
                        return updateEvent(event.event_id, {
                            sync_state: status,
                            last_error: String(result.message || result.error || status),
                            next_retry_at: 0
                        }).then(function () {
                            if (typeof options.onAttention === "function") options.onAttention(clone(event), clone(result));
                        });
                    }
                    return markRetry(event, result.message || result.error || "Повторим отправку позже.");
                });
            });
            return chain;
        }

        function flush() {
            if (running) return running;
            running = list().then(function (events) {
                notify(events, {reason: "flush_start"});
                function drain() {
                    return list().then(markTerminalDependencyConflicts).then(function (current) {
                        var batch = eligible(current);
                        if (!batch.length || documentHidden()) return current;
                        return Promise.all(batch.map(function (event) {
                            return updateEvent(event.event_id, {sync_state: "syncing"});
                        })).then(function () {
                            return list();
                        }).then(function (sending) {
                            notify(sending, {reason: "sending"});
                            var request;
                            try {
                                request = options.send(batch.map(wireEvent));
                            } catch (error) {
                                request = Promise.reject(error);
                            }
                            return Promise.resolve(request).then(function (payload) {
                                return applyResults(batch, payload);
                            }, function (error) {
                                var chain = Promise.resolve();
                                batch.forEach(function (event) {
                                    chain = chain.then(function () { return markRetry(event, error.message || error); });
                                });
                                return chain;
                            });
                        }).then(drain);
                    });
                }
                return drain().then(function () {
                    return list().then(function (remaining) {
                        notify(remaining, {reason: "flush_end"});
                        scheduleRetry(remaining);
                        return remaining;
                    });
                });
            }).finally(function () { running = null; });
            return running;
        }

        function restore(restoreOptions) {
            restoreOptions = restoreOptions || {};
            return list().then(function (events) {
                var chain = Promise.resolve();
                events.forEach(function (event) {
                    if (
                        event.sync_state === "syncing"
                        || (event.sync_state === "auth_required" && restoreOptions.resumeAuthRequired === true)
                    ) {
                        event.sync_state = "pending";
                        event.next_retry_at = 0;
                    }
                    chain = chain.then(function () { return persist(event); });
                });
                return chain.then(function () { notify(events, {reason: "restored"}); scheduleRetry(events); return events.map(clone); });
            });
        }

        function discardUnsent(eventId) {
            return list().then(function (events) {
                var target = events.find(function (event) { return event.event_id === eventId; });
                if (!target || Number(target.attempt_count || 0) > 0 || target.sync_state !== "pending") return false;
                if (events.some(function (event) { return (event.depends_on || []).indexOf(eventId) >= 0; })) return false;
                return remove(eventId).then(function () { return list(); }).then(function (remaining) {
                    notify(remaining, {reason: "discarded"}); return true;
                });
            });
        }

        function retryNow() {
            return list().then(function (events) {
                var chain = Promise.resolve();
                events.forEach(function (event) {
                    if (event.sync_state !== "pending") return;
                    chain = chain.then(function () {
                        return updateEvent(event.event_id, {next_retry_at: 0});
                    });
                });
                return chain.then(flush);
            });
        }

        return {
            ready: restore,
            queue: queue,
            flush: flush,
            pending: list,
            discardUnsent: discardUnsent,
            retryNow: retryNow,
            storageKind: function () { return adapterPromise.then(function (adapter) { return adapter.kind; }); }
        };
    }

    root.createExcavatorFieldOutbox = createOutbox;
    if (typeof module !== "undefined") module.exports = createOutbox;
})(typeof window !== "undefined" ? window : globalThis);
