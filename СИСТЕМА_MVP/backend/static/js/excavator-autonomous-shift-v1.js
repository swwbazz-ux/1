(function (root) {
    "use strict";
    function copy(value) { return JSON.parse(JSON.stringify(value)); }
    function reviewClose(payload, basis) {
        function number(value, name, optional) {
            if (optional && (value == null || String(value).trim() === "")) return null;
            var result = Number(String(value == null ? "" : value).trim().replace(/\s/g, "").replace(",", "."));
            if (value == null || String(value).trim() === "" || !Number.isFinite(result) || result < 0
                || result > 99999999 || (!optional && !Number.isInteger(result))) {
                throw new Error(name + ": проверьте показание (от 0 до 99 999 999).");
            }
            return result;
        }
        var fuel = number(payload.fuel, "Топливо");
        var hours = number(payload.engine_hours, "Моточасы");
        var percent = number(payload.fuel_percent, "Топливо, %", true);
        var capacity = number(basis.fuel_capacity_l, "Вместимость бака", true);
        var start = number(basis.start_engine_hours, "Начальные моточасы", true);
        if (!capacity) throw new Error("Вместимость топливного бака не настроена.");
        if (percent !== null && (!Number.isInteger(percent) || Math.round(capacity * percent / 100) !== fuel)) {
            throw new Error("Проценты и рассчитанные литры не совпадают.");
        }
        var warnings = [];
        function warning(code, field, title, message) { warnings.push({code: code, field: field, title: title, message: message}); }
        if (fuel > capacity) warning("fuel_above_capacity", "fuel", "Топливо выше вместимости бака",
            "Вместимость бака: " + capacity + " л. Введено: " + fuel + " л. Превышение: " + (fuel - capacity) + " л.");
        if (start === null) warning("engine_hours_start_missing", "engine_hours", "Нет начального показания моточасов",
            "Введено на конец: " + hours + " м/ч; сравнить разницу невозможно.");
        else if (hours < start) warning("engine_hours_decreased", "engine_hours", "Моточасы меньше начального показания",
            "На начало: " + start + " м/ч. Введено: " + hours + " м/ч. Разница: " + (hours - start) + " м/ч.");
        else if (hours - start > 12) warning("engine_hours_delta_high", "engine_hours", "Моточасы за смену выросли больше чем на 12",
            "На начало: " + start + " м/ч. Введено: " + hours + " м/ч. Разница: " + (hours - start) + " м/ч.");
        return {warnings: warnings, confirmation: {
            version: 1, accepted: true, shift_ref: String(basis.shift_ref), equipment_id: Number(basis.equipment_id),
            fuel: String(fuel), fuel_percent: percent === null ? null : String(percent), engine_hours: String(hours),
            start_engine_hours: start === null ? null : String(start), fuel_capacity_l: String(capacity),
            warning_codes: warnings.map(function (item) { return item.code; })
        }};
    }
    function latestContext(shift) {
        return (shift && shift.events || []).map(function (entry) { return entry.event; })
            .filter(function (event) { return event.event_type === "excavator.work_context.changed"; })
            .sort(function (a, b) { return Number(b.sequence) - Number(a.sequence); })[0] || null;
    }

    function closeBatch(event) {
        var batch = [];
        var dependencies = event.depends_on;
        // Preserve every reference, including already confirmed originals.
        // A receipt for each group proves all its parents were accepted. Build
        // another level when necessary; never truncate a long shift's history.
        while (dependencies.length > 32) {
            var parents = [];
            for (var offset = 0; offset < dependencies.length; offset += 32) {
                if (!root.crypto || typeof root.crypto.getRandomValues !== "function") {
                    throw new Error("Не удалось создать ID закрытия. Повторите действие.");
                }
                var bytes = root.crypto.getRandomValues(new Uint8Array(16));
                var id = "exc-checkpoint:" + Array.from(bytes, function (value) {
                    return value.toString(16).padStart(2, "0");
                }).join("");
                batch.push({
                    event_id: id, event_type: "excavator.shift.checkpoint", format_version: 1,
                    actor_id: event.actor_id, access_id: event.access_id, role_code: event.role_code,
                    device_id: event.device_id, equipment_id: event.equipment_id,
                    shift_id: event.shift_id, local_shift_id: event.local_shift_id,
                    occurred_at: event.occurred_at, sequence: event.sequence++,
                    depends_on: dependencies.slice(offset, offset + 32),
                    payload: {local_shift_id: event.local_shift_id}
                });
                parents.push(id);
            }
            dependencies = parents;
        }
        event.depends_on = dependencies;
        batch.push(event);
        return batch;
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
            return ledger.recordPreparedBatch(function (state) {
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
                    if (event.payload.reading_confirmation) {
                        var review = reviewClose(event.payload, {
                            shift_ref: "local:" + current.local_shift_id, equipment_id: current.equipment_id,
                            start_engine_hours: current.readings.engine_hours,
                            fuel_capacity_l: event.payload.fuel_capacity_l
                        });
                        if (JSON.stringify(review.confirmation) !== JSON.stringify(event.payload.reading_confirmation)) {
                            throw new Error("Показания или смена изменились. Подтвердите закрытие ещё раз.");
                        }
                    }
                    current.events.forEach(function (entry) { dependencies.push(entry.event.event_id); });
                }
                event.depends_on = dependencies.filter(function (id, index, all) {
                    return id && id !== event.event_id && all.indexOf(id) === index;
                });
                return event.event_type === "excavator.shift.closed" ? closeBatch(event) : [event];
            }).then(function (batch) { return batch[batch.length - 1]; });
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
            return ledger.hasEvent(eventId).then(function (event) {
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
    root.reviewExcavatorShiftClose = reviewClose;
    create.reviewClose = reviewClose;
    if (typeof module !== "undefined") module.exports = create;
})(typeof window !== "undefined" ? window : globalThis);
