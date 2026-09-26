(function (root) {
    "use strict";

    var CATALOG_SCHEMA = "driver-free-bucket-catalog-v1";
    var STATE_SCHEMA = "driver-free-bucket-state-v1";
    var CATALOG_STALE_AFTER_MS = 60 * 60 * 1000;
    var FREE_BUCKET_REQUEST_TTL_MS = 10 * 60 * 1000;
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

    function timestampMs(value) {
        var parsed = Date.parse(text(value));
        return Number.isFinite(parsed) ? parsed : 0;
    }

    function finiteNumber(value) {
        value = Number(value);
        return Number.isFinite(value) ? value : 0;
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
            catalog_generated_at: text(value.catalog_generated_at),
            expires_at: text(value.expires_at || value.free_bucket_expires_at),
            expiry_basis: text(value.expiry_basis),
            expires_local_at_ms: finiteNumber(value.expires_local_at_ms)
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
        /* "review" значит сервер уже ОТКЛОНИЛ эту попытку — она не может быть
           достовернее свежего ответа сервера. Держать её как активную здесь
           означало бы застревать в отклонённом состоянии навсегда: ни выбрать
           новый экскаватор (select() не пускает, пока state.active), ни отменить
           (отменять то, чего сервер не подтверждает, нечего). Только "local"
           (ещё не отправлено) достаточно веская причина не доверять свежему
           ответу — оно ещё может дойти до сервера и стать реальным. */
        var savedIsPending = saved.sync_mode === "local";
        if (!fresh.active && saved.active && savedIsPending && stateIsNewer(saved, fresh)) return saved;
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
        var expiryTimer = null;
        var expiryLifecycleBound = false;

        function nowMs() {
            var value = typeof options.now === "function" ? options.now() : options.now;
            value = Number(value);
            return Number.isFinite(value) ? value : Date.now();
        }

        function stateUsesRequestTtl(candidate) {
            candidate = candidate || {};
            return Boolean(
                candidate.active
                && candidate.selection
                && ["used", "cancelled", "closed"].indexOf(text(candidate.status)) < 0
            );
        }

        function sameAcceptance(left, right) {
            left = left || {};
            right = right || {};
            if (left.acceptance_id && right.acceptance_id) {
                return Number(left.acceptance_id) === Number(right.acceptance_id);
            }
            if (text(left.acceptance_local_id) && text(right.acceptance_local_id)) {
                return text(left.acceptance_local_id) === text(right.acceptance_local_id);
            }
            return false;
        }

        function localFallbackDeadline(candidate) {
            if (!stateUsesRequestTtl(candidate)) return 0;
            var occurredAt = timestampMs(candidate.generated_at);
            if (!occurredAt) return 0;
            return occurredAt + FREE_BUCKET_REQUEST_TTL_MS;
        }

        function prepareInstalledExpiry(candidate, fresh, saved, rawServerState) {
            candidate = normalizeState(candidate);
            fresh = normalizeState(fresh);
            saved = normalizeState(saved);
            rawServerState = rawServerState || {};
            if (!stateUsesRequestTtl(candidate)) {
                candidate.expires_at = "";
                candidate.expiry_basis = "";
                candidate.expires_local_at_ms = 0;
                return candidate;
            }

            var explicitExpiresAt = timestampMs(
                rawServerState.expires_at
                || rawServerState.free_bucket_expires_at
                || fresh.expires_at
            );
            if (explicitExpiresAt && sameAcceptance(candidate, fresh)) {
                var explicitText = text(
                    rawServerState.expires_at
                    || rawServerState.free_bucket_expires_at
                    || fresh.expires_at
                );
                if (
                    sameAcceptance(candidate, saved)
                    && saved.expires_local_at_ms
                    && saved.expires_at === explicitText
                    && !stateIsNewer(fresh, saved)
                ) {
                    candidate.expires_at = saved.expires_at;
                    candidate.expiry_basis = saved.expiry_basis;
                    candidate.expires_local_at_ms = saved.expires_local_at_ms;
                    return candidate;
                }
                var serverNow = timestampMs(rawServerState.server_now || fresh.generated_at);
                var capturedAt = nowMs();
                candidate.expires_at = explicitText;
                candidate.expiry_basis = "server";
                candidate.expires_local_at_ms = serverNow
                    ? capturedAt + (explicitExpiresAt - serverNow)
                    : explicitExpiresAt;
                return candidate;
            }

            if (sameAcceptance(candidate, saved) && saved.expires_local_at_ms) {
                candidate.expires_at = saved.expires_at;
                candidate.expiry_basis = saved.expiry_basis;
                candidate.expires_local_at_ms = saved.expires_local_at_ms;
                return candidate;
            }

            if (["local", "review"].indexOf(candidate.sync_mode) >= 0) {
                candidate.expiry_basis = "local";
                candidate.expires_local_at_ms = localFallbackDeadline(candidate);
            }
            return candidate;
        }

        function expiryDeadline(candidate) {
            if (!stateUsesRequestTtl(candidate)) return 0;
            return finiteNumber(candidate.expires_local_at_ms)
                || (["local", "review"].indexOf(candidate.expiry_basis || candidate.sync_mode) >= 0
                    ? localFallbackDeadline(candidate)
                    : 0);
        }

        function clearExpiryTimer() {
            if (expiryTimer && typeof windowObject.clearTimeout === "function") {
                windowObject.clearTimeout(expiryTimer);
            }
            expiryTimer = null;
        }

        function expireStateIfDue() {
            var deadline = expiryDeadline(state);
            if (!deadline || nowMs() < deadline) return false;
            state = normalizeState({active: false, status: "", sync_mode: "confirmed"});
            storageRemove(stateKey);
            return true;
        }

        function scheduleExpiryTimer() {
            clearExpiryTimer();
            var deadline = expiryDeadline(state);
            if (!deadline || typeof windowObject.setTimeout !== "function") return;
            var delay = deadline - nowMs();
            if (delay <= 0) {
                if (expireStateIfDue()) renderState();
                return;
            }
            expiryTimer = windowObject.setTimeout(function () {
                expiryTimer = null;
                renderState();
            }, delay);
            if (expiryTimer && typeof expiryTimer.unref === "function") expiryTimer.unref();
        }

        function reconcileExpiry() {
            var expired = expireStateIfDue();
            if (expired) renderState(); else scheduleExpiryTimer();
            return expired;
        }

        function onExpiryLifecycleResume() {
            if (document && document.hidden === true) return;
            reconcileExpiry();
        }

        function onExpiryVisibilityChange() {
            if (!document || document.hidden !== true) onExpiryLifecycleResume();
        }

        function bindExpiryLifecycle() {
            if (expiryLifecycleBound) return;
            expiryLifecycleBound = true;
            if (document && typeof document.addEventListener === "function") {
                document.addEventListener("visibilitychange", onExpiryVisibilityChange);
                document.addEventListener("resume", onExpiryLifecycleResume);
            }
            if (windowObject && typeof windowObject.addEventListener === "function") {
                windowObject.addEventListener("pageshow", onExpiryLifecycleResume);
                windowObject.addEventListener("resume", onExpiryLifecycleResume);
            }
        }

        function destroy() {
            clearExpiryTimer();
            if (!expiryLifecycleBound) return;
            expiryLifecycleBound = false;
            if (document && typeof document.removeEventListener === "function") {
                document.removeEventListener("visibilitychange", onExpiryVisibilityChange);
                document.removeEventListener("resume", onExpiryLifecycleResume);
            }
            if (windowObject && typeof windowObject.removeEventListener === "function") {
                windowObject.removeEventListener("pageshow", onExpiryLifecycleResume);
                windowObject.removeEventListener("resume", onExpiryLifecycleResume);
            }
        }

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
            state = prepareInstalledExpiry(
                resolveInstalledState(fresh, saved),
                fresh,
                saved,
                serverState
            );
            expireStateIfDue();
            if (state.active) storageWrite(stateKey, state); else storageRemove(stateKey);
            renderState();
            return state;
        }

        function setMessage(value, error) {
            if (!message) return;
            message.textContent = text(value);
            message.classList.toggle("is-error", error === true);
        }

        // Плитка — крупный номер экскаватора и, если есть, статус («Выбран»/«Недоступно»).
        // Горизонт/блок/порода/точка и «Не заполнено: …» больше не показываются (водителю
        // тут не нужны, только мешали разглядеть номер) — но остаются в data-атрибутах,
        // их бросок в рейс при выборе экскаватора не отображение. Тот же вид задаёт
        // сервер в шаблоне (driver_shift.html) — держать оба места в одном виде.
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
            button.innerHTML = '<strong class="driver-free-bucket-tile-number"></strong>'
                + '<span class="driver-free-bucket-tile-status" data-driver-free-bucket-tile-status></span>';
            button.querySelector(".driver-free-bucket-tile-number").textContent = item.label;
            button.querySelector("[data-driver-free-bucket-tile-status]").textContent = tileStatusLabel(item, false);
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
            setNode(".driver-work-note", state.status === "accepted" ? "ПРИНЯТ МАШИНИСТОМ" : "ОЖИДАНИЕ ПРИЁМА");
        }

        function renderState() {
            expireStateIfDue();
            if (!shell) {
                scheduleExpiryTimer();
                return;
            }
            var active = state.active && state.selection;
            shell.dataset.driverFreeBucketActive = active ? "true" : "false";
            if (trigger) {
                trigger.classList.toggle("is-current", !!active);
                trigger.classList.toggle("is-local", !!active && state.sync_mode === "local");
                trigger.classList.remove("is-review");
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
                sync.classList.remove("is-review");
                sync.textContent = state.sync_mode === "local"
                    ? "Действие сохранено"
                    : state.status === "accepted"
                        ? "Принят машинистом"
                        : state.status === "used" ? "Погружен" : "Ожидание приёма машинистом";
            }
            var remove = shell.querySelector("[data-driver-free-bucket-remove]");
            if (remove) remove.hidden = !active || state.status === "used";
            applyMainCard(active ? state.selection : null);
            if (typeof windowObject.CustomEvent === "function" && typeof windowObject.dispatchEvent === "function") {
                windowObject.dispatchEvent(new windowObject.CustomEvent("driver-free-bucket-state-changed", {
                    detail: {state: clone(state), catalog: clone(catalog)}
                }));
            }
            scheduleExpiryTimer();
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
                var occurredAt = text(event.occurred_at) || new Date(nowMs()).toISOString();
                state = normalizeState({
                    active: true,
                    acceptance_local_id: event.event_id,
                    status: "requested",
                    can_cancel: true,
                    selection: item,
                    sync_mode: "local",
                    version: catalog.version,
                    generated_at: occurredAt,
                    catalog_version: catalog.version,
                    catalog_generated_at: catalog.generated_at,
                    expiry_basis: "local",
                    expires_local_at_ms: timestampMs(occurredAt) + FREE_BUCKET_REQUEST_TTL_MS
                });
                expireStateIfDue();
                if (state.active) storageWrite(stateKey, state); else storageRemove(stateKey);
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
            setMessage("Сохраняю отмену на телефоне…", false);
            return outbox.pending().then(function (events) {
                var request = (events || []).slice().reverse().find(function (event) {
                    return event.event_type === "driver.free_bucket.selected"
                        && event.event_id === state.acceptance_local_id;
                });
                var requestRejected = !!request && ["conflict", "auth_required", "invalid"].indexOf(request.state) >= 0;
                if (requestRejected && !state.acceptance_id) {
                    // Сервер так и не принял выбор — отменять на сервере
                    // нечего. Ставить dependsOn на отклонённое событие давало
                    // dependency_rejected и снова полевое предупреждение по кругу
                    // (26.09.2026, боевой afb373a5); теперь гасим локально
                    // без сетевого запроса.
                    return null;
                }
                if (typeof windowObject.createDriverFreeBucketCancelledEvent !== "function") throw new Error("offline_runtime_unavailable");
                return outbox.enqueue(windowObject.createDriverFreeBucketCancelledEvent({
                    acceptanceId: state.acceptance_id,
                    acceptanceLocalId: state.acceptance_local_id,
                    dependsOn: (request && !requestRejected) ? [request.event_id] : []
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
            expireStateIfDue();
            var projected = normalizeState(state);
            if (projected.sync_mode === "review") {
                // Хранилище/предыдущая установка ещё держит зависшее «на
                // сверке» состояние с ДО этой правки (26.09.2026) — сервер
                // его уже отклонил, ковша нет, гасим сразу же, до разбора
                // новых событий.
                projected = normalizeState({active: false, status: "cancelled", sync_mode: "local"});
            }
            (events || []).slice().sort(function (a, b) { return Number(a.sequence) - Number(b.sequence); }).forEach(function (event) {
                var rejected = ["conflict", "auth_required", "invalid"].indexOf(event.state) >= 0;
                if (event.event_type === "driver.free_bucket.selected" && positive(event.payload && event.payload.truck_id) === positive(truckId)) {
                    if (rejected) {
                        // Сервер не принял выбор — ковша нет и никогда не было.
                        // Раньше это всё равно оставляло active:true с
                        // sync_mode "review" с полевым предупреждением — самосвал
                        // застревал навсегда, ни выбрать заново, ни отменить
                        // было нельзя (26.09.2026, боевой afb373a5).
                        projected = normalizeState({active: false, status: "cancelled", sync_mode: "local"});
                        return;
                    }
                    var item = itemFromEvent(event, catalog);
                    if (!item) return;
                    var occurredAt = text(event.occurred_at);
                    var candidate = normalizeState({
                        active: true,
                        acceptance_local_id: event.event_id,
                        status: "requested",
                        can_cancel: true,
                        selection: item,
                        sync_mode: "local",
                        version: Number(event.payload && event.payload.catalog_version || 0),
                        generated_at: occurredAt,
                        catalog_version: Number(event.payload && event.payload.catalog_version || 0),
                        catalog_generated_at: text(event.payload && event.payload.catalog_generated_at),
                        expiry_basis: "local",
                        expires_local_at_ms: timestampMs(occurredAt) + FREE_BUCKET_REQUEST_TTL_MS
                    });
                    if (expiryDeadline(candidate) && nowMs() >= expiryDeadline(candidate)) {
                        if (
                            projected.status !== "used"
                            && text(projected.acceptance_local_id) === text(event.event_id)
                        ) {
                            projected = normalizeState({active: false, status: "", sync_mode: "confirmed"});
                        }
                        return;
                    }
                    projected = candidate;
                }
                if (event.event_type === "driver.trip.loaded.cancelled") {
                    var restoreSnapshot = event.context_snapshot || {};
                    var restoreServerIds = event.server_ids || {};
                    var confirmedRestore = event.free_bucket_restored === true;
                    var restoreId = positive(
                        (confirmedRestore && restoreServerIds.free_bucket_acceptance_id)
                        || restoreSnapshot.free_bucket_acceptance_id
                    );
                    var restoreLocalId = text(
                        (confirmedRestore && event.free_bucket_client_acceptance_id)
                        || restoreSnapshot.free_bucket_acceptance_local_id
                    );
                    var isFreeBucketCancel = text(restoreSnapshot.authority_type) === "free_bucket"
                        && Boolean(restoreId || restoreLocalId);
                    var currentHasIdentity = Boolean(
                        projected.acceptance_id || projected.acceptance_local_id
                    );
                    var matchesCurrent = !projected.active || (
                        (restoreId && projected.acceptance_id === restoreId)
                        || (restoreLocalId && projected.acceptance_local_id === restoreLocalId)
                        || (
                            confirmedRestore
                            && projected.status === "used"
                            && !currentHasIdentity
                        )
                    );
                    if (!isFreeBucketCancel || !matchesCurrent) return;

                    var restoreDeadline = finiteNumber(
                        restoreSnapshot.free_bucket_expires_local_at_ms
                        || (confirmedRestore && event.free_bucket_expires_local_at_ms)
                    );
                    if (!restoreDeadline && projected.expires_local_at_ms) {
                        restoreDeadline = finiteNumber(projected.expires_local_at_ms);
                    }
                    if (!restoreDeadline) {
                        restoreDeadline = timestampMs(
                            restoreSnapshot.free_bucket_expires_at
                            || (confirmedRestore && event.free_bucket_expires_at)
                        );
                    }
                    if (!restoreDeadline) return;
                    if (nowMs() >= restoreDeadline) {
                        projected = normalizeState({
                            active: false,
                            status: "closed",
                            sync_mode: confirmedRestore ? "confirmed" : "local"
                        });
                        return;
                    }

                    var restoreSelection = projected.selection || normalizeItem({
                        id: restoreSnapshot.excavator_id || (event.payload && event.payload.excavator_id),
                        label: restoreSnapshot.excavator_label,
                        complex_label: restoreSnapshot.complex_label,
                        loading_horizon: restoreSnapshot.loading_horizon,
                        loading_block: restoreSnapshot.loading_block,
                        rock_type_id: restoreSnapshot.rock_type_id,
                        rock_type: restoreSnapshot.rock_type_name,
                        dump_points: restoreSnapshot.dump_points,
                        available: true,
                        missing_fields: []
                    });
                    if (!restoreSelection) return;
                    projected = normalizeState({
                        active: true,
                        acceptance_id: restoreId || projected.acceptance_id,
                        acceptance_local_id: restoreLocalId || projected.acceptance_local_id,
                        status: "accepted",
                        can_cancel: true,
                        selection: restoreSelection,
                        sync_mode: confirmedRestore ? "confirmed" : "local",
                        generated_at: text(projected.generated_at || event.occurred_at),
                        version: Number(event.version || projected.version || 0),
                        catalog_version: projected.catalog_version,
                        catalog_generated_at: projected.catalog_generated_at,
                        expires_at: text(
                            restoreSnapshot.free_bucket_expires_at
                            || (confirmedRestore && event.free_bucket_expires_at)
                            || projected.expires_at
                        ),
                        expiry_basis: "restored",
                        expires_local_at_ms: restoreDeadline
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
                    // Намерение водителя отменить — истина, даже если сама
                    // отмена не подтверждена сервером (в т.ч. dependency_rejected
                    // на уже отклонённый выбор — правило 1, замкнутый круг с
                    // "review" больше не создаётся).
                    projected = normalizeState({active: false, status: "cancelled", sync_mode: "local"});
                }
            });
            state = projected;
            expireStateIfDue();
            if (state.active) storageWrite(stateKey, state); else storageRemove(stateKey);
            renderState();
            return clone(state);
        }

        function confirmManualCancellation(event, result) {
            if (!event || event.event_type !== "driver.trip.loaded.cancelled") return clone(state);
            result = result || {};
            var snapshot = event.context_snapshot || {};
            var serverIds = result.server_ids || event.server_ids || {};
            var snapshotId = positive(
                serverIds.free_bucket_acceptance_id || snapshot.free_bucket_acceptance_id
            );
            var snapshotLocalId = text(
                result.free_bucket_client_acceptance_id
                || event.free_bucket_client_acceptance_id
                || snapshot.free_bucket_acceptance_local_id
            );
            var matchesCurrent = state.active && (
                (snapshotId && state.acceptance_id === snapshotId)
                || (snapshotLocalId && state.acceptance_local_id === snapshotLocalId)
            );
            if (result.free_bucket_restored === false) {
                if (matchesCurrent) state = normalizeState({active: false, status: "closed", sync_mode: "confirmed"});
            } else if (result.free_bucket_restored === true) {
                var confirmedEvent = Object.assign({}, event, {
                    free_bucket_restored: true,
                    server_ids: clone(serverIds),
                    free_bucket_client_acceptance_id: snapshotLocalId,
                    free_bucket_expires_at: text(
                        result.free_bucket_expires_at || event.free_bucket_expires_at
                    ),
                    free_bucket_expires_local_at_ms: finiteNumber(
                        result.free_bucket_expires_local_at_ms
                        || event.free_bucket_expires_local_at_ms
                        || snapshot.free_bucket_expires_local_at_ms
                    )
                });
                project([confirmedEvent]);
                var restoredMatchesCurrent = state.active && (
                    (snapshotId && state.acceptance_id === snapshotId)
                    || (snapshotLocalId && state.acceptance_local_id === snapshotLocalId)
                );
                if (restoredMatchesCurrent) {
                    state.acceptance_id = snapshotId || state.acceptance_id;
                    state.acceptance_local_id = snapshotLocalId || state.acceptance_local_id;
                    state.sync_mode = "confirmed";
                }
            }
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

        bindExpiryLifecycle();

        return {
            bind: bind,
            installCatalog: installCatalog,
            installState: installState,
            project: project,
            confirmManualCancellation: confirmManualCancellation,
            select: select,
            cancel: cancel,
            reconcileExpiry: reconcileExpiry,
            destroy: destroy,
            state: function () {
                reconcileExpiry();
                return clone(state);
            },
            catalog: function () { return clone(catalog); },
            ownsShell: function (candidate) { return shell === candidate; }
        };
    }

    function tileStatusLabel(item, selected) {
        if (selected) return "Выбран";
        return item && item.available === false ? "Недоступно" : "";
    }

    function usableBrowserShell(documentObject, candidate) {
        if (!documentObject || !candidate || candidate.dataset.driverFreeBucketEnabled !== "true") return false;
        if (candidate.isConnected === false) return false;
        return candidate === documentObject.querySelector("[data-driver-shell]");
    }

    function dropCurrentController() {
        if (currentController && typeof currentController.destroy === "function") currentController.destroy();
        currentController = null;
    }

    function bindBrowser(options) {
        options = options || {};
        var document = root.document;
        var shell = options.shell || document.querySelector("[data-driver-shell]");
        if (!usableBrowserShell(document, shell)) {
            dropCurrentController();
            if (!shell || shell === document.querySelector("[data-driver-shell]")) pendingFragment = null;
            return null;
        }
        var fragment = pendingFragment;
        pendingFragment = null;
        dropCurrentController();
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
                dropCurrentController();
                if (!requestedShell || requestedShell === activeShell) pendingFragment = null;
                return null;
            }
            if (!currentController || !currentController.ownsShell(requestedShell)) {
                currentController = bindBrowser({shell: requestedShell, outbox: root.driverOfflineOutbox});
            }
            return currentController ? currentController.project(events || []) : null;
        },
        currentState: function () {
            return currentController ? currentController.state() : null;
        },
        currentCatalog: function () {
            return currentController ? currentController.catalog() : null;
        },
        confirmManualCancellation: function (event, result) {
            return currentController
                ? currentController.confirmManualCancellation(event, result)
                : null;
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
