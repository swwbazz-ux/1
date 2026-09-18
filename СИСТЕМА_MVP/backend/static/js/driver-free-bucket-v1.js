(function (root) {
    "use strict";

    var CATALOG_SCHEMA = "driver-free-bucket-catalog-v1";
    var STATE_SCHEMA = "driver-free-bucket-state-v1";
    var CATALOG_STALE_AFTER_MS = 60 * 60 * 1000;
    var pendingFragment = null;
    var currentController = null;

    function text(value) {
        return String(value == null ? "" : value).trim();
    }

    function positive(value) {
        value = Number(value);
        return Number.isInteger(value) && value > 0 ? value : null;
    }

    function clone(value) {
        return JSON.parse(JSON.stringify(value));
    }

    function readJsonScript(document, id) {
        var node = document && document.getElementById(id);
        if (!node) return null;
        try { return JSON.parse(node.textContent || "null"); } catch (error) { return null; }
    }

    function normalizeItem(item) {
        item = item || {};
        var id = positive(item.id || item.excavator_id);
        if (!id) return null;
        var dumpPoints = (Array.isArray(item.dump_points) ? item.dump_points : []).map(function (point) {
            point = point || {};
            var pointId = positive(point.id || point.dump_point_id);
            return pointId ? {
                id: pointId,
                name: text(point.name || point.dump_point),
                transport_distance_km: text(point.transport_distance_km)
            } : null;
        }).filter(Boolean);
        if (!dumpPoints.length && positive(item.dump_point_id)) {
            dumpPoints.push({
                id: positive(item.dump_point_id),
                name: text(item.dump_point),
                transport_distance_km: text(item.transport_distance_km)
            });
        }
        var primaryDumpPoint = dumpPoints[0] || {};
        var missingFields = Array.isArray(item.missing_fields) ? item.missing_fields.map(text).filter(Boolean) : [];
        return {
            id: id,
            label: text(item.label) || ("ЭКС-" + id),
            complex_label: text(item.complex_label),
            is_primary: item.is_primary === true || item.is_primary === "true",
            available: item.available === undefined
                ? missingFields.length === 0
                : item.available !== false && item.available !== "false",
            loading_horizon: text(item.loading_horizon),
            loading_block: text(item.loading_block),
            rock_type_id: positive(item.rock_type_id),
            rock_type: text(item.rock_type || item.rock_type_name),
            dump_point_id: positive(item.dump_point_id || primaryDumpPoint.id),
            dump_point: text(item.dump_point || primaryDumpPoint.name),
            dump_points: dumpPoints,
            transport_distance_km: text(item.transport_distance_km || primaryDumpPoint.transport_distance_km),
            placement_updated_at: text(item.placement_updated_at),
            missing_fields: missingFields
        };
    }

    function normalizeCatalog(value, stale) {
        value = value || {};
        if (value.schema && value.schema !== CATALOG_SCHEMA) return null;
        var items = (Array.isArray(value.excavators) ? value.excavators : []).map(normalizeItem).filter(Boolean);
        return {
            schema: CATALOG_SCHEMA,
            generated_at: text(value.generated_at),
            version: Number(value.version || 0),
            complete: value.complete === true,
            stale: stale === true || value.stale === true,
            missing_fields: Array.isArray(value.missing_fields) ? value.missing_fields.map(text).filter(Boolean) : [],
            primary_assignment_label: text(value.primary_assignment_label),
            excavators: items
        };
    }

    function isAuthoritativeCatalog(value) {
        return !!(
            value
            && typeof value === "object"
            && !Array.isArray(value)
            && value.schema === CATALOG_SCHEMA
            && value.complete === true
            && Array.isArray(value.excavators)
        );
    }

    function catalogIsStale(value, windowObject, nowValue) {
        value = value || {};
        if (value.stale === true) return true;
        if (windowObject && windowObject.navigator && windowObject.navigator.onLine === false) return true;
        var generatedAt = Date.parse(value.generated_at || "");
        if (!generatedAt) return true;
        var now = Number(nowValue);
        if (!Number.isFinite(now)) now = Date.now();
        return now - generatedAt >= CATALOG_STALE_AFTER_MS;
    }

    function normalizeState(value) {
        value = value || {};
        var selection = normalizeItem(value.selection);
        return {
            schema: STATE_SCHEMA,
            generated_at: text(value.generated_at),
            version: Number(value.version || 0),
            active: value.active === true && !!selection,
            acceptance_id: positive(value.acceptance_id),
            acceptance_local_id: text(value.acceptance_local_id),
            status: text(value.status),
            can_cancel: value.can_cancel === true,
            selection: selection,
            sync_mode: text(value.sync_mode) || "confirmed",
            catalog_version: Number(value.catalog_version || value.version || 0),
            catalog_generated_at: text(value.catalog_generated_at)
        };
    }

    function stateIsNewer(candidate, baseline) {
        candidate = normalizeState(candidate);
        baseline = normalizeState(baseline);
        if (candidate.version !== baseline.version) return candidate.version > baseline.version;
        return (Date.parse(candidate.generated_at || "") || 0) > (Date.parse(baseline.generated_at || "") || 0);
    }

    function resolveInstalledState(serverState, savedState) {
        var fresh = normalizeState(serverState);
        var saved = normalizeState(savedState);
        var savedIsLocal = saved.sync_mode === "local" || saved.sync_mode === "review";
        if (!fresh.active && saved.active && savedIsLocal && stateIsNewer(saved, fresh)) return saved;
        return fresh;
    }

    function displaySnapshot(item) {
        item = normalizeItem(item);
        if (!item) return {};
        return {
            excavator_id: item.id,
            excavator_label: item.label,
            complex_label: item.complex_label,
            loading_horizon: item.loading_horizon,
            loading_block: item.loading_block,
            rock_type_id: item.rock_type_id,
            rock_type_name: item.rock_type,
            dump_points: clone(item.dump_points),
            placement_updated_at: item.placement_updated_at,
            missing_fields: clone(item.missing_fields),
            is_primary: item.is_primary
        };
    }

    function itemFromEvent(event, catalog) {
        var payload = event && event.payload || {};
        var snapshot = event && event.context_snapshot || payload.display || {};
        var id = positive(payload.excavator_id || snapshot.excavator_id);
        var fromCatalog = catalog && catalog.excavators.find(function (item) { return item.id === id; });
        return normalizeItem(Object.assign({}, fromCatalog || {}, snapshot, {
            id: id,
            label: snapshot.excavator_label || (fromCatalog && fromCatalog.label)
        }));
    }

    function createDriverFreeBucketController(options) {
        options = options || {};
        var windowObject = options.window || root;
        var document = options.document || windowObject.document;
        var storage = options.storage || windowObject.localStorage;
        var shell = options.shell;
        var outbox = options.outbox;
        var accessId = text(shell && shell.dataset.driverAccessId);
        var authGeneration = text(shell && shell.dataset.driverAuthGeneration);
        var shiftId = text(shell && shell.dataset.driverShiftId);
        var truckId = text(shell && shell.dataset.driverCurrentTruckId);
        var catalogKey = CATALOG_SCHEMA + ":" + accessId + ":" + authGeneration;
        var stateKey = STATE_SCHEMA + ":" + accessId + ":" + shiftId + ":" + truckId;
        var trigger = shell && shell.querySelector('[data-mobile-dial-action="free-bucket"]');
        var sheet = shell && shell.querySelector("[data-driver-free-bucket-sheet]");
        var grid = shell && shell.querySelector("[data-driver-free-bucket-grid]");
        var message = shell && shell.querySelector("[data-driver-free-bucket-message]");
        var returnFocus = null;
        var catalog = null;
        var state = normalizeState(options.state || {});

        function storageRead(key) {
            if (!storage) return null;
            try { return JSON.parse(storage.getItem(key) || "null"); } catch (error) { return null; }
        }

        function storageWrite(key, value) {
            if (!storage) return false;
            try {
                storage.setItem(key, JSON.stringify(value));
                return true;
            } catch (error) {
                return false;
            }
        }

        function storageRemove(key) {
            if (!storage) return;
            try { storage.removeItem(key); } catch (error) {}
        }

        function installCatalog(serverCatalog) {
            var fresh = isAuthoritativeCatalog(serverCatalog)
                ? normalizeCatalog(serverCatalog, false)
                : null;
            if (fresh) {
                storageWrite(catalogKey, fresh);
                catalog = normalizeCatalog(
                    fresh,
                    catalogIsStale(fresh, windowObject, typeof options.now === "function" ? options.now() : options.now)
                );
            } else {
                catalog = normalizeCatalog(storageRead(catalogKey), true) || normalizeCatalog({}, true);
            }
            renderTiles();
            setNode("[data-driver-free-bucket-primary-label]", catalog.primary_assignment_label || "—");
            return catalog;
        }

        function installState(serverState) {
            var fresh = normalizeState(serverState);
            var saved = normalizeState(storageRead(stateKey));
            state = resolveInstalledState(fresh, saved);
            if (state.active) storageWrite(stateKey, state); else storageRemove(stateKey);
            renderState();
            return state;
        }

        function setMessage(value, error) {
            if (!message) return;
            message.textContent = text(value);
            message.classList.toggle("is-error", error === true);
        }

        function tileMarkup(item) {
            var button = document.createElement("button");
            button.type = "button";
            button.className = "driver-free-bucket-tile";
            button.dataset.driverFreeBucketOption = "";
            button.dataset.excavatorId = String(item.id);
            button.dataset.excavatorLabel = item.label;
            button.dataset.complexLabel = item.complex_label;
            button.dataset.loadingHorizon = item.loading_horizon;
            button.dataset.loadingBlock = item.loading_block;
            button.dataset.rockType = item.rock_type;
            button.dataset.dumpPoint = item.dump_point;
            button.dataset.isPrimary = item.is_primary ? "true" : "false";
            var primary = item.is_primary ? "<em>Основной</em>" : "";
            button.innerHTML = '<span class="driver-free-bucket-tile-topline"><strong></strong>' + primary + '</span>'
                + '<span class="driver-free-bucket-tile-place"></span>'
                + '<span class="driver-free-bucket-tile-meta"></span>'
                + '<span class="driver-free-bucket-tile-status" data-driver-free-bucket-tile-status></span>';
            button.querySelector("strong").textContent = item.label;
            button.querySelector(".driver-free-bucket-tile-place").textContent = (item.loading_horizon || "Горизонт —") + " · " + (item.loading_block || "Блок —");
            var dumpPointNames = item.dump_points.map(function (point) { return point.name; }).filter(Boolean);
            button.querySelector(".driver-free-bucket-tile-meta").textContent = (item.rock_type || "Порода —") + " · " + (dumpPointNames.join(", ") || "Точки —");
            button.querySelector("[data-driver-free-bucket-tile-status]").textContent = tileStatusLabel(item, false, state);
            if (item.missing_fields.length) {
                var missing = document.createElement("span");
                missing.className = "driver-free-bucket-tile-missing";
                missing.textContent = "Не заполнено: " + item.missing_fields.map(function (field) {
                    return ({loading_horizon: "горизонт", loading_block: "блок", rock_type: "порода", dump_points: "точки разгрузки", catalog_entry: "снимок настроек"})[field] || field;
                }).join(", ");
                button.insertBefore(missing, button.querySelector("[data-driver-free-bucket-tile-status]"));
            }
            if (item.is_primary || !item.available) {
                button.disabled = true;
                button.setAttribute("aria-disabled", "true");
            }
            return button;
        }

        function renderTiles() {
            if (!grid || !catalog) return;
            grid.replaceChildren();
            if (!catalog.excavators.length) {
                var empty = document.createElement("p");
                empty.className = "driver-free-bucket-empty";
                empty.dataset.driverFreeBucketEmpty = "";
                empty.textContent = catalog.stale
                    ? "Локальный справочник экскаваторов ещё не загружен."
                    : "Нет активных экскаваторов.";
                grid.appendChild(empty);
                return;
            }
            catalog.excavators.forEach(function (item) { grid.appendChild(tileMarkup(item)); });
            if (catalog.stale) setMessage("Показан сохранённый справочник. Выбор будет проверен сервером.", false);
            renderState();
        }

        function rememberBaseline() {
            if (!shell || shell.dataset.driverFreeBucketBaselineReady === "true") return;
            ["excavator", "complex", "horizon", "block", "rock"].forEach(function (name) {
                var node = shell.querySelector("[data-driver-context-" + name + "]");
                if (node) shell.dataset["driverFreeBucketBaseline" + name[0].toUpperCase() + name.slice(1)] = node.textContent;
            });
            var dial = shell.querySelector("[data-driver-dial-label]");
            var note = shell.querySelector(".driver-work-note");
            if (dial) shell.dataset.driverFreeBucketBaselineDial = dial.textContent;
            if (note) shell.dataset.driverFreeBucketBaselineNote = note.textContent;
            shell.dataset.driverFreeBucketBaselineReady = "true";
        }

        function setNode(selector, value) {
            var node = shell && shell.querySelector(selector);
            if (node) node.textContent = value;
        }

        function setDialLabel(value) {
            var node = shell && shell.querySelector("[data-driver-dial-label]");
            if (!node) return;
            var label = text(value);
            node.textContent = label;
            node.dataset.driverDialRaw = label;
            delete node.dataset.driverDialFitKey;
            if (typeof windowObject.scheduleDriverDialLabelFit === "function") {
                windowObject.scheduleDriverDialLabelFit(true);
            }
        }

        var mainCardApplied = false;

        function applyMainCard(item) {
            rememberBaseline();
            if (!item) {
                // Возвращать исходные подписи есть смысл только после того, как модуль
                // сам их подменил: иначе каждая отрисовка состояния перетирала подпись
                // круга, которую во время простоя выставляет режим ожидания
                // («ОЖИДАНИЕ ПОГРУЗКИ» превращалось обратно в «НА ЗАГРУЗКУ»).
                if (!mainCardApplied) return;
                mainCardApplied = false;
                setNode("[data-driver-context-excavator]", shell.dataset.driverFreeBucketBaselineExcavator || "—");
                setNode("[data-driver-context-complex]", shell.dataset.driverFreeBucketBaselineComplex || "К-—");
                setNode("[data-driver-context-horizon]", shell.dataset.driverFreeBucketBaselineHorizon || "Горизонт —");
                setNode("[data-driver-context-block]", shell.dataset.driverFreeBucketBaselineBlock || "Блок —");
                setNode("[data-driver-context-rock]", shell.dataset.driverFreeBucketBaselineRock || "—");
                setDialLabel(shell.dataset.driverFreeBucketBaselineDial || "—");
                // Подпись круга во время ожидания принадлежит простою — её не трогаем.
                if (!shell.querySelector(".driver-work-dial-button.is-waiting-operation")) {
                    setNode(".driver-work-note", shell.dataset.driverFreeBucketBaselineNote || "НА ЗАГРУЗКУ");
                }
                return;
            }
            mainCardApplied = true;
            setNode("[data-driver-context-excavator]", item.label);
            setNode("[data-driver-context-complex]", item.complex_label || "К-—");
            setNode("[data-driver-context-horizon]", "Горизонт " + (item.loading_horizon || "—"));
            setNode("[data-driver-context-block]", "Блок " + (item.loading_block || "—"));
            setNode("[data-driver-context-rock]", item.rock_type || "—");
            setDialLabel(item.label);
            setNode(".driver-work-note", state.sync_mode === "review" ? "НУЖНА СВЕРКА" : state.status === "accepted" ? "ПРИНЯТ МАШИНИСТОМ" : "ОЖИДАНИЕ ПРИЁМА");
        }

        function renderState() {
            if (!shell) return;
            var active = state.active && state.selection;
            shell.dataset.driverFreeBucketActive = active ? "true" : "false";
            if (trigger) {
                trigger.classList.toggle("is-current", !!active);
                trigger.classList.toggle("is-local", !!active && state.sync_mode === "local");
                trigger.classList.toggle("is-review", !!active && state.sync_mode === "review");
                trigger.setAttribute("aria-haspopup", "dialog");
                trigger.setAttribute("aria-controls", "driver-free-bucket-dialog");
            }
            if (grid) grid.querySelectorAll("[data-driver-free-bucket-option]").forEach(function (tile) {
                var selected = !!active && positive(tile.dataset.excavatorId) === state.selection.id;
                tile.classList.toggle("is-current", selected);
                if (selected) tile.setAttribute("aria-current", "true"); else tile.removeAttribute("aria-current");
                var status = tile.querySelector("[data-driver-free-bucket-tile-status]");
                var item = catalog && catalog.excavators.find(function (candidate) {
                    return candidate.id === positive(tile.dataset.excavatorId);
                });
                if (status) status.textContent = tileStatusLabel(item, selected, state);
            });
            var current = shell.querySelector("[data-driver-free-bucket-current]");
            if (current) current.hidden = !active;
            setNode("[data-driver-free-bucket-current-label]", active ? "Свободный ковш · " + state.selection.label : "");
            var chip = shell.querySelector("[data-driver-free-bucket-chip]");
            if (chip) {
                chip.hidden = !active;
                chip.textContent = active ? "Свободный ковш · " + state.selection.label : "";
                if (active) chip.title = chip.textContent; else chip.removeAttribute("title");
            }
            var sync = shell.querySelector("[data-driver-free-bucket-sync-state]");
            if (sync) {
                sync.classList.toggle("is-review", state.sync_mode === "review");
                sync.textContent = state.sync_mode === "review"
                    ? "Не подтверждено"
                    : state.sync_mode === "local"
                        ? "Действие сохранено"
                        : state.status === "accepted"
                            ? "Принят машинистом"
                            : state.status === "used" ? "Погружен" : "Ожидание приёма машинистом";
            }
            var remove = shell.querySelector("[data-driver-free-bucket-remove]");
            if (remove) remove.hidden = !active || state.status === "used";
            applyMainCard(active ? state.selection : null);
        }

        function focusWithoutScroll(target) {
            if (!target || typeof target.focus !== "function") return;
            try {
                target.focus({ preventScroll: true });
            } catch (error) {
                target.focus();
            }
        }

        function setOpen(open) {
            if (!sheet) return;
            sheet.hidden = !open;
            if (trigger) trigger.setAttribute("aria-expanded", open ? "true" : "false");
            if (open) {
                returnFocus = document.activeElement;
                windowObject.requestAnimationFrame(function () {
                    var focus = sheet.querySelector(".driver-free-bucket-tile.is-current, [data-driver-free-bucket-option], [data-driver-free-bucket-close]");
                    focusWithoutScroll(focus);
                });
            } else if (returnFocus && typeof returnFocus.focus === "function") {
                focusWithoutScroll(returnFocus);
                returnFocus = null;
                if (windowObject.AppRealtime && typeof windowObject.AppRealtime.wake === "function") windowObject.AppRealtime.wake("driver_free_bucket_modal_closed");
            }
        }

        function selectedSpec(item) {
            if (typeof windowObject.createDriverFreeBucketSelectedEvent !== "function") throw new Error("offline_runtime_unavailable");
            return windowObject.createDriverFreeBucketSelectedEvent({
                truckId: positive(truckId),
                excavatorId: item.id,
                catalogVersion: catalog.version,
                catalogGeneratedAt: catalog.generated_at,
                contextSnapshot: displaySnapshot(item)
            });
        }

        function select(item) {
            if (!outbox || !item || item.is_primary || !item.available || state.active || state.status === "used") return Promise.reject(new Error("free_bucket_unavailable"));
            setMessage("Сохраняю выбор на телефоне…", false);
            return outbox.enqueue(selectedSpec(item)).then(function (event) {
                state = normalizeState({
                    active: true,
                    acceptance_local_id: event.event_id,
                    status: "requested",
                    can_cancel: true,
                    selection: item,
                    sync_mode: "local",
                    version: catalog.version,
                    generated_at: text(event.occurred_at) || new Date().toISOString(),
                    catalog_version: catalog.version,
                    catalog_generated_at: catalog.generated_at
                });
                storageWrite(stateKey, state);
                renderState();
                setOpen(false);
                return event;
            }).catch(function (error) {
                setMessage(error.message === "offline_runtime_unavailable" ? "Локальное хранилище недоступно." : "Выбор не сохранён. Повторите.", true);
                throw error;
            });
        }

        function cancel() {
            if (!outbox || !state.active || state.status === "used") return Promise.resolve(null);
            if (typeof windowObject.createDriverFreeBucketCancelledEvent !== "function") return Promise.reject(new Error("offline_runtime_unavailable"));
            setMessage("Сохраняю отмену на телефоне…", false);
            return outbox.pending().then(function (events) {
                var request = (events || []).slice().reverse().find(function (event) {
                    return event.event_type === "driver.free_bucket.selected"
                        && event.event_id === state.acceptance_local_id;
                });
                return outbox.enqueue(windowObject.createDriverFreeBucketCancelledEvent({
                    acceptanceId: state.acceptance_id,
                    acceptanceLocalId: state.acceptance_local_id,
                    dependsOn: request ? [request.event_id] : []
                }));
            }).then(function (event) {
                state = normalizeState({active: false, status: "cancelled", sync_mode: "local"});
                storageRemove(stateKey);
                renderState();
                setOpen(false);
                return event;
            }).catch(function (error) {
                setMessage("Отмена не сохранена. Повторите.", true);
                throw error;
            });
        }

        function project(events) {
            var projected = normalizeState(state);
            (events || []).slice().sort(function (a, b) { return Number(a.sequence) - Number(b.sequence); }).forEach(function (event) {
                if (event.event_type === "driver.free_bucket.selected" && positive(event.payload && event.payload.truck_id) === positive(truckId)) {
                    var item = itemFromEvent(event, catalog);
                    if (!item) return;
                    projected = normalizeState({
                        active: true,
                        acceptance_local_id: event.event_id,
                        status: "requested",
                        can_cancel: true,
                        selection: item,
                        sync_mode: ["conflict", "auth_required", "invalid"].indexOf(event.state) >= 0 ? "review" : "local",
                        version: Number(event.payload && event.payload.catalog_version || 0),
                        generated_at: text(event.occurred_at),
                        catalog_version: Number(event.payload && event.payload.catalog_version || 0),
                        catalog_generated_at: text(event.payload && event.payload.catalog_generated_at)
                    });
                }
                if (event.event_type === "driver.free_bucket.cancelled") {
                    var cancelPayload = event.payload || {};
                    var cancelsCurrent = (
                        positive(cancelPayload.free_bucket_acceptance_id) === projected.acceptance_id
                        || (
                            text(cancelPayload.free_bucket_acceptance_local_id)
                            && text(cancelPayload.free_bucket_acceptance_local_id) === projected.acceptance_local_id
                        )
                        || (
                            projected.acceptance_local_id
                            && Array.isArray(event.depends_on)
                            && event.depends_on.indexOf(projected.acceptance_local_id) >= 0
                        )
                    );
                    if (!projected.active || !cancelsCurrent) return;
                    if (["conflict", "auth_required", "invalid"].indexOf(event.state) >= 0) {
                        projected.sync_mode = "review";
                    } else {
                        projected = normalizeState({active: false, status: "cancelled", sync_mode: "local"});
                    }
                }
            });
            state = projected;
            if (state.active) storageWrite(stateKey, state); else storageRemove(stateKey);
            renderState();
            return clone(state);
        }

        function bind() {
            if (!shell || !sheet || !trigger) return false;
            rememberBaseline();
            trigger.setAttribute("aria-expanded", "false");
            trigger.addEventListener("click", function (event) { event.stopPropagation(); setOpen(true); });
            sheet.querySelectorAll("[data-driver-free-bucket-close]").forEach(function (button) {
                button.addEventListener("click", function () { setOpen(false); });
            });
            sheet.addEventListener("click", function (event) {
                if (event.target === sheet) return setOpen(false);
                var tile = event.target.closest && event.target.closest("[data-driver-free-bucket-option]");
                if (tile) {
                    var item = catalog.excavators.find(function (candidate) { return candidate.id === positive(tile.dataset.excavatorId); });
                    if (item) select(item).catch(function () {});
                    return;
                }
                if (event.target.closest && event.target.closest("[data-driver-free-bucket-remove]")) cancel().catch(function () {});
            });
            sheet.addEventListener("keydown", function (event) {
                if (event.key === "Escape") { event.preventDefault(); setOpen(false); }
            });
            installCatalog(options.catalog);
            installState(options.state);
            return true;
        }

        return {
            bind: bind,
            installCatalog: installCatalog,
            installState: installState,
            project: project,
            select: select,
            cancel: cancel,
            state: function () { return clone(state); },
            catalog: function () { return clone(catalog); },
            ownsShell: function (candidate) { return shell === candidate; }
        };
    }

    function tileStatusLabel(item, selected, currentState) {
        if (selected) return currentState && currentState.sync_mode === "review" ? "Не подтверждено" : "Выбран";
        return item && item.available === false ? "Недоступно" : "";
    }

    function usableBrowserShell(documentObject, candidate) {
        if (!documentObject || !candidate || candidate.dataset.driverFreeBucketEnabled !== "true") return false;
        if (candidate.isConnected === false) return false;
        return candidate === documentObject.querySelector("[data-driver-shell]");
    }

    function bindBrowser(options) {
        options = options || {};
        var document = root.document;
        var shell = options.shell || document.querySelector("[data-driver-shell]");
        if (!usableBrowserShell(document, shell)) {
            currentController = null;
            if (!shell || shell === document.querySelector("[data-driver-shell]")) pendingFragment = null;
            return null;
        }
        var fragment = pendingFragment;
        pendingFragment = null;
        currentController = createDriverFreeBucketController({
            window: root,
            document: document,
            storage: root.localStorage,
            shell: shell,
            outbox: options.outbox || root.driverOfflineOutbox,
            catalog: fragment && fragment.catalog || readJsonScript(document, "driver-free-bucket-catalog-data"),
            state: fragment && fragment.state || readJsonScript(document, "driver-free-bucket-state-data")
        });
        currentController.bind();
        return currentController;
    }

    root.DriverFreeBucket = {
        bind: bindBrowser,
        receiveFragment: function (catalog, state) { pendingFragment = {catalog: catalog, state: state}; },
        renderProjection: function (shell, events) {
            var activeShell = root.document.querySelector("[data-driver-shell]");
            var requestedShell = shell || activeShell;
            if (!usableBrowserShell(root.document, requestedShell)) {
                currentController = null;
                if (!requestedShell || requestedShell === activeShell) pendingFragment = null;
                return null;
            }
            if (!currentController || !currentController.ownsShell(requestedShell)) {
                currentController = bindBrowser({shell: requestedShell, outbox: root.driverOfflineOutbox});
            }
            return currentController ? currentController.project(events || []) : null;
        }
    };

    if (typeof module !== "undefined" && module.exports) {
        module.exports = {
            createDriverFreeBucketController: createDriverFreeBucketController,
            normalizeCatalog: normalizeCatalog,
            isAuthoritativeCatalog: isAuthoritativeCatalog,
            catalogIsStale: catalogIsStale,
            normalizeState: normalizeState,
            tileStatusLabel: tileStatusLabel,
            displaySnapshot: displaySnapshot,
            stateIsNewer: stateIsNewer,
            resolveInstalledState: resolveInstalledState
        };
    }
})(typeof window !== "undefined" ? window : globalThis);
