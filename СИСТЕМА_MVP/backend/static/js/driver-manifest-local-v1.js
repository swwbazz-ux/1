/* Путёвка водителя на телефоне: рейсы и простои смены видны сразу, без
   интернета и без сервера (владелец, 30.09.2026: «путёвка должна
   наполняться рейсами и простоями независимо от интернета и сервера»).

   Раньше путёвку рисовал только сервер: без связи она стояла пустой, а
   после перезапуска без сети показывала то, что было в закэшированной
   странице. Теперь телефон ведёт свой журнал смены: каждое действие из
   очереди отправки (driver-offline-outbox-v2.js) записывается сюда в момент
   нажатия и хранится в localStorage, пока смена не закрыта больше 7 дней
   назад. Подтверждение сервера добавляет к записи серверные ID, отказ —
   пометку «не принято сервером» (такие строки не удаляются сами).

   Показ — сведение журнала с данными сервера (driver-manifest-data во
   фрагменте экрана, users/views.py driver_manifest_payload): одна строка на
   рейс или простой, сопоставление по серверному ID или по ID события
   телефона. Время строки — время нажатия на телефоне; серверное — только
   если сервер доказанно поправил часы телефона (device_clock_adjusted), и
   тогда строка помечена «время сервера». */
(function (root) {
    "use strict";

    var KEY_PREFIX = "driver-manifest-local-v1:";
    var RETENTION_MS = 7 * 24 * 60 * 60 * 1000;
    var REJECTED_STATES = {conflict: true, invalid: true};
    var TRIP_EVENTS = {
        "driver.trip.loaded": true,
        "driver.trip.loaded.cancelled": true,
        "driver.trip.manual_completed": true,
        "driver.trip.unloaded": true,
        "driver.trip.dump_point_changed": true
    };
    var DOWNTIME_EVENTS = {"driver.downtime.started": true, "driver.downtime.ended": true};
    var SHIFT_EVENTS = {"driver.shift.opened": true, "driver.shift.closed": true};

    function text(value) { return value === null || value === undefined ? "" : String(value).trim(); }
    function positive(value) {
        var parsed = Number(value);
        return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
    }
    function stamp(value) {
        var parsed = Date.parse(text(value));
        return Number.isFinite(parsed) ? parsed : null;
    }
    function escapeHtml(value) {
        return text(value).replace(/[&<>"']/g, function (ch) {
            return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[ch];
        });
    }

    /* Ключи смены: серверный ID ("s:12") и местный ID открытия без связи
       ("l:driver-shift-open:…"). "server-shift:12" — местная запись смены,
       открытой на сервере и закрытой на телефоне (driver-local-shift-v1.js). */
    function shiftAlias(value) {
        var raw = text(value);
        if (!raw) return "";
        if (/^\d+$/.test(raw)) return positive(raw) ? "s:" + Number(raw) : "";
        if (raw.indexOf("server-shift:") === 0) return shiftAlias(raw.slice(13));
        if (raw.indexOf("s:") === 0 || raw.indexOf("l:") === 0) return raw;
        return "l:" + raw;
    }
    function eventShiftAliases(event) {
        return [shiftAlias(event && event.shift_id), shiftAlias(event && event.local_shift_id)].filter(Boolean);
    }

    /* ---- Журнал ---- */

    function emptyJournal() { return {version: 1, shifts: []}; }

    function readJournal(storage, accessId) {
        if (!storage || !accessId) return emptyJournal();
        try {
            var value = JSON.parse(storage.getItem(KEY_PREFIX + accessId) || "null");
            if (value && Array.isArray(value.shifts)) return value;
        } catch (error) {}
        return emptyJournal();
    }

    function shiftActivity(shift) {
        return Math.max(stamp(shift.closed_at) || 0, stamp(shift.updated_at) || 0, stamp(shift.opened_at) || 0);
    }

    function prune(journal, now, keepAliases) {
        journal.shifts = journal.shifts.filter(function (shift) {
            if (keepAliases && shift.aliases.some(function (alias) { return keepAliases.indexOf(alias) >= 0; })) return true;
            return now - shiftActivity(shift) <= RETENTION_MS;
        });
        return journal;
    }

    function writeJournal(storage, accessId, journal, now) {
        if (!storage || !accessId) return false;
        var payload = JSON.stringify(journal);
        try {
            storage.setItem(KEY_PREFIX + accessId, payload);
            return true;
        } catch (error) {
            /* Места нет: убираем самые старые смены, пока запись не пройдёт.
               Текущую смену (последнюю по активности) не трогаем никогда. */
            var ordered = journal.shifts.slice().sort(function (a, b) { return shiftActivity(a) - shiftActivity(b); });
            while (ordered.length > 1) {
                var oldest = ordered.shift();
                journal.shifts = journal.shifts.filter(function (shift) { return shift !== oldest; });
                try {
                    storage.setItem(KEY_PREFIX + accessId, JSON.stringify(journal));
                    return true;
                } catch (retryError) {}
            }
            return false;
        }
    }

    function findShift(journal, aliases) {
        return journal.shifts.filter(function (shift) {
            return shift.aliases.some(function (alias) { return aliases.indexOf(alias) >= 0; });
        });
    }

    /* Одна смена может быть известна под двумя ключами: местным (открыта без
       связи) и серверным (после подтверждения). Все записи под любым из них —
       одна смена. */
    function ensureShift(journal, aliases, now) {
        aliases = aliases.filter(Boolean);
        if (!aliases.length) return null;
        var found = findShift(journal, aliases);
        var target = found[0];
        if (!target) {
            target = {aliases: [], opened_at: null, closed_at: null, updated_at: new Date(now).toISOString(), log: [], server: null};
            journal.shifts.push(target);
        }
        found.slice(1).forEach(function (other) {
            other.log.forEach(function (entry) { upsertLog(target, entry); });
            if (other.server && (!target.server || Number(other.server.version || 0) > Number(target.server.version || 0))) {
                target.server = other.server;
            }
            other.aliases.forEach(function (alias) { if (target.aliases.indexOf(alias) < 0) target.aliases.push(alias); });
            if (other.opened_at && (!target.opened_at || stamp(other.opened_at) < stamp(target.opened_at))) target.opened_at = other.opened_at;
            if (other.closed_at && (!target.closed_at || stamp(other.closed_at) > stamp(target.closed_at))) target.closed_at = other.closed_at;
            journal.shifts = journal.shifts.filter(function (shift) { return shift !== other; });
        });
        aliases.forEach(function (alias) { if (target.aliases.indexOf(alias) < 0) target.aliases.push(alias); });
        return target;
    }

    function upsertLog(shift, entry) {
        var existing = shift.log.find(function (item) { return item.event_id === entry.event_id; });
        if (!existing) {
            shift.log.push(entry);
            return true;
        }
        var before = JSON.stringify(existing);
        /* Подтверждение сервера окончательно: более поздний снимок очереди со
           старым состоянием его не отменяет. */
        var keepConfirmed = existing.status === "confirmed" && entry.status !== "confirmed";
        Object.keys(entry).forEach(function (key) {
            if (entry[key] === undefined) return;
            if (keepConfirmed && (key === "status" || key === "reject")) return;
            existing[key] = entry[key];
        });
        return JSON.stringify(existing) !== before;
    }

    function statusOf(event) {
        var state = text(event && event.state);
        if (state === "confirmed") return "confirmed";
        if (REJECTED_STATES[state]) return "rejected";
        return "pending";
    }

    function logEntry(event, reasonLabel) {
        var payload = event.payload || {};
        var snapshot = event.context_snapshot || {};
        var entry = {
            event_id: text(event.event_id),
            event_type: text(event.event_type),
            sequence: Number(event.sequence || 0),
            occurred_at: text(event.occurred_at),
            trip_id: positive(event.trip_id) || positive(payload.trip_id),
            local_trip_id: text(event.local_trip_id),
            local_downtime_id: text(event.local_downtime_id) || text(payload.local_downtime_id),
            downtime_id: positive(payload.downtime_id),
            excavator_id: positive(payload.excavator_id),
            excavator_label: text(snapshot.excavator_label),
            dump_point_id: positive(payload.dump_point_id),
            dump_point_label: text(snapshot.selected_dump_point_name),
            reason_id: positive(payload.reason_id),
            reason_label: text(reasonLabel),
            status: statusOf(event)
        };
        if (entry.status === "rejected") {
            entry.reject = {
                code: text(event.last_error && event.last_error.code),
                message: text(event.last_error && event.last_error.message)
            };
        }
        return entry;
    }

    /* ---- Сведение журнала в строки путёвки ---- */

    function replay(log) {
        var trips = [];
        var downtimes = [];
        function tripFor(entry) {
            return trips.find(function (trip) {
                return (entry.local_trip_id && trip.local_trip_id === entry.local_trip_id)
                    || (entry.trip_id && trip.trip_id === entry.trip_id)
                    || (entry.local_trip_id && trip.local_ids.indexOf(entry.local_trip_id) >= 0);
            });
        }
        function touch(row, entry) {
            if (row.local_ids.indexOf(entry.event_id) < 0) row.local_ids.push(entry.event_id);
            if (entry.status === "rejected") {
                row.rejected = true;
                row.reject = entry.reject || row.reject;
            } else if (entry.status === "pending") {
                row.pending = true;
            }
        }
        function timeOf(entry) {
            return entry.clock_adjusted && entry.effective_at ? entry.effective_at : entry.occurred_at;
        }
        function openDowntime() {
            for (var index = downtimes.length - 1; index >= 0; index -= 1) {
                if (!downtimes[index].ended_at) return downtimes[index];
            }
            return null;
        }
        var ordered = log.slice().sort(function (a, b) {
            return (a.sequence || 0) - (b.sequence || 0) || (stamp(a.occurred_at) || 0) - (stamp(b.occurred_at) || 0);
        });
        ordered.forEach(function (entry) {
            var at = timeOf(entry);
            var row;
            switch (entry.event_type) {
            case "driver.trip.loaded":
                /* Ручной цикл «погрузка → погрузка»: новая отметка завершает
                   предыдущий ручной рейс (core/offline_sync.py, cycle_advanced). */
                trips.forEach(function (trip) {
                    if (trip.origin === "manual" && !trip.completed_at && !trip.cancelled) {
                        trip.completed_at = at;
                        trip.completed_server_time = !!entry.clock_adjusted;
                    }
                });
                row = tripFor(entry);
                if (!row) {
                    row = {
                        local_trip_id: entry.local_trip_id || entry.event_id,
                        trip_id: null,
                        local_ids: [entry.local_trip_id || entry.event_id],
                        origin: "manual"
                    };
                    trips.push(row);
                }
                row.trip_id = entry.server_trip_id || row.trip_id;
                row.excavator_id = entry.excavator_id;
                row.excavator_label = entry.excavator_label;
                row.dump_point_id = entry.dump_point_id;
                row.dump_point_label = entry.dump_point_label;
                row.loaded_at = at;
                row.loaded_server_time = !!entry.clock_adjusted;
                touch(row, entry);
                break;
            case "driver.trip.loaded.cancelled":
                row = tripFor(entry);
                if (row) { row.cancelled = true; touch(row, entry); }
                break;
            case "driver.trip.manual_completed":
            case "driver.trip.unloaded":
                row = tripFor(entry);
                if (!row) {
                    row = {
                        local_trip_id: "",
                        trip_id: entry.trip_id,
                        local_ids: [],
                        origin: "excavator"
                    };
                    trips.push(row);
                }
                row.completed_at = at;
                row.completed_server_time = !!entry.clock_adjusted;
                touch(row, entry);
                break;
            case "driver.trip.dump_point_changed":
                row = tripFor(entry);
                if (!row && entry.trip_id) {
                    row = {local_trip_id: "", trip_id: entry.trip_id, local_ids: [], origin: "excavator"};
                    trips.push(row);
                }
                if (row) {
                    row.dump_point_id = entry.dump_point_id;
                    row.dump_point_label = entry.dump_point_label;
                    row.point_changed = entry.status !== "confirmed";
                    touch(row, entry);
                }
                break;
            case "driver.downtime.started":
                var current = openDowntime();
                /* Та же причина ещё раз — сервер оставляет идущий простой
                   (downtime_unchanged); другая — закрывает его этим же временем. */
                if (current && current.reason_id && current.reason_id === entry.reason_id) {
                    touch(current, entry);
                    break;
                }
                if (current) current.ended_at = at;
                row = {
                    local_downtime_id: entry.local_downtime_id || entry.event_id,
                    downtime_id: entry.server_downtime_id || null,
                    local_ids: [entry.local_downtime_id || entry.event_id],
                    reason_id: entry.reason_id,
                    reason_label: entry.reason_label,
                    started_at: at,
                    started_server_time: !!entry.clock_adjusted,
                    ended_at: null
                };
                downtimes.push(row);
                touch(row, entry);
                break;
            case "driver.downtime.ended":
                row = downtimes.find(function (item) {
                    return (entry.local_downtime_id && item.local_ids.indexOf(entry.local_downtime_id) >= 0)
                        || (entry.downtime_id && item.downtime_id === entry.downtime_id);
                }) || (entry.local_downtime_id || entry.downtime_id ? null : openDowntime());
                if (!row) {
                    /* Начало простоя телефон не видел (начат до этой версии или
                       записан сервером): строку даст сервер, отсюда — конец. */
                    row = {
                        local_downtime_id: "",
                        downtime_id: entry.downtime_id,
                        local_ids: [],
                        reason_id: null,
                        reason_label: "",
                        started_at: null,
                        end_only: true
                    };
                    downtimes.push(row);
                }
                row.ended_at = at;
                row.ended_server_time = !!entry.clock_adjusted;
                touch(row, entry);
                break;
            default:
                break;
            }
        });
        return {trips: trips, downtimes: downtimes};
    }

    function intersects(left, right) {
        return (left || []).some(function (value) { return (right || []).indexOf(value) >= 0; });
    }

    /* Строки путёвки: журнал телефона поверх данных сервера. Несколько
       местных записей могут относиться к одной серверной строке (погрузка и
       отдельная разгрузка того же рейса, повторное начало простоя той же
       причины): все они сводятся в неё, ни одна не становится второй строкой. */
    function merge(local, server, fallbackLabels, fallbackTrips) {
        server = server || {};
        var labels = server.labels || fallbackLabels || {};
        var excavators = labels.excavators || {};
        var reasons = labels.reasons || {};
        var serverTrips = Array.isArray(server.trips) ? server.trips : [];
        var serverDowntimes = Array.isArray(server.downtimes) ? server.downtimes : [];

        function firstWith(rows, field) {
            return rows.filter(function (row) { return row[field]; })
                .sort(function (x, y) { return (stamp(x[field]) || 0) - (stamp(y[field]) || 0); })[0] || null;
        }
        function lastWith(rows, field) {
            return rows.filter(function (row) { return row[field]; })
                .sort(function (x, y) { return (stamp(y[field]) || 0) - (stamp(x[field]) || 0); })[0] || null;
        }
        function allIds(base, rows) {
            return rows.reduce(function (ids, row) { return ids.concat(row.local_ids || []); }, (base || []).slice());
        }

        var tripGroups = serverTrips.map(function () { return []; });
        var looseTrips = [];
        local.trips.forEach(function (trip) {
            var index = serverTrips.findIndex(function (item) {
                return (trip.trip_id && trip.trip_id === item.id) || intersects(trip.local_ids, item.local_ids);
            });
            if (index >= 0) tripGroups[index].push(trip);
            else looseTrips.push(trip);
        });
        /* Местные строки с одним серверным номером, которого нет в данных
           сервера (страница старше), сводятся в одну. */
        var unmatched = [];
        looseTrips.forEach(function (trip) {
            var host = trip.trip_id && unmatched.find(function (other) { return other.trip_id === trip.trip_id; });
            if (host) {
                host.local_ids = host.local_ids.concat(trip.local_ids);
                if (trip.loaded_at && !host.loaded_at) {
                    host.loaded_at = trip.loaded_at;
                    host.excavator_id = trip.excavator_id;
                    host.excavator_label = trip.excavator_label;
                    host.dump_point_label = host.dump_point_label || trip.dump_point_label;
                }
                if (trip.completed_at && !host.completed_at) {
                    host.completed_at = trip.completed_at;
                    host.completed_server_time = trip.completed_server_time;
                }
                host.rejected = host.rejected || trip.rejected;
                host.reject = host.reject || trip.reject;
            } else {
                unmatched.push(Object.assign({}, trip, {local_ids: trip.local_ids.slice()}));
            }
        });

        var trips = serverTrips.map(function (item, index) {
            var mine = tripGroups[index];
            var loaded = firstWith(mine, "loaded_at");
            var completed = lastWith(mine, "completed_at");
            var cancelledHere = !completed && mine.some(function (row) { return row.cancelled; });
            var pointChange = mine.filter(function (row) { return row.point_changed && row.dump_point_label; }).pop();
            var serverCancelled = item.status === "cancelled";
            var isCompleted = completed ? true : item.status === "completed";
            return {
                trip_id: item.id,
                local_ids: allIds(item.local_ids, mine),
                excavator: text(item.excavator) || "—",
                dump_point: pointChange ? pointChange.dump_point_label : (text(item.dump_point) || "—"),
                loaded_at: loaded ? loaded.loaded_at : item.loaded_at,
                completed_at: isCompleted ? (completed ? completed.completed_at : item.completed_at) : null,
                loaded_server_time: loaded ? !!loaded.loaded_server_time : item.load_time_source === "server_receipt",
                completed_server_time: completed ? !!completed.completed_server_time : item.unload_time_source === "server_receipt",
                cancelled: cancelledHere || (serverCancelled && !completed),
                completed: isCompleted,
                rejected: mine.some(function (row) { return row.rejected; }),
                reject: (mine.find(function (row) { return row.reject; }) || {}).reject || null,
                source: mine.length ? "both" : "server"
            };
        });
        unmatched.forEach(function (trip) {
            if (!trip.loaded_at && !trip.completed_at) return;
            /* Переходящий рейс (погружен в прошлой смене, разгружен в этой):
               подписи и время погрузки — из данных сервера о прошлой смене. */
            var known = trip.trip_id && (Array.isArray(fallbackTrips) ? fallbackTrips : []).find(function (item) {
                return item && item.id === trip.trip_id;
            });
            if (known) {
                trip.loaded_at = trip.loaded_at || known.loaded_at;
                trip.excavator_label = trip.excavator_label || known.excavator;
                trip.dump_point_label = trip.dump_point_label || known.dump_point;
            }
            trips.push({
                trip_id: trip.trip_id || null,
                local_ids: trip.local_ids,
                excavator: text(excavators[String(trip.excavator_id)]) || text(trip.excavator_label) || "—",
                dump_point: text(trip.dump_point_label) || "—",
                loaded_at: trip.loaded_at || null,
                completed_at: trip.cancelled ? null : (trip.completed_at || null),
                loaded_server_time: !!trip.loaded_server_time,
                completed_server_time: !!trip.completed_server_time,
                cancelled: !!trip.cancelled,
                completed: !!trip.completed_at && !trip.cancelled,
                rejected: !!trip.rejected,
                reject: trip.reject || null,
                source: "phone"
            });
        });
        trips.sort(function (x, y) {
            return (stamp(x.loaded_at || x.completed_at) || 0) - (stamp(y.loaded_at || y.completed_at) || 0);
        });

        var downtimeGroups = serverDowntimes.map(function () { return []; });
        var looseDowntimes = [];
        local.downtimes.forEach(function (row) {
            var index = serverDowntimes.findIndex(function (item) {
                return (row.downtime_id && row.downtime_id === item.id) || intersects(row.local_ids, item.local_ids);
            });
            if (index >= 0) downtimeGroups[index].push(row);
            else looseDowntimes.push(row);
        });
        /* Конец простоя без ссылки на начало (телефон начала не видел) — к
           последнему идущему на сервере простою, начатому раньше этого конца. */
        looseDowntimes = looseDowntimes.filter(function (row) {
            if (!row.end_only || row.downtime_id || !row.ended_at) return true;
            var end = stamp(row.ended_at);
            var candidate = -1;
            serverDowntimes.forEach(function (item, index) {
                if (!item.ended_at && (stamp(item.started_at) || 0) <= end) candidate = index;
            });
            if (candidate < 0) return true;
            downtimeGroups[candidate].push(row);
            return false;
        });
        var downtimes = serverDowntimes.map(function (item, index) {
            var mine = downtimeGroups[index];
            var started = firstWith(mine, "started_at");
            var ended = lastWith(mine, "ended_at");
            return {
                downtime_id: item.id,
                local_ids: allIds(item.local_ids, mine),
                reason: text(item.reason) || text(started && started.reason_label) || "Простой",
                started_at: started ? started.started_at : item.started_at,
                ended_at: ended ? ended.ended_at : item.ended_at,
                started_server_time: !!(started && started.started_server_time),
                ended_server_time: !!(ended && ended.ended_server_time),
                rejected: mine.some(function (row) { return row.rejected; }),
                reject: (mine.find(function (row) { return row.reject; }) || {}).reject || null,
                source: mine.length ? "both" : "server"
            };
        });
        looseDowntimes.forEach(function (row) {
            if (row.end_only || !row.started_at) return;
            downtimes.push({
                downtime_id: row.downtime_id || null,
                local_ids: row.local_ids,
                reason: text(reasons[String(row.reason_id)]) || text(row.reason_label) || "Простой",
                started_at: row.started_at,
                ended_at: row.ended_at || null,
                started_server_time: !!row.started_server_time,
                ended_server_time: !!row.ended_server_time,
                rejected: !!row.rejected,
                reject: row.reject || null,
                source: "phone"
            });
        });
        downtimes.sort(function (x, y) { return (stamp(x.started_at) || 0) - (stamp(y.started_at) || 0); });
        /* Простой без конца, за которым начат другой, закончился его началом —
           так же считает сервер (переключение причины). */
        downtimes.forEach(function (row, index) {
            var next = downtimes[index + 1];
            if (!row.ended_at && next && next.started_at) row.ended_at = next.started_at;
        });
        return {trips: trips, downtimes: downtimes};
    }

    /* ---- Форматы — как у сервера (users/views.py) ---- */

    function pad(value) { return String(value).padStart(2, "0"); }
    function clock(value, offsetMinutes) {
        var at = stamp(value);
        if (at === null) return "—";
        if (offsetMinutes === null || offsetMinutes === undefined) {
            var local = new Date(at);
            return pad(local.getHours()) + ":" + pad(local.getMinutes());
        }
        var shifted = new Date(at + offsetMinutes * 60000);
        return pad(shifted.getUTCHours()) + ":" + pad(shifted.getUTCMinutes());
    }
    function day(value, offsetMinutes) {
        var at = stamp(value);
        if (at === null) return "";
        var shifted = offsetMinutes === null || offsetMinutes === undefined
            ? new Date(at - new Date(at).getTimezoneOffset() * 60000)
            : new Date(at + offsetMinutes * 60000);
        return pad(shifted.getUTCDate()) + "." + pad(shifted.getUTCMonth() + 1) + "." + shifted.getUTCFullYear();
    }
    function formatDuration(seconds) {
        seconds = Math.max(0, Math.floor(Number(seconds) || 0));
        return pad(Math.floor(seconds / 3600)) + ":" + pad(Math.floor((seconds % 3600) / 60)) + ":" + pad(seconds % 60);
    }
    function reportDuration(seconds, total) {
        var rounded = Math.max(0, Math.floor((Math.max(0, Math.floor(Number(seconds) || 0)) + 30) / 60));
        var hours = Math.floor(rounded / 60);
        var minutes = rounded % 60;
        if (total && hours) return hours + ":" + pad(minutes) + " мин.";
        if (hours && minutes) return hours + " ч. " + minutes + " мин.";
        if (hours) {
            var word = hours % 10 === 1 && hours % 100 !== 11
                ? "час"
                : ([2, 3, 4].indexOf(hours % 10) >= 0 && [12, 13, 14].indexOf(hours % 100) < 0 ? "часа" : "часов");
            return hours + " " + word + ".";
        }
        return rounded + " мин.";
    }

    /* Отчёт и журнал смены из сведённых строк. */
    function buildReport(rows, window_, offsetMinutes) {
        var openedAt = stamp(window_.opened_at);
        var endAt = stamp(window_.closed_at) || window_.now;
        var groups = [];
        var completed = rows.trips.filter(function (trip) { return trip.completed; });
        completed.forEach(function (trip) {
            var group = groups.find(function (item) { return item.excavator === trip.excavator && item.dump_point === trip.dump_point; });
            if (!group) {
                group = {excavator: trip.excavator, dump_point: trip.dump_point, count: 0};
                groups.push(group);
            }
            group.count += 1;
        });
        var reasonTotals = [];
        var timeline = [];
        var downtimeTotal = 0;
        rows.downtimes.forEach(function (row) {
            var start = stamp(row.started_at);
            if (start === null) return;
            var end = row.ended_at ? stamp(row.ended_at) : endAt;
            var overlapStart = openedAt !== null ? Math.max(start, openedAt) : start;
            var overlapEnd = Math.min(end === null ? endAt : end, endAt);
            if (overlapEnd < overlapStart && row.ended_at) return;
            var seconds = Math.max(0, Math.floor((overlapEnd - overlapStart) / 1000));
            var total = reasonTotals.find(function (item) { return item.reason === row.reason; });
            if (!total) {
                total = {reason: row.reason, seconds: 0};
                reasonTotals.push(total);
            }
            total.seconds += seconds;
            downtimeTotal += seconds;
            var marks = [];
            if (row.rejected) marks.push("не принято сервером");
            timeline.push({
                at: overlapStart,
                kind: "downtime-start",
                time: clock(new Date(overlapStart).toISOString(), offsetMinutes),
                title: "Начат простой: " + row.reason,
                meta: marks.concat(row.started_server_time ? ["время сервера"] : []).join(" · "),
                rejected: row.rejected,
                id: row.downtime_id
            });
            if (row.ended_at) {
                timeline.push({
                    at: overlapEnd,
                    kind: "downtime-end",
                    time: clock(new Date(overlapEnd).toISOString(), offsetMinutes),
                    title: "Завершён простой: " + row.reason,
                    meta: [formatDuration(seconds)].concat(marks, row.ended_server_time ? ["время сервера"] : []).join(" · "),
                    rejected: row.rejected,
                    id: row.downtime_id
                });
            }
        });
        rows.trips.forEach(function (trip, index) {
            var marks = [];
            if (trip.cancelled) marks.push("отменён");
            if (trip.rejected) marks.push("не принято сервером");
            if (trip.loaded_server_time || trip.completed_server_time) marks.push("время сервера");
            var started = trip.loaded_at ? clock(trip.loaded_at, offsetMinutes) : "—";
            var finished = trip.completed_at ? clock(trip.completed_at, offsetMinutes) : "...";
            timeline.push({
                at: stamp(trip.loaded_at || trip.completed_at) || 0,
                kind: "trip",
                time: trip.loaded_at ? started : clock(trip.completed_at, offsetMinutes),
                title: "Рейс " + pad(index + 1) + " · " + trip.excavator + " → " + trip.dump_point,
                meta: [started + "–" + finished].concat(marks).join(" · "),
                rejected: trip.rejected,
                id: trip.trip_id
            });
        });
        timeline.sort(function (a, b) { return a.at - b.at; });
        return {
            groups: groups,
            tripTotal: completed.length,
            tripRejected: completed.filter(function (trip) { return trip.rejected; }).length,
            downtimes: reasonTotals.map(function (item) {
                return {reason: item.reason, seconds: item.seconds, duration: reportDuration(item.seconds, false)};
            }),
            downtimeTotal: downtimeTotal,
            downtimeTotalLabel: reportDuration(downtimeTotal, true),
            downtimeRejected: rows.downtimes.filter(function (row) { return row.rejected; }).length,
            running: rows.downtimes.some(function (row) { return row.started_at && !row.ended_at; }),
            timeline: timeline
        };
    }

    function tripsHtml(report) {
        var html = report.groups.map(function (group) {
            return '<div class="driver-report-row" data-driver-report-trip data-excavator="' + escapeHtml(group.excavator)
                + '" data-dump-point="' + escapeHtml(group.dump_point) + '" data-count="' + group.count + '"><span>'
                + escapeHtml(group.excavator) + " " + escapeHtml(group.dump_point) + "</span><strong>" + group.count + "</strong></div>";
        }).join("");
        if (!report.groups.length) html = '<div class="driver-report-row"><span>Завершённых рейсов нет</span><strong>0</strong></div>';
        html += '<div class="driver-report-row"><span>Всего рейсов</span><strong>' + report.tripTotal + "</strong></div>";
        if (report.tripRejected) {
            html += '<div class="driver-report-row" data-driver-report-rejected="trips"><span>Из них не принято сервером</span><strong>'
                + report.tripRejected + "</strong></div>";
        }
        return html;
    }

    function downtimesHtml(report) {
        var html = report.downtimes.map(function (row) {
            return '<div class="driver-report-row" data-driver-report-downtime data-reason="' + escapeHtml(row.reason)
                + '" data-duration="' + escapeHtml(row.duration) + '"><span>' + escapeHtml(row.reason) + "</span><strong>"
                + escapeHtml(row.duration) + "</strong></div>";
        }).join("");
        if (!report.downtimes.length) html = '<div class="driver-report-row"><span>Простоев нет</span><strong>00:00:00</strong></div>';
        html += '<div class="driver-report-row"><span>Всего простоев</span><strong>' + escapeHtml(report.downtimeTotalLabel) + "</strong></div>";
        if (report.downtimeRejected) {
            html += '<div class="driver-report-row" data-driver-report-rejected="downtimes"><span>Из них не принято сервером</span><strong>'
                + report.downtimeRejected + "</strong></div>";
        }
        return html;
    }

    function timelineHtml(report) {
        if (!report.timeline.length) return '<div class="driver-empty-state">Событий в текущей смене пока нет.</div>';
        return report.timeline.map(function (item) {
            var idAttr = item.id
                ? (item.kind === "trip" ? ' data-driver-trip-id="' : ' data-driver-downtime-id="') + escapeHtml(item.id) + '"'
                : "";
            return '<div class="driver-timeline-row is-' + item.kind + (item.rejected ? " is-rejected" : "") + '"' + idAttr
                + "><time>" + escapeHtml(item.time) + "</time><strong>" + escapeHtml(item.title) + "</strong><span>"
                + escapeHtml(item.meta) + "</span></div>";
        }).join("");
    }

    function setHtml(node, html) {
        if (node && node.__driverManifestHtml !== html) {
            node.innerHTML = html;
            node.__driverManifestHtml = html;
        }
    }

    function createController(options) {
        options = options || {};
        var storageOverride = options.storage;
        var nowFn = typeof options.now === "function" ? options.now : function () { return Date.now(); };
        var runningTimer = null;

        function storage() {
            if (storageOverride) return storageOverride;
            try { return root.localStorage || null; } catch (error) { return null; }
        }
        function doc() { return root.document || null; }
        function currentShell() {
            var d = doc();
            return d && typeof d.querySelector === "function" ? d.querySelector("[data-driver-shell]") : null;
        }
        function accessOf(shell) { return text(shell && shell.dataset && shell.dataset.driverAccessId); }

        function reasonLabelFor(shell, reasonId) {
            if (!shell || !reasonId || typeof shell.querySelector !== "function") return "";
            var button = shell.querySelector('[data-driver-downtime-reason-button][data-driver-downtime-reason-id="' + String(reasonId) + '"]');
            return button ? text(button.dataset.driverReasonLabel) : "";
        }

        function mutate(accessId, change) {
            if (!accessId) return false;
            var store = storage();
            var journal = readJournal(store, accessId);
            var changed = change(journal);
            if (changed) writeJournal(store, accessId, prune(journal, nowFn(), changed === true ? null : changed), nowFn());
            return !!changed;
        }

        function recordEvents(accessId, events, shell) {
            return mutate(accessId, function (journal) {
                var changed = false;
                (Array.isArray(events) ? events : []).forEach(function (event) {
                    if (!event || String(event.access_id || accessId) !== String(accessId)) return;
                    var type = text(event.event_type);
                    if (!TRIP_EVENTS[type] && !DOWNTIME_EVENTS[type] && !SHIFT_EVENTS[type]) return;
                    var shift = ensureShift(journal, eventShiftAliases(event), nowFn());
                    if (!shift) return;
                    var reasonLabel = type === "driver.downtime.started"
                        ? reasonLabelFor(shell, event.payload && event.payload.reason_id)
                        : "";
                    if (type === "driver.shift.opened" && !shift.opened_at) {
                        shift.opened_at = text(event.occurred_at);
                        changed = true;
                    }
                    if (type === "driver.shift.closed" && !shift.closed_at) {
                        shift.closed_at = text(event.occurred_at);
                        changed = true;
                    }
                    var entry = logEntry(event, reasonLabel);
                    if (!reasonLabel) delete entry.reason_label;
                    if (upsertLog(shift, entry)) {
                        shift.updated_at = new Date(nowFn()).toISOString();
                        changed = true;
                    }
                });
                return changed;
            });
        }

        function observe(events) {
            var shell = currentShell();
            var accessId = accessOf(shell) || text(events && events[0] && events[0].access_id);
            recordEvents(accessId, events, shell);
            if (shell) render(shell);
        }

        function confirmed(event, result) {
            if (!event) return false;
            var shell = currentShell();
            var accessId = accessOf(shell) || text(event.access_id);
            var serverIds = result && result.server_ids || {};
            var changed = mutate(accessId, function (journal) {
                var aliases = eventShiftAliases(event);
                if (positive(serverIds.shift_id)) aliases.push("s:" + positive(serverIds.shift_id));
                var shift = ensureShift(journal, aliases, nowFn());
                if (!shift) return false;
                var type = text(event.event_type);
                if (!TRIP_EVENTS[type] && !DOWNTIME_EVENTS[type] && !SHIFT_EVENTS[type]) return true;
                var entry = logEntry(Object.assign({}, event, {state: "confirmed"}), "");
                delete entry.reason_label;
                entry.reject = null;
                entry.server_trip_id = positive(serverIds.trip_id) || undefined;
                entry.server_downtime_id = positive(serverIds.downtime_event_id || serverIds.downtime_id) || undefined;
                if (result && result.device_clock_adjusted && result.effective_occurred_at) {
                    entry.clock_adjusted = true;
                    entry.effective_at = text(result.effective_occurred_at);
                }
                if (type === "driver.shift.opened" && entry.clock_adjusted) shift.opened_at = entry.effective_at;
                if (type === "driver.shift.closed" && entry.clock_adjusted) shift.closed_at = entry.effective_at;
                upsertLog(shift, entry);
                shift.updated_at = new Date(nowFn()).toISOString();
                return true;
            });
            if (shell) render(shell);
            return changed;
        }

        function review(event, result) {
            if (!event) return false;
            var shell = currentShell();
            var accessId = accessOf(shell) || text(event.access_id);
            var status = text(result && result.status) || "conflict";
            var changed = mutate(accessId, function (journal) {
                var shift = ensureShift(journal, eventShiftAliases(event), nowFn());
                if (!shift) return false;
                var entry = logEntry(Object.assign({}, event, {
                    state: REJECTED_STATES[status] ? status : "pending",
                    last_error: {code: text(result && result.code) || status, message: text(result && result.message)}
                }), "");
                delete entry.reason_label;
                return upsertLog(shift, entry);
            });
            if (shell) render(shell);
            return changed;
        }

        /* Данные сервера — атрибутом панели путёвки: <script> с JSON разбор
           фрагмента при подмене экрана выбрасывает, атрибут переживает и
           подмену, и послойное обновление. */
        function serverData(shell) {
            var panel = shell && typeof shell.querySelector === "function"
                ? shell.querySelector('[data-driver-tab-panel="manifest"]')
                : null;
            var raw = panel && typeof panel.getAttribute === "function"
                ? panel.getAttribute("data-driver-manifest-data")
                : null;
            if (!raw) return null;
            try { return JSON.parse(raw); } catch (error) { return null; }
        }

        function pageVersion() {
            var d = doc();
            return Number(d && d.body && d.body.dataset ? d.body.dataset.operationalStateVersion || 0 : 0);
        }

        function localShiftAliases(shell) {
            var local = root.DriverLocalShift && typeof root.DriverLocalShift.state === "function"
                ? root.DriverLocalShift.state(shell)
                : null;
            if (!local) return {aliases: [], state: null};
            return {
                aliases: [shiftAlias(local.local_shift_id), shiftAlias(local.server_shift_id)].filter(Boolean),
                state: local
            };
        }

        /* Какая смена сейчас в путёвке и её строки. */
        function model(shell) {
            var accessId = accessOf(shell);
            var now = nowFn();
            var page = serverData(shell);
            var version = pageVersion();
            var local = localShiftAliases(shell);
            var open = text(shell.dataset.driverShiftOpen) === "true";
            var shownAlias = open ? shiftAlias(shell.dataset.driverShiftId) : "";
            var result = null;
            mutate(accessId, function (journal) {
                var changed = false;
                var pageAliases = page && page.shift
                    ? [shiftAlias(page.shift.id), shiftAlias(page.shift.local_id)].filter(Boolean)
                    : [];
                if (pageAliases.length) {
                    var pageShift = ensureShift(journal, pageAliases, now);
                    if (!pageShift.server || version >= Number(pageShift.server.version || 0)) {
                        if (JSON.stringify(pageShift.server && pageShift.server.data) !== JSON.stringify(page)) changed = true;
                        pageShift.server = {version: version, data: page};
                    }
                    if (!pageShift.opened_at && page.shift.opened_at) { pageShift.opened_at = page.shift.opened_at; changed = true; }
                    if (page.shift.closed_at && !pageShift.closed_at) { pageShift.closed_at = page.shift.closed_at; changed = true; }
                }
                if (local.aliases.length) {
                    var before = journal.shifts.length;
                    var localShift = ensureShift(journal, local.aliases, now);
                    if (journal.shifts.length !== before) changed = true;
                    if (local.state.opened_at && !localShift.opened_at) { localShift.opened_at = local.state.opened_at; changed = true; }
                    if (local.state.status === "closed" && local.state.closed_at && !localShift.closed_at) {
                        localShift.closed_at = local.state.closed_at;
                        changed = true;
                    }
                }
                var shown = null;
                if (shownAlias) {
                    shown = findShift(journal, [shownAlias])[0] || null;
                    if (!shown) {
                        shown = ensureShift(journal, [shownAlias], now);
                        changed = true;
                    }
                } else {
                    /* Смена закрыта: показываем последнюю по времени — закрытую на
                       телефоне или ту, что нарисовал сервер. */
                    var candidates = [];
                    if (local.aliases.length) candidates = candidates.concat(findShift(journal, local.aliases));
                    if (pageAliases.length) candidates = candidates.concat(findShift(journal, pageAliases));
                    candidates.sort(function (a, b) { return (stamp(b.opened_at) || 0) - (stamp(a.opened_at) || 0); });
                    shown = candidates[0] || null;
                }
                if (shown) {
                    var data = shown.server ? shown.server.data : null;
                    var replayed = replay(shown.log);
                    /* Подпись причины, не записанная в журнал (событие пришло без
                       экрана), — с кнопки причины на экране. */
                    replayed.downtimes.forEach(function (row) {
                        if (!row.reason_label && row.reason_id) row.reason_label = reasonLabelFor(shell, row.reason_id);
                    });
                    var rows = merge(replayed, data, page && page.labels, page && page.trips);
                    var offsetSource = data || page;
                    result = {
                        shift: shown,
                        server: data,
                        rows: rows,
                        window: {
                            opened_at: shown.opened_at || (data && data.shift && data.shift.opened_at) || null,
                            closed_at: open ? null : (shown.closed_at || (data && data.shift && data.shift.closed_at) || null),
                            now: now
                        },
                        offset: offsetSource && Number.isFinite(Number(offsetSource.utc_offset_minutes))
                            ? Number(offsetSource.utc_offset_minutes)
                            : null,
                        local: local.state
                    };
                }
                return changed ? (shown ? shown.aliases : true) : false;
            });
            return result;
        }

        function render(shell) {
            shell = shell || currentShell();
            if (!shell || !shell.dataset || typeof shell.querySelector !== "function") return null;
            var panel = shell.querySelector('[data-driver-tab-panel="manifest"]');
            var current = model(shell);
            if (!panel || !current) return current;
            var report = buildReport(current.rows, current.window, current.offset);
            setHtml(panel.querySelector("[data-driver-report-trip-scroll]"), tripsHtml(report));
            setHtml(panel.querySelector("[data-driver-report-downtime-scroll]"), downtimesHtml(report));
            setHtml(panel.querySelector("[data-driver-timeline]"), timelineHtml(report));
            panel.dataset.driverReportTripTotal = String(report.tripTotal);
            panel.dataset.driverReportDowntimeTotal = report.downtimeTotalLabel;
            var serverShift = current.server && current.server.shift;
            var isServerShift = !!serverShift && current.shift.aliases.indexOf(shiftAlias(serverShift.id)) >= 0;
            if (!isServerShift && current.window.opened_at) {
                panel.dataset.driverReportDate = day(current.window.opened_at, current.offset);
                var type = text(current.local && current.local.shift_type) || text(shell.dataset.driverShiftType);
                if (type === "day") panel.dataset.driverReportShift = "Первая смена";
                if (type === "night") panel.dataset.driverReportShift = "Вторая смена";
            }
            /* Показания на конец смены, закрытой на телефоне: сервер их ещё не видел. */
            var local = current.local;
            var localAliases = local ? [shiftAlias(local.local_shift_id), shiftAlias(local.server_shift_id)].filter(Boolean) : [];
            var ending = local && local.status === "closed"
                && current.shift.aliases.some(function (alias) { return localAliases.indexOf(alias) >= 0; })
                ? local.end_readings || {}
                : null;
            if (ending) {
                ["fuel", "mileage", "engine_hours"].forEach(function (field) {
                    var value = text(ending["end_" + field]);
                    var key = "driverReportEnd" + field.split("_").map(function (part) {
                        return part.charAt(0).toUpperCase() + part.slice(1);
                    }).join("");
                    if (value) panel.dataset[key] = value;
                });
            }
            shell.querySelectorAll("[data-driver-shift-fact]").forEach(function (node) {
                var label = report.tripTotal + " шт.";
                if (node.textContent !== label) node.textContent = label;
            });
            scheduleRunning(report.running);
            root.__driverManifestLastModel = {
                trips: current.rows.trips.length,
                downtimes: current.rows.downtimes.length,
                tripTotal: report.tripTotal,
                shift: current.shift.aliases.slice()
            };
            return current;
        }

        /* Идущий простой растёт: пересчитываем раз в 30 с, пока он идёт. */
        function scheduleRunning(running) {
            if (running && !runningTimer && typeof root.setInterval === "function") {
                runningTimer = root.setInterval(function () {
                    var d = doc();
                    if (d && d.hidden) return;
                    render(currentShell());
                }, 30000);
                if (runningTimer && typeof runningTimer.unref === "function") runningTimer.unref();
            } else if (!running && runningTimer && typeof root.clearInterval === "function") {
                root.clearInterval(runningTimer);
                runningTimer = null;
            }
        }

        return {
            observe: observe,
            confirmed: confirmed,
            review: review,
            render: render,
            model: model,
            journal: function (accessId) { return readJournal(storage(), accessId); }
        };
    }

    root.createDriverManifestLocal = createController;
    root.DriverManifestLocal = createController();
    if (typeof module !== "undefined" && module.exports) {
        module.exports = {
            createDriverManifestLocal: createController,
            replay: replay,
            merge: merge,
            buildReport: buildReport,
            reportDuration: reportDuration,
            shiftAlias: shiftAlias
        };
    }
})(typeof window !== "undefined" ? window : globalThis);
