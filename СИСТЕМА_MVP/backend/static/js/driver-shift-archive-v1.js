(function (root) {
    "use strict";
    // Coverage stays separate from delivery. Older closed shifts may be packed
    // losslessly only after a fresh full server comparison and an atomic recheck.
    var PREFIX = "driver-shift-archive-v1:";
    var DELIVERY_FIELDS = ["state", "attempt_count", "next_retry_at", "last_error", "updated_at",
        "created_session", "created_mono", "auth_generation", "server_result", "server_received_at", "sent_live"];
    function copy(value) { return JSON.parse(JSON.stringify(value)); }
    function canonical(value) {
        if (Array.isArray(value)) return value.map(canonical);
        if (value && typeof value === "object") return Object.keys(value).sort().reduce(function (result, key) {
            if (value[key] !== undefined) result[key] = canonical(value[key]);
            return result;
        }, {});
        return value;
    }
    function same(a, b) { return JSON.stringify(canonical(a)) === JSON.stringify(canonical(b)); }
    function wire(event) {
        var value = copy(event);
        DELIVERY_FIELDS.forEach(function (key) { delete value[key]; });
        return value;
    }
    function positive(value) { return Number.isSafeInteger(value) && value > 0; }
    function date(value) { return typeof value === "string" && Number.isFinite(Date.parse(value)); }
    function microseconds(value) {
        // Django keeps six fractional digits; Date.parse keeps three. Preserve
        // the remainder before flooring a downtime duration to whole seconds.
        var fraction = /\.(\d+)(?:Z|[+-]\d{2}:\d{2})$/.exec(value);
        return Date.parse(value) * 1000 + Number(((fraction ? fraction[1] : "") + "000000").slice(3, 6));
    }
    function own(event, identity) {
        return event && event.role_code === "driver" && event.actor_id === identity.actor_id
            && event.access_id === identity.access_id && event.device_id === identity.device_id;
    }
    function localId(event) { return event.local_shift_id || (event.payload || {}).local_shift_id || ""; }
    function serverId(event) { return event.shift_id || ((event.server_result || {}).server_ids || {}).shift_id || null; }
    function sources(events, identity, shift) {
        return events.filter(function (event) {
            return own(event, identity) && ((shift.local_shift_id && (localId(event) === shift.local_shift_id
                || event.event_id === shift.local_shift_id))
                || (shift.server_shift_id && serverId(event) === shift.server_shift_id));
        }).sort(function (a, b) { return a.sequence - b.sequence; });
    }
    function candidates(events, identity) {
        return events.filter(function (event) {
            return event.event_type === "driver.shift.closed" && event.role_code === "driver"
                && event.actor_id === identity.actor_id && event.access_id === identity.access_id && event.device_id;
        }).map(function (close) {
            var id = serverId(close), local = localId(close);
            var opening = events.find(function (event) {
                return event.event_type === "driver.shift.opened" && event.device_id === close.device_id
                    && event.actor_id === close.actor_id && event.access_id === close.access_id
                    && ((local && event.event_id === local) || (id && serverId(event) === id));
            });
            return {local_shift_id: opening ? opening.event_id : local, server_shift_id: id,
                close_event_id: close.event_id, equipment_id: close.equipment_id,
                identity: {actor_id: close.actor_id, access_id: close.access_id, role_code: "driver", device_id: close.device_id}};
        });
    }
    async function hash(value) {
        var bytes = new root.TextEncoder().encode(JSON.stringify(canonical(value)));
        var value = await root.crypto.subtle.digest("SHA-256", bytes);
        return Array.from(new Uint8Array(value)).map(function (byte) { return byte.toString(16).padStart(2, "0"); }).join("");
    }
    function digest(events) { return hash(events.map(function (event) {
        return {event: wire(event), cancelled_locally: event.state === "cancelled_locally"};
    })); }
    function readProof(storage, key) {
        try {
            var value = JSON.parse(storage.getItem(key) || "null");
            return value && value.schema_version === 1 && value.shift && value.identity
                && /^[a-f0-9]{64}$/.test(value.source_digest || "") && /^[a-f0-9]{64}$/.test(value.proof_digest || "") ? value : null;
        } catch (error) { return null; } // Only the derived proof is replaceable.
    }
    function verify(proof, events, candidate) {
        function invalid() { throw new Error("driver_archive_unproven"); }
        function money(value) {
            if (typeof value !== "string" || !/^\d+(\.\d{1,2})?$/.test(value)) invalid();
            var parts = value.split("."), result = Number(parts[0]) * 100 + Number((parts[1] || "").padEnd(2, "0"));
            if (!Number.isSafeInteger(result)) invalid();
            return result;
        }
        var shift = proof && proof.shift;
        if (!proof || proof.schema_version !== 1 || !same(proof.identity, candidate.identity)
            || !/^[a-f0-9]{64}$/.test(proof.snapshot_id || "") || !date(proof.generated_at)
            || !shift || !positive(shift.server_shift_id) || !date(shift.opened_at) || !date(shift.closed_at)
            || Date.parse(shift.closed_at) < Date.parse(shift.opened_at)
            || shift.close_event_id !== candidate.close_event_id || shift.equipment_id !== candidate.equipment_id
            || (candidate.server_shift_id && shift.server_shift_id !== candidate.server_shift_id)
            || (candidate.local_shift_id && shift.local_shift_id !== candidate.local_shift_id)
            || shift.open_event_id !== shift.local_shift_id || !Array.isArray(proof.entries)) invalid();
        var local = sources(events, candidate.identity, shift);
        if (proof.event_count !== local.length || proof.entries.length !== local.length || !local.length
            || local.some(function (event) { return event.state === "cancelled_locally"; })) invalid();
        var originals = new Map(local.map(function (event) { return [event.event_id, event]; }));
        var seen = new Set(), trips = new Map(), stops = new Map();
        proof.entries.forEach(function (entry) {
            var event = entry && entry.event, ids = entry && entry.result && entry.result.server_ids;
            if (!event || seen.has(event.event_id) || !originals.has(event.event_id) || entry.status !== "accepted"
                || !positive(entry.receipt_id) || !/^[a-f0-9]{64}$/.test(entry.fingerprint || "")
                || !ids || ids.event_receipt_id !== entry.receipt_id
                || (ids.shift_id && ids.shift_id !== shift.server_shift_id)
                || !same(wire(event), wire(originals.get(event.event_id)))) invalid();
            seen.add(event.event_id);
            if (/^driver\.shift\.(opened|closed)$/.test(event.event_type) && ids.shift_id !== shift.server_shift_id) invalid();
            var trip = entry.trip_fact, stop = entry.downtime_fact;
            if (event.event_type.indexOf("driver.trip.") === 0) {
                if (!trip || trip.trip_id !== ids.trip_id || !positive(trip.trip_id) || trip.truck_id !== shift.equipment_id
                    || !date(trip.loaded_at) || !(trip.completed_at === null || date(trip.completed_at))
                    || !(trip.cancelled_at === null || date(trip.cancelled_at)) || typeof trip.is_carryover !== "boolean"
                    || ["active", "loaded_waiting_unload", "completed", "uncontrolled", "cancelled"].indexOf(trip.status) < 0
                    || (trip.status === "completed" && !date(trip.completed_at))
                    || ![trip.driver_control_shift_id, trip.unloading_shift_id, trip.credited_shift_id].every(function (id) {
                        return id === null || positive(id);
                    }) || !positive(trip.excavator_id) || typeof trip.excavator !== "string"
                    || typeof trip.dump_point !== "string" || !(trip.dump_point_id === null || positive(trip.dump_point_id))) invalid();
                if (trip.volume_m3 !== null) money(trip.volume_m3);
                if (trips.has(trip.trip_id) && !same(trips.get(trip.trip_id), trip)) invalid();
                trips.set(trip.trip_id, trip);
            } else if (trip != null) invalid();
            if (event.event_type.indexOf("driver.downtime.") === 0) {
                if (!stop || !positive(stop.downtime_id) || stop.downtime_id !== (ids.downtime_event_id || ids.downtime_id)
                    || stop.equipment_id !== shift.equipment_id || !positive(stop.reason_id) || typeof stop.reason !== "string"
                    || !date(stop.started_at) || !(stop.ended_at === null || date(stop.ended_at))) invalid();
                var seconds = Math.max(0, Math.floor((Math.min(microseconds(stop.ended_at || shift.closed_at), microseconds(shift.closed_at))
                    - Math.max(microseconds(stop.started_at), microseconds(shift.opened_at))) / 1000000));
                if (stop.shift_seconds !== seconds || (stops.has(stop.downtime_id) && !same(stops.get(stop.downtime_id), stop))) invalid();
                stops.set(stop.downtime_id, stop);
            } else if (stop != null) invalid();
        });
        if (!seen.has(shift.close_event_id) || (shift.open_event_id && !seen.has(shift.open_event_id))) invalid();
        if (originals.get(shift.close_event_id).event_type !== "driver.shift.closed"
            || (shift.open_event_id && originals.get(shift.open_event_id).event_type !== "driver.shift.opened")) invalid();
        var projection = proof.projection, facts = Array.from(trips.values());
        var credited = facts.filter(function (trip) { return trip.status === "completed" && trip.credited_shift_id === shift.server_shift_id; });
        if (!projection || !same((projection.source_event_ids || []).slice().sort(), Array.from(seen).sort())
            || !same(projection.source_trip_ids, Array.from(trips.keys()).sort(function (a, b) { return a - b; }))
            || !same(projection.source_downtime_ids, Array.from(stops.keys()).sort(function (a, b) { return a - b; }))
            || projection.completed_trip_count !== facts.filter(function (trip) { return trip.status === "completed"; }).length
            || projection.cancelled_trip_count !== facts.filter(function (trip) { return trip.status === "cancelled"; }).length
            || projection.credited_trip_count !== credited.length
            || money(projection.credited_volume_m3) !== credited.reduce(function (sum, trip) { return sum + (trip.volume_m3 === null ? 0 : money(trip.volume_m3)); }, 0)
            || projection.unknown_volume_trip_count !== credited.filter(function (trip) { return trip.volume_m3 === null; }).length
            || projection.downtime_seconds !== Array.from(stops.values()).reduce(function (sum, stop) { return sum + stop.shift_seconds; }, 0)) invalid();
        return {schema_version: 1, snapshot_id: proof.snapshot_id, generated_at: proof.generated_at,
            identity: copy(proof.identity), shift: copy(shift), event_count: local.length, projection: copy(projection),
            trip_facts: copy(facts), downtime_facts: copy(Array.from(stops.values())),
            manifest: proof.entries.map(function (entry) { return {event_id: entry.event.event_id,
                receipt_id: entry.receipt_id, fingerprint: entry.fingerprint}; })};
    }
    function create(options) {
        var inFlight = null, stopped = false, timer = null, lastId = "", retryMs = 5000, hasWork = false;
        function current() { return !stopped && (!options.isCurrent || options.isCurrent()); }
        function key(candidate) { return PREFIX + JSON.stringify([candidate.identity, candidate.close_event_id]); }
        function refresh() {
            if (inFlight) return inFlight;
            if (!current() || (root.navigator && root.navigator.onLine === false)) return Promise.resolve(false);
            hasWork = true;
            var expired = false, timeout, abort = root.AbortController ? new root.AbortController() : null;
            function allowed() { if (expired || !current()) throw new Error("driver_archive_stale"); }
            var deadline = new Promise(function (resolve, reject) {
                timeout = setTimeout(function () { expired = true; if (abort) abort.abort(); reject(new Error("driver_archive_deadline")); }, options.timeoutMs || 12000);
            });
            var work = (async function () {
                var events = await options.outbox.journal();
                allowed();
                var pending = [];
                for (var candidate of candidates(events, options.identity)) {
                    var saved = readProof(options.storage, key(candidate));
                    var sealed = saved && copy(saved);
                    if (sealed) delete sealed.proof_digest;
                    if (saved && same(saved.identity, candidate.identity) && saved.shift.close_event_id === candidate.close_event_id
                        && saved.proof_digest === await hash(sealed)
                        && saved.source_digest === await digest(sources(events, candidate.identity, saved.shift))) {
                        var status = options.outbox.archiveStatus ? await options.outbox.archiveStatus(candidate.close_event_id) : {unsupported: true};
                        if (status.unsupported || status.compacted || status.source_digest === saved.source_digest
                            || !root.DriverJournalStorage || !root.DriverJournalStorage.eligible(events, candidate.identity, candidate)) continue;
                    }
                    pending.push(candidate);
                }
                allowed();
                hasWork = pending.length > 0;
                if (!hasWork) return false;
                var index = pending.findIndex(function (item) { return item.close_event_id === lastId; });
                var target = pending[(index + 1) % pending.length];
                lastId = target.close_event_id;
                var proof = null, entries = [], offset = 0;
                do {
                    allowed();
                    var url = new URL(options.url, root.location && root.location.href || "https://localhost/");
                    url.searchParams.set("device_id", target.identity.device_id);
                    url.searchParams.set("close_event_id", target.close_event_id);
                    if (target.local_shift_id) url.searchParams.set("local_shift_id", target.local_shift_id);
                    if (target.server_shift_id) url.searchParams.set("server_shift_id", String(target.server_shift_id));
                    url.searchParams.set("offset", String(offset));
                    if (proof) url.searchParams.set("snapshot_id", proof.snapshot_id);
                    var response = await options.fetch(url.toString(), {credentials: "same-origin", cache: "no-store",
                        headers: {"Accept": "application/json"}, signal: abort && abort.signal});
                    allowed();
                    if (!response.ok) throw new Error("driver_archive_not_ready");
                    var body = await response.json();
                    allowed();
                    if (!body || body.ok !== true || body.schema_version !== 1 || body.offset !== offset
                        || !positive(body.event_count) || body.event_count > 10000
                        || !Array.isArray(body.entries) || !body.entries.length || body.entries.length > 100
                        || (proof && (body.snapshot_id !== proof.snapshot_id || body.event_count !== proof.event_count
                            || !same(body.shift, proof.shift) || !same(body.identity, proof.identity) || !same(body.projection, proof.projection)))) throw new Error("driver_archive_page_invalid");
                    if (!proof) proof = body;
                    entries = entries.concat(body.entries);
                    if (entries.length > proof.event_count || (body.next_offset !== null
                        && (body.next_offset !== entries.length || entries.length >= proof.event_count))) throw new Error("driver_archive_cursor_invalid");
                    offset = body.next_offset;
                } while (offset !== null);
                if (entries.length !== proof.event_count) throw new Error("driver_archive_incomplete");
                proof = Object.assign({}, proof, {entries: entries});
                // A new fact or another window must be included before proof is saved.
                events = await options.outbox.journal();
                allowed();
                var coverage = verify(proof, events, target);
                coverage.source_digest = await digest(sources(events, target.identity, coverage.shift));
                coverage.proof_digest = await hash(coverage);
                var latest = await options.outbox.journal();
                if (coverage.source_digest !== await digest(sources(latest, target.identity, coverage.shift))) throw new Error("driver_archive_sources_changed");
                allowed();
                var previous = readProof(options.storage, key(target));
                if (previous && Date.parse(previous.generated_at) > Date.parse(coverage.generated_at)) throw new Error("driver_archive_older_snapshot");
                options.storage.setItem(key(target), JSON.stringify(coverage));
                if (typeof options.outbox.compactArchive === "function") {
                    var stillCurrent = function () { return !expired && current(); };
                    stillCurrent.signal = abort && abort.signal;
                    await options.outbox.compactArchive(proof, target, stillCurrent);
                }
                return coverage;
            })();
            inFlight = Promise.race([work, deadline]).catch(function () { return false; }).finally(function () {
                expired = true; clearTimeout(timeout); inFlight = null;
            });
            return inFlight;
        }
        function schedule(delay) {
            if (!current()) return;
            clearTimeout(timer);
            timer = setTimeout(function () {
                timer = null;
                refresh().then(function (result) {
                    if (!current() || (root.navigator && root.navigator.onLine === false)) return;
                    if (result) { retryMs = 5000; schedule(); }
                    else if (hasWork) { schedule(retryMs); retryMs = Math.min(retryMs * 2, 60000); }
                });
            }, typeof delay === "number" ? delay : 1000);
        }
        return {refresh: refresh, schedule: schedule, stop: function () { stopped = true; clearTimeout(timer); }};
    }
    var active = null, activeOutbox = null, bound = false;
    function bind(outbox) {
        if (!outbox || typeof outbox.journal !== "function" || !root.document) return;
        if (active && outbox === activeOutbox) { active.schedule(); return; }
        if (active) active.stop();
        var shell = root.document.querySelector("[data-driver-shell]");
        if (!shell) return;
        var identity = {actor_id: Number(shell.dataset.driverActorId), access_id: Number(shell.dataset.driverAccessId)};
        var storage;
        try { storage = root.localStorage; } catch (error) { return; }
        if (!storage) return;
        activeOutbox = outbox;
        active = create({outbox: outbox, storage: storage, identity: identity,
            url: shell.dataset.driverShiftArchiveUrl, fetch: root.fetch.bind(root), isCurrent: function () {
                var currentShell = root.document.querySelector("[data-driver-shell]");
                return root.driverOfflineOutbox === outbox && currentShell
                    && Number(currentShell.dataset.driverActorId) === identity.actor_id
                    && Number(currentShell.dataset.driverAccessId) === identity.access_id
                    && !(root.isAppRoleReadonly && root.isAppRoleReadonly());
            }});
        active.schedule();
        if (!bound) {
            bound = true;
            root.addEventListener("online", scheduleActive);
            root.addEventListener("focus", scheduleActive);
            root.document.addEventListener("visibilitychange", function () { if (!root.document.hidden) scheduleActive(); });
        }
    }
    function scheduleActive() { if (active) active.schedule(); }
    root.DriverShiftArchive = {create: create, verify: verify, candidates: candidates, digest: digest,
        sources: sources, bind: bind, schedule: scheduleActive};
    if (typeof module !== "undefined") module.exports = root.DriverShiftArchive;
})(typeof window !== "undefined" ? window : globalThis);
