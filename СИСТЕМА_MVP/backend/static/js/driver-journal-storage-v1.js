(function (root) {
    "use strict";
    // Версия 2 изолирует упакованные записи от прежнего runtime. При живом
    // соединении версии 1 обновление откладывается; исходники не меняются.
    var DB = "field-offline-events-v1", EVENTS = "events", META = "meta", ARCHIVES = "driver_archives";
    function copy(value) { return value === undefined ? undefined : JSON.parse(JSON.stringify(value)); }
    function canonical(value) {
        if (Array.isArray(value)) return value.map(canonical);
        if (value && typeof value === "object") return Object.keys(value).sort().reduce(function (out, key) {
            if (value[key] !== undefined) Object.defineProperty(out, key, {value: canonical(value[key]), enumerable: true});
            return out;
        }, {});
        return value;
    }
    function json(value) { return JSON.stringify(canonical(value)); }
    function fail(code) { throw new Error(code || "driver_archive_corrupt"); }
    async function hash(value) {
        var bytes = new root.TextEncoder().encode(json(value));
        var digest = await root.crypto.subtle.digest("SHA-256", bytes);
        return Array.from(new Uint8Array(digest)).map(function (v) { return v.toString(16).padStart(2, "0"); }).join("");
    }
    // Словарь общих значений полей. В отличие от сводного отчёта эта упаковка
    // восстанавливает каждый исходный конверт и всё состояние доставки.
    function pack(events) {
        var keys = Array.from(new Set(events.flatMap(function (event) { return Object.keys(event); }))).sort();
        var values = [], seen = new Map();
        var rows = events.map(function (event) {
            return keys.map(function (key) {
                if (!Object.prototype.hasOwnProperty.call(event, key)) return -1;
                var value = event[key], encoded = json(value);
                if (encoded === undefined) fail();
                if (!seen.has(encoded)) { seen.set(encoded, values.length); values.push(copy(value)); }
                return seen.get(encoded);
            });
        });
        return {keys: keys, values: values, rows: rows};
    }
    function unpack(data) {
        if (!data || !Array.isArray(data.keys) || !Array.isArray(data.values) || !Array.isArray(data.rows)
            || data.keys.length > 256 || new Set(data.keys).size !== data.keys.length
            || data.keys.some(function (key) { return typeof key !== "string"; })
            || data.rows.length > 10000) fail();
        return data.rows.map(function (row) {
            if (!Array.isArray(row) || row.length !== data.keys.length) fail();
            var event = {};
            row.forEach(function (index, column) {
                if (!Number.isInteger(index) || index < -1 || index >= data.values.length) fail();
                if (index >= 0) Object.defineProperty(event, data.keys[column], {value: copy(data.values[index]), enumerable: true, writable: true});
            });
            if (!event.event_id || event.state !== "confirmed") fail();
            return event;
        });
    }
    async function expand(snapshot) {
        var archives = new Map(), expected = new Map();
        for (var packet of snapshot.archives) {
            var sealed = copy(packet); delete sealed.digest;
            if (packet.schema_version !== 1 || packet.digest !== await hash(sealed)) fail();
            var originals = unpack(packet.data);
            if (!packet.coverage || packet.coverage.event_count !== originals.length
                || json(packet.coverage.manifest.map(function (item) { return item.event_id; }).sort())
                    !== json(originals.map(function (event) { return event.event_id; }).sort())) fail();
            archives.set(packet.archive_id, originals);
            originals.forEach(function (event, index) {
                if (expected.has(event.event_id)) fail();
                expected.set(event.event_id, {archive_id: packet.archive_id, archive_index: index});
            });
        }
        var seen = new Set();
        var events = snapshot.events.map(function (row) {
            var event = row;
            var pointer = expected.get(row.event_id);
            if (pointer && (row.archive_id !== pointer.archive_id || row.archive_index !== pointer.archive_index)) fail();
            if (row.archive_id) {
                var source = archives.get(row.archive_id);
                event = source && source[row.archive_index];
                if (!event || row.event_id !== event.event_id || row.access_id !== event.access_id
                    || row.sequence !== event.sequence || row.state !== "confirmed") fail();
            }
            if (seen.has(event.event_id)) fail();
            seen.add(event.event_id);
            return copy(event);
        });
        // Missing pointers must never silently shorten the driver's journal.
        for (var originals of archives.values()) for (var event of originals) if (!seen.has(event.event_id)) fail();
        return events;
    }
    function eligible(events, identity, target) {
        var api = root.DriverShiftArchive;
        if (!api) return false;
        var originals = api.sources(events, identity, target);
        if (!originals.length || originals.some(function (event) { return event.state !== "confirmed"; })) return false;
        var closed = api.candidates(events, identity).filter(function (item) {
            return item.identity.device_id === target.identity.device_id;
        }).sort(function (a, b) {
            return events.find(function (event) { return event.event_id === a.close_event_id; }).sequence
                - events.find(function (event) { return event.event_id === b.close_event_id; }).sequence;
        });
        var index = closed.findIndex(function (item) { return item.close_event_id === target.close_event_id; });
        if (index < 0) return false;
        var newer = new Set(closed.slice(index + 1)
            .map(function (item) { return item.server_shift_id || item.local_shift_id; }));
        return newer.size >= 2;
    }
    function create(indexedDB, options) {
        options = options || {};
        var database = null, timeoutMs = options.timeoutMs || 2500;
        function open() {
            if (database) return database;
            var blocked = false;
            database = new Promise(function (resolve, reject) {
                var request, expired = false;
                var timer = setTimeout(function () { stop("driver_storage_deadline"); }, timeoutMs);
                function stop(code) { expired = true; clearTimeout(timer); reject(new Error(code)); }
                try { request = indexedDB.open(DB, 2); } catch (error) { clearTimeout(timer); reject(error); return; }
                request.onblocked = function () { blocked = true; stop("driver_storage_upgrade_blocked"); };
                request.onupgradeneeded = function () {
                    if (expired) { request.transaction.abort(); return; }
                    var db = request.result;
                    if (!db.objectStoreNames.contains(EVENTS)) {
                        db.createObjectStore(EVENTS, {keyPath: "event_id"}).createIndex("access_sequence", ["access_id", "sequence"], {unique: false});
                    }
                    if (!db.objectStoreNames.contains(META)) db.createObjectStore(META);
                    if (!db.objectStoreNames.contains(ARCHIVES)) db.createObjectStore(ARCHIVES, {keyPath: "archive_id"});
                };
                request.onerror = function () { clearTimeout(timer); database = null; reject(request.error || new Error("indexeddb_open_failed")); };
                request.onsuccess = function () {
                    clearTimeout(timer);
                    var db = request.result;
                    if (expired) { db.close(); database = null; return; }
                    db.onversionchange = function () { db.close(); database = null; };
                    resolve(db);
                };
            }).catch(function (error) { if (!blocked) database = null; throw error; });
            return database;
        }
        function transaction(stores, mode, operation, isCurrent) {
            return open().then(function (db) {
                return new Promise(function (resolve, reject) {
                    var tx, value, failure, expired = false;
                    var signal = isCurrent && isCurrent.signal;
                    function cancel() {
                        expired = true; failure = new Error("driver_archive_stale");
                        if (tx) try { tx.abort(); } catch (error) {}
                        reject(failure);
                    }
                    function cleanup() { clearTimeout(timer); if (signal) signal.removeEventListener("abort", cancel); }
                    var timer = setTimeout(function () {
                        expired = true; failure = new Error("driver_storage_deadline");
                        if (tx) try { tx.abort(); } catch (error) {}
                        reject(failure);
                    }, timeoutMs);
                    function allowed() { if (expired || (isCurrent && !isCurrent())) fail("driver_archive_stale"); }
                    function guard(fn) { return function () {
                        try { allowed(); return fn.apply(null, arguments); }
                        catch (error) { failure = error; try { tx.abort(); } catch (ignored) {} }
                    }; }
                    try {
                        allowed(); tx = db.transaction(stores, mode);
                        if (signal) { signal.addEventListener("abort", cancel, {once: true}); if (signal.aborted) cancel(); }
                        allowed();
                        tx.oncomplete = function () { cleanup(); resolve(value); };
                        tx.onabort = tx.onerror = function () { cleanup(); reject(failure || tx.error || new Error("indexeddb_transaction_failed")); };
                        operation(tx, function (result) { value = result; }, guard);
                    } catch (error) { cleanup(); if (tx) try { tx.abort(); } catch (ignored) {} reject(error); }
                });
            });
        }
        function readSnapshot() {
            return transaction([EVENTS, ARCHIVES], "readonly", function (tx, done, guard) {
                var value = {}, remaining = 2;
                [[EVENTS, "events"], [ARCHIVES, "archives"]].forEach(function (item) {
                    var request = tx.objectStore(item[0]).getAll();
                    request.onsuccess = guard(function () { value[item[1]] = request.result; if (!--remaining) done(value); });
                });
            });
        }
        async function bounded(work, isCurrent) {
            var expired = false, timer, abort = new root.AbortController();
            function allowed() { return !expired && (!isCurrent || isCurrent()); }
            allowed.signal = abort.signal;
            function cancel() { expired = true; abort.abort(); }
            var signal = isCurrent && isCurrent.signal;
            if (signal) { signal.addEventListener("abort", cancel, {once: true}); if (signal.aborted) cancel(); }
            var deadline = new Promise(function (resolve, reject) {
                timer = setTimeout(function () { cancel(); reject(new Error("driver_storage_deadline")); }, timeoutMs);
            });
            try { return await Promise.race([work(allowed), deadline]); }
            finally { cancel(); clearTimeout(timer); if (signal) signal.removeEventListener("abort", cancel); }
        }
        var repo = {
            kind: "indexedDB",
            list: function () { return bounded(async function () { return expand(await readSnapshot()); }); },
            get: function (id) {
                return bounded(async function () {
                    var snapshot = await transaction([EVENTS, ARCHIVES], "readonly", function (tx, done, guard) {
                        var request = tx.objectStore(EVENTS).get(id);
                        request.onsuccess = guard(function () {
                            var row = request.result;
                            if (!row || !row.archive_id) { done({event: row}); return; }
                            var packet = tx.objectStore(ARCHIVES).get(row.archive_id);
                            packet.onsuccess = guard(function () { done({row: row, packet: packet.result}); });
                        });
                    });
                    if (!snapshot.row) return copy(snapshot.event);
                    var packet = snapshot.packet, sealed = copy(packet);
                    if (!packet) fail();
                    delete sealed.digest;
                    if (packet.schema_version !== 1 || packet.digest !== await hash(sealed)) fail();
                    var event = unpack(packet.data)[snapshot.row.archive_index];
                    if (!event || event.event_id !== id || event.access_id !== snapshot.row.access_id
                        || event.sequence !== snapshot.row.sequence || snapshot.row.state !== "confirmed") fail();
                    return event;
                });
            },
            put: function (event) {
                return transaction([EVENTS], "readwrite", function (tx, done, guard) {
                    var store = tx.objectStore(EVENTS), request = store.get(event.event_id);
                    request.onsuccess = guard(function () {
                        var current = request.result;
                        if (current && current.archive_id) fail("driver_archive_immutable");
                        if (current && current.state === "confirmed" && event.state !== "confirmed") { done(copy(current)); return; }
                        store.put(copy(event)); done(copy(event));
                    });
                });
            },
            remove: function (id) {
                return transaction([EVENTS], "readwrite", function (tx, done, guard) {
                    var store = tx.objectStore(EVENTS), request = store.get(id);
                    request.onsuccess = guard(function () { if (request.result && request.result.archive_id) fail("driver_archive_immutable"); store.delete(id); });
                });
            },
            getMeta: async function (name) {
                var value = await transaction([META], "readonly", function (tx, done, guard) {
                    var request = tx.objectStore(META).get(name); request.onsuccess = guard(function () { done(request.result); });
                });
                if (value === undefined && name.indexOf("event-identity:") === 0) {
                    var event = await repo.get(name.slice(15));
                    if (event && event.state === "confirmed") return event;
                }
                return value;
            },
            setMeta: function (name, value) {
                return transaction([EVENTS, META], "readwrite", function (tx, done, guard) {
                    if (name.indexOf("event-identity:") !== 0) { tx.objectStore(META).put(copy(value), name); return; }
                    var request = tx.objectStore(EVENTS).get(name.slice(15));
                    request.onsuccess = guard(function () {
                        if (!request.result || !request.result.archive_id) tx.objectStore(META).put(copy(value), name);
                    });
                });
            },
            archiveStatus: function (id) {
                return transaction([EVENTS, META], "readonly", function (tx, done, guard) {
                    var request = tx.objectStore(EVENTS).get(id);
                    request.onsuccess = guard(function () {
                        if (request.result && request.result.archive_id) { done({compacted: true}); return; }
                        var skip = tx.objectStore(META).get("archive-skip:" + id);
                        skip.onsuccess = guard(function () { done(skip.result || {}); });
                    });
                });
            },
            compact: function (proof, target, isCurrent) {
                return bounded(async function (allowed) {
                    var api = root.DriverShiftArchive, snapshot = await readSnapshot(), events = await expand(snapshot);
                    if (!allowed() || !api || !eligible(events, target.identity, target)) return false;
                    var coverage = api.verify(proof, events, target), originals = api.sources(events, target.identity, coverage.shift);
                    if (originals.some(function (event) { return event.state !== "confirmed"; })
                        || snapshot.events.some(function (row) { return row.archive_id && originals.some(function (event) { return event.event_id === row.event_id; }); })) return false;
                    var rawSize = new root.TextEncoder().encode(JSON.stringify(originals)).length;
                    var packet = null;
                    if (rawSize <= 8 * 1024 * 1024) {
                        packet = {schema_version: 1, archive_id: target.close_event_id, coverage: coverage, data: pack(originals)};
                        packet.digest = await hash(packet);
                        if (json(unpack(packet.data)) !== json(originals)) fail();
                    }
                    var pointers = originals.map(function (event, index) { return {event_id: event.event_id, access_id: event.access_id,
                        sequence: event.sequence, state: "confirmed", archive_id: target.close_event_id, archive_index: index}; });
                    var packedSize = new root.TextEncoder().encode(JSON.stringify(packet) + JSON.stringify(pointers)).length;
                    var save = packet && packedSize < rawSize;
                    var skipDigest = await api.digest(originals);
                    if (!allowed()) return false;
                    return transaction([EVENTS, META, ARCHIVES], "readwrite", function (tx, done, guard) {
                        var store = tx.objectStore(EVENTS), request = store.getAll();
                        request.onsuccess = guard(function () {
                            // Compare inside the same write transaction: another window,
                            // an ACK, or a new action cancels this attempt without deleting.
                            if (json(request.result) !== json(snapshot.events)) { done(false); return; }
                            if (!save) { tx.objectStore(META).put({source_digest: skipDigest}, "archive-skip:" + target.close_event_id); done(false); return; }
                            tx.objectStore(ARCHIVES).put(packet);
                            pointers.forEach(function (row) { store.put(row); tx.objectStore(META).delete("event-identity:" + row.event_id); });
                            done({compacted: true, event_count: originals.length, before_bytes: rawSize, after_bytes: packedSize});
                        });
                    }, allowed);
                }, isCurrent);
            }
        };
        return repo;
    }
    root.DriverJournalStorage = {create: create, pack: pack, unpack: unpack, eligible: eligible};
    if (typeof module !== "undefined") module.exports = root.DriverJournalStorage;
})(typeof window !== "undefined" ? window : globalThis);
