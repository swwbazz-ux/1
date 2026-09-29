/* Смена водителя на телефоне: открывается и закрывается по нажатию, без
   ожидания сервера (владелец, 30.09.2026: «приложение не должно ждать
   сервера… без интернета всё работает; сервер догоняет, когда связь
   появится»).

   Открытие и закрытие — события общей очереди driver-offline-outbox-v2.js
   (driver.shift.opened / driver.shift.closed). Пока сервер не принял
   открытие, у смены нет серверного ID: её местный ID — ID события открытия
   (local_shift_id), и все действия в этой смене несут его, как у машиниста
   (excavator-local-shift-v1.js, Codex c05a5925). Сервер привязывает их по
   квитанции открытия (core/offline_sync.py, _bind_driver_local_shift).

   Здесь хранится только последнее состояние смены на телефоне и решается,
   что показывать: страницу рисует сервер (при закрытой смене — рабочий экран
   назначенного самосвала выключенным и вторую форму смены скрытой), а этот
   модуль переключает её в «смена открыта / закрыта», пока сервер не
   догонит. Серверная отрисовка побеждает, как только в ней видно то же
   (смена открыта с тем же ID / закрыта) или прошла более новая версия
   состояния, чем та, в которой сервер принял событие. */
(function (root) {
    "use strict";

    var KEY_PREFIX = "driver-local-shift-v1:";
    var READING_FIELDS = ["fuel", "mileage", "engine_hours"];

    function text(value) { return value === null || value === undefined ? "" : String(value).trim(); }
    function positive(value) {
        var parsed = Number(value);
        return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
    }
    function clone(value) { return value === null || value === undefined ? value : JSON.parse(JSON.stringify(value)); }
    function randomId(prefix) {
        var uuid = root.crypto && typeof root.crypto.randomUUID === "function"
            ? root.crypto.randomUUID()
            : Date.now().toString(36) + "-" + Math.random().toString(36).slice(2) + "-" + Math.random().toString(36).slice(2);
        return prefix + ":" + uuid;
    }

    function storageFor(options) {
        if (options && options.storage) return options.storage;
        try { return root.localStorage || null; } catch (error) { return null; }
    }

    function read(storage, accessId) {
        if (!storage || !accessId) return null;
        try {
            var value = JSON.parse(storage.getItem(KEY_PREFIX + accessId) || "null");
            return value && value.status ? value : null;
        } catch (error) {
            return null;
        }
    }

    function write(storage, accessId, state) {
        if (!storage || !accessId) throw new Error("Хранилище телефона недоступно.");
        if (state) {
            state.updated_at = new Date().toISOString();
            storage.setItem(KEY_PREFIX + accessId, JSON.stringify(state));
        } else {
            storage.removeItem(KEY_PREFIX + accessId);
        }
    }

    /* Что показывать: состояние телефона против серверной отрисовки.
       server = {shiftId: открытая смена в разметке сервера или "", version}. */
    function decide(state, server) {
        var rendered = text(server && server.shiftId);
        var version = Number(server && server.version || 0);
        if (!state) return {open: !!rendered, shiftRef: rendered, local: false, drop: false};
        var serverShiftId = text(state.server_shift_id);
        /* Состояние телефона не выбрасывается, когда сервер просто согласился:
           оболочка из кэша при следующем запуске без сети может быть старше, и
           тогда только оно знает правду. Сервер побеждает, лишь показав другое
           в версии новее той, в которой принял событие (например, смену закрыл
           диспетчер). */
        if (state.status === "open") {
            if (serverShiftId && rendered === serverShiftId) {
                return {open: true, shiftRef: rendered, local: false, drop: false};
            }
            if (serverShiftId && state.open_confirmed_version && version > Number(state.open_confirmed_version)) {
                return {open: !!rendered, shiftRef: rendered, local: false, drop: true};
            }
            return {
                open: true,
                shiftRef: serverShiftId || text(state.local_shift_id),
                local: !serverShiftId,
                drop: false
            };
        }
        if (
            state.close_confirmed_version
            && version > Number(state.close_confirmed_version)
            && rendered
            && rendered !== serverShiftId
        ) {
            return {open: true, shiftRef: rendered, local: false, drop: true};
        }
        return {open: false, shiftRef: "", local: false, drop: false};
    }

    function readingsFrom(form, prefix) {
        var result = {};
        READING_FIELDS.forEach(function (field) {
            var input = form && form.querySelector('[name="' + prefix + field + '"]');
            result[prefix + field] = input ? text(input.value) : "";
        });
        return result;
    }

    function setDisabled(control, disabled) {
        control.disabled = disabled;
        if (disabled) control.setAttribute("aria-disabled", "true");
        else control.removeAttribute("aria-disabled");
    }

    function resetOpeningForm(form, readings) {
        form.dataset.driverInPlacePending = "false";
        form.dataset.driverShiftOpeningPending = "false";
        delete form.dataset.driverShiftHoldComplete;
        var button = form.querySelector("[data-driver-shift-open-button]");
        if (readings) {
            READING_FIELDS.forEach(function (field) {
                var input = form.querySelector('[name="start_' + field + '"]');
                var value = text(readings["end_" + field]);
                if (input && value) input.value = value;
            });
        }
        if (button) {
            button.classList.remove("is-pending");
            var label = button.querySelector("[data-mobile-shift-label]");
            if (label) label.textContent = "Начать смену";
            var inputs = Array.prototype.slice.call(form.querySelectorAll("input[type='number']"));
            button.disabled = !inputs.length || inputs.some(function (input) {
                return text(input.value) === "" || (typeof input.checkValidity === "function" && !input.checkValidity());
            });
        }
    }

    /* Удержание «Начать смену» ставит форме признак отправки (pending), ввод
       показаний — признак набора (dirty). Раньше их снимала полная подмена
       экрана ответом сервера; при местном открытии экран не подменяется, и
       признаки оставались на скрытой форме навсегда: проверки «водитель вводит»
       (isDriverOperationalRefreshUnsafe, driverIsTypingIntoForm) откладывали
       каждое обновление с сервера — синий индикатор, пустая путёвка
       (бой v371, Infinix, 30.09.2026). Смена открыта — форма открытия больше не
       в работе. */
    function clearOpeningBusy(form) {
        form.dataset.driverShiftOpeningPending = "false";
        form.dataset.driverShiftOpeningDirty = "false";
        form.dataset.driverInPlacePending = "false";
        delete form.dataset.driverShiftHoldComplete;
        delete form.dataset.otherRoleShiftConfirmed;
        var button = form.querySelector("[data-driver-shift-open-button]");
        if (button) {
            button.classList.remove("is-pending");
            var label = button.querySelector("[data-mobile-shift-label]");
            if (label) label.textContent = "Начать смену";
        }
    }

    /* То же для формы закрытия, когда смена закрыта: отправка завершена. */
    function clearClosingBusy(form) {
        form.dataset.driverInPlacePending = "false";
        delete form.dataset.driverShiftHoldComplete;
        var button = form.querySelector("[data-driver-shift-close-button]");
        if (button) {
            button.disabled = false;
            button.classList.remove("is-pending");
            var label = button.querySelector("[data-mobile-shift-label]");
            if (label) label.textContent = "Закрыть смену";
        }
    }

    function resetClosingForm(form, state) {
        form.dataset.driverInPlacePending = "false";
        form.classList.remove("is-sync-pending");
        var pendingPanel = form.querySelector("[data-driver-shift-sync-pending]");
        if (pendingPanel) pendingPanel.hidden = true;
        var button = form.querySelector("[data-driver-shift-close-button]");
        if (button) {
            button.disabled = false;
            button.classList.remove("is-pending");
            var label = button.querySelector("[data-mobile-shift-label]");
            if (label) label.textContent = "Закрыть смену";
        }
        if (state && state.readings) {
            form.querySelectorAll("[data-driver-shift-start]").forEach(function (node) {
                var value = text(state.readings[node.dataset.driverShiftStart]);
                node.textContent = value || "—";
            });
        }
    }

    function apply(shell, decision, state) {
        var open = !!decision.open;
        shell.dataset.driverShiftId = decision.shiftRef || "";
        shell.dataset.driverLocalShiftId = decision.local && state ? text(state.local_shift_id) : "";
        shell.dataset.driverShiftOpen = open ? "true" : "false";
        shell.dataset.driverShiftProjected = decision.shiftRef === text(shell.dataset.driverServerShiftId) ? "server" : "local";
        if (open && !text(shell.dataset.driverCurrentTruckId) && text(shell.dataset.driverPreparedTruckId)) {
            shell.dataset.driverCurrentTruckId = text(shell.dataset.driverPreparedTruckId);
        }
        var header = shell.querySelector(".driver-header-id");
        if (header) header.classList.toggle("is-inactive", !open);
        shell.querySelectorAll("[data-driver-shift-gated]").forEach(function (control) {
            setDisabled(control, !open);
        });
        var closeForm = shell.querySelector('[data-driver-local-shift-form="close"]');
        var openForm = shell.querySelector('[data-driver-local-shift-form="open"]');
        /* Показываем вторую форму только если она есть: без назначения сервер не
           рисует форму открытия, и тогда остаётся то, что он нарисовал. */
        if (closeForm && openForm) {
            closeForm.hidden = !open;
            openForm.hidden = open;
        }
        if (closeForm) {
            var serverId = positive(decision.shiftRef) ? String(positive(decision.shiftRef)) : "";
            closeForm.dataset.nativeShiftId = serverId;
            var shiftInput = closeForm.querySelector('input[name="shift_id"]');
            if (shiftInput) shiftInput.value = serverId;
            if (open && decision.local) resetClosingForm(closeForm, state);
            else if (open && state && state.status === "open") resetClosingForm(closeForm, state);
        }
        if (openForm && open) clearOpeningBusy(openForm);
        if (closeForm && !open) clearClosingBusy(closeForm);
        if (openForm && !open && state && state.status === "closed") {
            resetOpeningForm(openForm, state.end_readings);
        }
    }

    function createController(options) {
        options = options || {};
        var storage = storageFor(options);

        function accessOf(shell) { return text(shell && shell.dataset.driverAccessId); }
        function outbox() { return options.outbox || root.driverOfflineOutbox || null; }
        function serverView(shell) {
            var body = root.document && root.document.body;
            return {
                shiftId: text(shell.dataset.driverServerShiftId),
                version: Number(body && body.dataset ? body.dataset.operationalStateVersion || 0 : 0)
            };
        }

        function project(shell) {
            if (!shell) return null;
            var accessId = accessOf(shell);
            var state = read(storage, accessId);
            var decision = decide(state, serverView(shell));
            if (decision.drop) {
                try { write(storage, accessId, null); } catch (error) {}
                state = null;
            }
            apply(shell, decision, state);
            return decision;
        }

        function openShift(form) {
            var shell = form && form.closest("[data-driver-shell]");
            var box = outbox();
            if (!shell || !box) return Promise.reject(new Error("Очередь телефона недоступна. Обновите экран."));
            var accessId = accessOf(shell);
            var truckId = positive(shell.dataset.driverPreparedTruckId) || positive(shell.dataset.driverCurrentTruckId);
            if (!truckId) return Promise.reject(new Error("Самосвал не назначен — начать смену нельзя."));
            var readings = readingsFrom(form, "start_");
            var previous = read(storage, accessId);
            var dependsOn = [];
            if (previous && previous.status === "closed" && previous.close_event_id && !previous.close_confirmed_version) {
                dependsOn.push(String(previous.close_event_id));
            }
            var eventId = randomId("driver-shift-open");
            return box.enqueue({
                event_id: eventId,
                event_type: "driver.shift.opened",
                local_shift_id: eventId,
                shift_id: null,
                equipment_id: truckId,
                depends_on: dependsOn,
                payload: Object.assign({
                    truck_id: truckId,
                    shift_type: text(shell.dataset.driverShiftType),
                    local_shift_id: eventId
                }, readings),
                context_snapshot: {source: "driver_local_shift"}
            }).then(function (event) {
                write(storage, accessId, {
                    local_shift_id: eventId,
                    open_event_id: eventId,
                    server_shift_id: null,
                    equipment_id: truckId,
                    opened_at: event.occurred_at,
                    readings: readings,
                    status: "open"
                });
                project(root.document.querySelector("[data-driver-shell]") || shell);
                return event;
            });
        }

        function closeShift(form) {
            var shell = form && form.closest("[data-driver-shell]");
            var box = outbox();
            if (!shell || !box) return Promise.reject(new Error("Очередь телефона недоступна. Обновите экран."));
            var accessId = accessOf(shell);
            var state = read(storage, accessId);
            var localShiftId = text(shell.dataset.driverLocalShiftId);
            var serverShiftId = positive(shell.dataset.driverShiftId);
            if (!localShiftId && !serverShiftId) {
                return Promise.reject(new Error("Смена на телефоне не найдена. Обновите экран."));
            }
            var readings = readingsFrom(form, "end_");
            var eventId = randomId("driver-shift-close");
            /* Закрытие уходит на сервер только после всех неотправленных событий
               своей смены. Без этого очередь, копившаяся без связи, отправляла
               закрытие раньше простоев и выбора ковша той же смены (у старых
               записей бэкофф длиннее), и выбор ковша попадал в уже закрытую
               смену — отказ driver_shift_closed (стенд, 30.09.2026). */
            var shiftKeys = [];
            if (serverShiftId) shiftKeys.push(String(serverShiftId));
            if (localShiftId) shiftKeys.push(localShiftId);
            if (state && state.local_shift_id) shiftKeys.push(text(state.local_shift_id));
            if (state && state.server_shift_id) shiftKeys.push(String(state.server_shift_id));
            var pendingList = typeof box.pending === "function" ? box.pending() : Promise.resolve([]);
            return Promise.resolve(pendingList).catch(function () { return []; }).then(function (events) {
                var dependsOn = (Array.isArray(events) ? events : []).filter(function (item) {
                    return item
                        && item.state === "pending"
                        && item.event_type !== "driver.shift.closed"
                        && shiftKeys.indexOf(text(item.shift_id || item.local_shift_id)) >= 0;
                }).map(function (item) { return String(item.event_id); });
                return box.enqueue({
                    event_id: eventId,
                    event_type: "driver.shift.closed",
                    shift_id: serverShiftId,
                    local_shift_id: serverShiftId ? null : localShiftId,
                    depends_on: dependsOn,
                    payload: Object.assign({confirmation_token: ""}, readings),
                    context_snapshot: {source: "driver_local_shift"}
                });
            }).then(function (event) {
                var base = state && state.status === "open" ? state : {};
                write(storage, accessId, {
                    local_shift_id: text(base.local_shift_id) || (serverShiftId ? "server-shift:" + serverShiftId : localShiftId),
                    open_event_id: text(base.open_event_id),
                    server_shift_id: serverShiftId || base.server_shift_id || null,
                    equipment_id: base.equipment_id || positive(shell.dataset.driverCurrentTruckId),
                    opened_at: base.opened_at || "",
                    readings: base.readings || {},
                    open_confirmed_version: base.open_confirmed_version || null,
                    status: "closed",
                    close_event_id: eventId,
                    closed_at: event.occurred_at,
                    end_readings: readings
                });
                project(root.document.querySelector("[data-driver-shell]") || shell);
                return event;
            });
        }

        function onConfirmed(event, result) {
            if (!event || !/^driver\.shift\.(opened|closed)$/.test(String(event.event_type || ""))) return false;
            var shell = root.document && root.document.querySelector("[data-driver-shell]");
            var accessId = shell ? accessOf(shell) : text(event.access_id);
            var state = read(storage, accessId);
            if (!state) return false;
            var version = Number(result && (result.version || result.server_version) || 0) || 1;
            var serverIds = result && result.server_ids || {};
            var changed = false;
            if (event.event_type === "driver.shift.opened" && text(event.event_id) === text(state.open_event_id)) {
                state.server_shift_id = positive(serverIds.shift_id) || state.server_shift_id;
                state.open_confirmed_version = version;
                changed = true;
            }
            if (event.event_type === "driver.shift.closed" && text(event.event_id) === text(state.close_event_id)) {
                state.server_shift_id = positive(serverIds.shift_id) || state.server_shift_id;
                state.close_confirmed_version = version;
                changed = true;
            }
            if (!changed) return false;
            try { write(storage, accessId, state); } catch (error) { return false; }
            if (shell) project(shell);
            return true;
        }

        return {
            project: project,
            open: openShift,
            close: closeShift,
            onConfirmed: onConfirmed,
            state: function (shell) { return clone(read(storage, accessOf(shell))); }
        };
    }

    root.createDriverLocalShift = createController;
    root.DriverLocalShift = createController();
    if (typeof module !== "undefined" && module.exports) {
        module.exports = {createDriverLocalShift: createController, decide: decide};
    }
})(typeof window !== "undefined" ? window : globalThis);
