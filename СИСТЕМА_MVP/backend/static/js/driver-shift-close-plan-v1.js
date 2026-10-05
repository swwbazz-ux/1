(function (root) {
    "use strict";
    function copy(value) { return JSON.parse(JSON.stringify(value)); }
    function own(event, target) { return event.role_code === "driver" && event.actor_id === target.actor_id
        && event.access_id === target.access_id && event.device_id === target.device_id; }
    function refs(event) {
        return [event.shift_id, event.local_shift_id, (event.payload || {}).local_shift_id,
            ((event.server_result || {}).server_ids || {}).shift_id].filter(Boolean).map(String);
    }
    function sources(events, target) {
        var aliases = new Set(refs(target));
        events.filter(function (event) { return own(event, target) && event.event_type === "driver.shift.opened"; })
            .forEach(function (event) {
                if (refs(event).concat([event.event_id]).some(function (ref) { return aliases.has(ref); })) {
                    refs(event).concat([event.event_id]).forEach(function (ref) { aliases.add(ref); });
                }
            });
        return events.filter(function (event) {
            return own(event, target) && refs(event).some(function (ref) { return aliases.has(ref); });
        }).sort(function (a, b) { return Number(a.sequence) - Number(b.sequence); });
    }
    function assertOpen(events, event) {
        if (event.event_type === "driver.shift.opened") return;
        if (sources(events, event).some(function (item) { return item.event_type === "driver.shift.closed"; })) {
            throw new Error("Смена уже закрыта в другом окне. Обновите экран.");
        }
    }
    function id() {
        if (!root.crypto || !root.crypto.getRandomValues) throw new Error("Не удалось сохранить закрытие. Повторите действие.");
        return "driver-checkpoint:" + Array.from(root.crypto.getRandomValues(new Uint8Array(16)))
            .map(function (value) { return value.toString(16).padStart(2, "0"); }).join("");
    }
    function build(events, closing, options) {
        if (closing.event_type !== "driver.shift.closed") throw new Error("driver_close_type_required");
        var close = copy(closing), originals = sources(events, close);
        var existing = originals.find(function (event) { return event.event_type === "driver.shift.closed"; });
        if (existing) {
            if (["end_fuel", "end_mileage", "end_engine_hours"].some(function (field) {
                return String(existing.payload[field] || "") !== String(close.payload[field] || "");
            })) throw new Error("Смена уже закрыта с другими показаниями. Обновите экран.");
            return {existing: existing};
        }
        if (originals.some(function (event) { return event.equipment_id !== close.equipment_id; })) throw new Error("driver_close_equipment_changed");
        var cancelled = new Set(originals.filter(function (event) { return event.state === "cancelled_locally"; })
            .map(function (event) { return event.event_id; }));
        originals.forEach(function (event) {
            if (!cancelled.has(event.event_id) && (event.depends_on || []).some(function (parent) { return cancelled.has(parent); })) {
                throw new Error("Не удалось восстановить последовательность действий смены.");
            }
        });
        // Locally annihilated facts remain in the journal, but have no server
        // receipts. They must never become impossible checkpoint dependencies.
        var parents = originals.filter(function (event) { return !cancelled.has(event.event_id); })
            .map(function (event) { return event.event_id; });
        var batch = [], localId = close.local_shift_id || close.payload.local_shift_id;
        if (localId && parents.indexOf(localId) < 0) parents.unshift(localId);
        var downtime = originals.filter(function (event) {
            return /^driver\.downtime\./.test(event.event_type) && event.state !== "cancelled_locally";
        }).pop();
        var active = String(options.activeDowntimeId || "");
        if (downtime && downtime.event_type === "driver.downtime.ended") active = "";
        else if (downtime && downtime.event_type === "driver.downtime.started") active = "local:" + downtime.event_id;
        if (active) {
            var localStart = active.indexOf("local:") === 0 ? active.slice(6) : "";
            var end = {
                event_id: id(), event_type: "driver.downtime.ended", occurred_at: close.occurred_at,
                shift_id: close.shift_id, local_shift_id: localId || null, equipment_id: close.equipment_id,
                local_downtime_id: localStart || null, depends_on: localStart ? [localStart] : [],
                payload: {downtime_id: localStart ? null : Number(active), local_downtime_id: localStart || null},
                context_snapshot: {source: "driver_local_shift_close"}
            };
            if (localId) end.payload.local_shift_id = localId;
            batch.push(end); parents.push(end.event_id);
        }
        while (parents.length > 32) {
            var next = [];
            for (var offset = 0; offset < parents.length; offset += 32) {
                var checkpoint = {
                    event_id: id(), event_type: "driver.shift.checkpoint", occurred_at: close.occurred_at,
                    shift_id: close.shift_id, local_shift_id: localId || null, equipment_id: close.equipment_id,
                    depends_on: parents.slice(offset, offset + 32),
                    payload: localId ? {local_shift_id: localId} : {},
                    context_snapshot: {source: "driver_local_shift_close", close_plan_version: 1}
                };
                batch.push(checkpoint); next.push(checkpoint.event_id);
            }
            parents = next;
        }
        close.depends_on = parents;
        if (localId) close.payload.local_shift_id = localId;
        close.context_snapshot.close_plan_version = 1;
        close.context_snapshot.shift_state = copy(options.shiftState || {});
        batch.push(close);
        return batch;
    }
    function restored(events, identity) {
        var mine = events.filter(function (event) { return own(event, identity); });
        var last = mine.filter(function (event) { return /^driver\.shift\.(opened|closed)$/.test(event.event_type); })
            .sort(function (a, b) { return Number(a.sequence) - Number(b.sequence); }).pop();
        if (!last) return null;
        var isClose = last.event_type === "driver.shift.closed";
        var previousClose = !isClose && mine.find(function (event) {
            return event.event_type === "driver.shift.closed" && (last.depends_on || []).indexOf(event.event_id) >= 0;
        });
        if ((isClose ? last : previousClose)?.context_snapshot?.close_plan_version !== 1) return null;
        var result = last.state === "confirmed" ? last.server_result || {} : {};
        var basis = isClose ? copy(last.context_snapshot.shift_state || {}) : {};
        var readings = {};
        ["fuel", "mileage", "engine_hours"].forEach(function (field) {
            var key = (isClose ? "end_" : "start_") + field;
            if (last.payload[key] !== undefined) readings[key] = last.payload[key];
        });
        return Object.assign(basis, {
            local_shift_id: isClose ? (basis.local_shift_id || last.local_shift_id || "server-shift:" + last.shift_id) : last.event_id,
            open_event_id: isClose ? basis.open_event_id || "" : last.event_id,
            server_shift_id: (result.server_ids || {}).shift_id || last.shift_id || basis.server_shift_id || null,
            equipment_id: last.equipment_id, status: isClose ? "closed" : "open",
            opened_at: isClose ? basis.opened_at || "" : last.occurred_at,
            readings: isClose ? basis.readings || {} : readings,
            ...(isClose ? {close_event_id: last.event_id, closed_at: last.occurred_at, end_readings: readings,
                close_confirmed_version: result.version || result.server_version || null}
                : {open_confirmed_version: result.version || result.server_version || null})
        });
    }
    root.DriverShiftClosePlan = {build: build, sources: sources, assertOpen: assertOpen, restored: restored};
    if (typeof module !== "undefined") module.exports = root.DriverShiftClosePlan;
})(typeof window !== "undefined" ? window : globalThis);
