(function (root) {
    "use strict";
    function copy(value) { return JSON.parse(JSON.stringify(value)); }
    function latestContext(shift) {
        return (shift && shift.events || []).map(function (entry) { return entry.event; })
            .filter(function (event) { return event.event_type === "excavator.work_context.changed"; })
            .sort(function (a, b) { return Number(b.sequence) - Number(a.sequence); })[0] || null;
    }

    // The existing transport remains authoritative for pre-existing server
    // shifts. Only shifts opened by this ledger use the autonomous envelope.
    function create(options) {
        var ledger = options.ledger;
        var transport = options.transport;
        var facade = Object.create(transport || null);
        var serverShiftId = 0;
        function owned() {
            var shift = ledger.currentShift();
            if (shift && shift.status === "closed" && serverShiftId
                && Number(shift.server_shift_id || 0) !== serverShiftId) return null;
            return shift && shift.open_event_id ? shift : null;
        }
        function prepare(raw, expectedId) {
            return ledger.recordPrepared(function (state) {
                var current = state.shifts.find(function (shift) {
                    return shift.local_shift_id === state.current_local_shift_id;
                });
                var opening = raw.event_type === "excavator.shift.opened";
                if (!opening && (!current || current.local_shift_id !== expectedId)) {
                    throw new Error("Смена изменилась в другом окне. Обновите экран.");
                }
                if (opening && String(current && current.local_shift_id || "") !== expectedId) {
                    throw new Error("Смена изменилась в другом окне. Обновите экран.");
                }
                var event = copy(raw);
                event.local_shift_id = opening ? event.event_id : current.local_shift_id;
                event.shift_id = opening ? 0 : Number(current.server_shift_id || 0);
                event.sequence = Math.max(Number(state.next_sequence || 1), Number(raw.sequence || 1), Date.now());
                event.payload = Object.assign({}, event.payload, {local_shift_id: event.local_shift_id});
                // A new opening must not inherit the previous shift's server ID.
                if (opening) delete event.payload.shift_id;
                else if (Object.prototype.hasOwnProperty.call(event.payload, "shift_id")) event.payload.shift_id = event.shift_id;
                var dependencies = (event.depends_on || []).slice();
                if (opening && current && current.close_event_id) dependencies.push(current.close_event_id);
                if (!opening && current.open_event_id) dependencies.push(current.open_event_id);
                var context = latestContext(current);
                var isLoad = event.event_type === "excavator.trip.loaded" || event.event_type === "excavator.free_bucket.loaded";
                if (context && (isLoad || event.event_type === "excavator.work_context.changed")) {
                    dependencies.push(context.event_id);
                    if (isLoad) {
                        var settings = context.payload;
                        if (!(settings.dump_point_ids || []).some(function (id) {
                            return String(id) === String(event.payload.dump_point_id);
                        })) throw new Error("Точка разгрузки изменилась. Выберите действующую точку.");
                        ["rock_type_id", "loading_horizon", "loading_block"].forEach(function (key) {
                            event.payload[key] = settings[key];
                        });
                    }
                }
                if (event.event_type === "excavator.shift.closed") {
                    current.events.forEach(function (entry) { dependencies.push(entry.event.event_id); });
                }
                event.depends_on = dependencies.filter(function (id, index, all) {
                    return id && id !== event.event_id && all.indexOf(id) === index;
                });
                return event;
            });
        }
        facade.pending = function () {
            return ledger.ready().then(function () {
                if (!owned()) return transport.pending();
                return ledger.snapshot().then(function (state) {
                    var result = [];
                    state.shifts.forEach(function (shift) {
                        shift.events.forEach(function (entry) {
                            if (entry.delivery_state !== "confirmed") result.push(copy(entry.event));
                        });
                    });
                    return result;
                });
            });
        };
        facade.allocateSequence = function (floor) {
            return ledger.ready().then(function () {
                return owned() ? ledger.nextSequence() : transport.allocateSequence(floor);
            });
        };
        facade.queue = function (raw) {
            var saved = copy(raw);
            var expected = owned();
            return ledger.ready().then(function () {
                if (!expected) return transport.queue(saved);
                return prepare(saved, expected.local_shift_id);
            });
        };
        facade.discardUnsent = function (eventId) {
            return ledger.getEvent(eventId).then(function (event) {
                // A cancellation is another immutable fact, not deletion.
                return event ? false : transport.discardUnsent(eventId);
            });
        };
        return {
            outbox: facade,
            setServerShift: function (id) { serverShiftId = Number(id || 0); },
            ledger: ledger,
            ready: function () { return ledger.ready(); },
            currentShift: owned,
            workContext: function () {
                var event = latestContext(owned());
                return event ? copy(event) : null;
            },
            saveWorkContext: function (payload, eventId) {
                var shift = owned();
                if (!shift || shift.status !== "open") return Promise.reject(new Error("Сначала начните смену."));
                var saved = copy(payload);
                if (!saved.rock_type_id || !Array.isArray(saved.dump_point_ids) || !saved.dump_point_ids.length) {
                    return Promise.reject(new Error("Выберите породу и хотя бы одну точку разгрузки."));
                }
                saved.dump_point_ids = saved.dump_point_ids.map(String).filter(function (id, index, all) {
                    return id && all.indexOf(id) === index;
                });
                ["loading_horizon", "loading_block"].forEach(function (key) {
                    saved[key] = String(saved[key] || "").replace(/\D/g, "").slice(0, 16);
                });
                var event = Object.assign({}, options.identity, {
                    event_id: eventId, event_type: "excavator.work_context.changed", format_version: 1,
                    equipment_id: Number(shift.equipment_id), occurred_at: new Date().toISOString(),
                    sequence: 1, depends_on: [], payload: saved
                });
                return prepare(event, shift.local_shift_id);
            },
            open: function (payload, eventId) {
                var savedPayload = copy(payload);
                var expected = owned();
                var expectedId = expected ? expected.local_shift_id : "";
                var event = Object.assign({}, options.identity, {
                    event_id: eventId, event_type: "excavator.shift.opened", format_version: 1,
                    equipment_id: Number(payload.excavator_id), occurred_at: new Date().toISOString(),
                    sequence: 1, depends_on: [], payload: savedPayload
                });
                return ledger.ready().then(function () { return prepare(event, expectedId); });
            },
            confirm: function (event, result) {
                return ledger.getEvent(event.event_id).then(function (original) {
                    if (!original || String(original.actor_id) !== String(event.actor_id)
                        || String(original.device_id) !== String(event.device_id)) return false;
                    return ledger.confirm(original, result);
                });
            },
            refresh: function () { return ledger.refresh(); }
        };
    }
    root.createExcavatorAutonomousShift = create;
    if (typeof module !== "undefined") module.exports = create;
})(typeof window !== "undefined" ? window : globalThis);
