(function (root) {
    "use strict";

    var DB_NAME = "copper-free-bucket-v1";
    var DB_VERSION = 1;
    var STORE_NAME = "catalogs";
    var LS_PREFIX = "excavator-free-bucket-catalog-v1:";
    var CONSUMED_PREFIX = "excavator-free-bucket-consumed-v1:";
    var OPEN_STATE_KEY = "eoFreeBucket";
    var modal = null;
    var input = null;
    var results = null;
    var acceptButton = null;
    var shell = null;
    var queueEvent = null;
    var fieldOutbox = null;
    var bindTruckCard = null;
    var showNotice = null;
    var invalidateRefresh = null;
    var catalog = [];
    var catalogMeta = {};
    var selectedTruck = null;
    var opener = null;
    var historyOwned = false;
    var closing = false;

    function text(value) {
        return String(value == null ? "" : value).trim();
    }

    function numberOf(item) {
        return text(item && (item.number || item.garage_number || item.label));
    }

    function truckIdOf(item) {
        return text(item && (item.id || item.truck_id));
    }

    function normalize(value) {
        return text(value).toLocaleUpperCase("ru-RU").replace(/\s+/g, "");
    }

    function catalogScope() {
        var current = shell || document.querySelector("[data-eo-shell]");
        return current ? text(current.dataset.eoAccessId || "anonymous") : "anonymous";
    }

    function consumedScope() {
        var current = shell || document.querySelector("[data-eo-shell]");
        return catalogScope() + ":" + (current ? text(current.dataset.eoCurrentExcavatorId) : "");
    }

    function consumedReferences() {
        try {
            return JSON.parse(root.localStorage.getItem(CONSUMED_PREFIX + consumedScope()) || "{}") || {};
        } catch (error) {
            return {};
        }
    }

    function storeConsumedReferences(references, consumed) {
        var stored = consumedReferences();
        (references || []).filter(Boolean).forEach(function (reference) {
            if (consumed) stored[text(reference)] = Date.now();
            else delete stored[text(reference)];
        });
        var compact = Object.keys(stored).sort(function (left, right) {
            return Number(stored[right] || 0) - Number(stored[left] || 0);
        }).slice(0, 100).reduce(function (result, reference) {
            result[reference] = stored[reference];
            return result;
        }, {});
        try {
            root.localStorage.setItem(CONSUMED_PREFIX + consumedScope(), JSON.stringify(compact));
        } catch (error) {}
    }

    function eventAcceptanceReferences(payload) {
        payload = payload || {};
        return [payload.free_bucket_acceptance_id, payload.free_bucket_acceptance_local_id].map(text).filter(Boolean);
    }

    function itemWasConsumed(item) {
        var stored = consumedReferences();
        return [item && item.id, item && item.free_bucket_acceptance_id, item && item.client_acceptance_id]
            .map(text).filter(Boolean).some(function (reference) { return Boolean(stored[reference]); });
    }

    function localStorageRead(scope) {
        try {
            return JSON.parse(root.localStorage.getItem(LS_PREFIX + scope) || "null");
        } catch (error) {
            return null;
        }
    }

    function localStorageWrite(scope, snapshot) {
        try {
            root.localStorage.setItem(LS_PREFIX + scope, JSON.stringify(snapshot));
        } catch (error) {}
    }

    function openDb() {
        return new Promise(function (resolve, reject) {
            if (!root.indexedDB) { reject(new Error("IndexedDB unavailable")); return; }
            var request;
            try { request = root.indexedDB.open(DB_NAME, DB_VERSION); } catch (error) { reject(error); return; }
            request.onupgradeneeded = function () {
                if (!request.result.objectStoreNames.contains(STORE_NAME)) {
                    request.result.createObjectStore(STORE_NAME, {keyPath: "scope"});
                }
            };
            request.onsuccess = function () { resolve(request.result); };
            request.onerror = function () { reject(request.error || new Error("Catalog storage unavailable")); };
        });
    }

    function idbRead(scope) {
        return openDb().then(function (db) {
            return new Promise(function (resolve, reject) {
                var tx = db.transaction(STORE_NAME, "readonly");
                var request = tx.objectStore(STORE_NAME).get(scope);
                request.onsuccess = function () { resolve(request.result ? request.result.snapshot : null); };
                request.onerror = function () { reject(request.error); };
            });
        });
    }

    function idbWrite(scope, snapshot) {
        return openDb().then(function (db) {
            return new Promise(function (resolve, reject) {
                var tx = db.transaction(STORE_NAME, "readwrite");
                tx.objectStore(STORE_NAME).put({scope: scope, snapshot: snapshot});
                tx.oncomplete = function () { resolve(); };
                tx.onerror = function () { reject(tx.error); };
                tx.onabort = function () { reject(tx.error); };
            });
        });
    }

    function normalizeSnapshot(raw) {
        if (Array.isArray(raw)) return {updated_at: "", version: 0, trucks: raw};
        raw = raw && typeof raw === "object" ? raw : {};
        return {
            updated_at: text(raw.updated_at || raw.generated_at),
            version: Number(raw.version || 0),
            trucks: Array.isArray(raw.trucks) ? raw.trucks : []
        };
    }

    function readEmbeddedCatalog(currentShell) {
        var node = currentShell && currentShell.querySelector("#eo-free-bucket-directory-data");
        if (!node) return null;
        try {
            return normalizeSnapshot(JSON.parse(node.textContent || "{}"));
        } catch (error) {
            return null;
        }
    }

    function readEmbeddedCards(currentShell) {
        var node = currentShell && currentShell.querySelector("#eo-free-bucket-cards-data");
        if (!node) return [];
        try {
            var parsed = JSON.parse(node.textContent || "[]");
            return Array.isArray(parsed) ? parsed : [];
        } catch (error) {
            return [];
        }
    }

    function acceptanceReferenceSet(items) {
        return (items || []).reduce(function (references, item) {
            [item && item.id, item && item.free_bucket_acceptance_id, item && item.client_acceptance_id]
                .map(text).filter(Boolean).forEach(function (reference) { references[reference] = true; });
            return references;
        }, Object.create(null));
    }

    function confirmedAcceptanceIsAbsent(record, snapshot) {
        var event = record && record.event;
        var result = record && record.result || {};
        if (!event || event.event_type !== "excavator.free_bucket.accepted") return false;
        snapshot = snapshot || {};
        var snapshotVersion = Number(snapshot.version || 0);
        var acceptedVersion = Number(result.server_version || result.version || 0);
        if (!snapshotVersion || !acceptedVersion || snapshotVersion < acceptedVersion) return false;
        var references = acceptanceReferenceSet(snapshot.cards || []);
        var serverId = text(result.server_ids && result.server_ids.free_bucket_acceptance_id);
        return !references[text(event.event_id)] && (!serverId || !references[serverId]);
    }

    function serverAcceptanceSnapshot(currentShell) {
        return {
            version: Number(root.document && root.document.body && root.document.body.dataset.operationalStateVersion || 0),
            cards: readEmbeddedCards(currentShell)
        };
    }

    function useSnapshot(snapshot) {
        snapshot = normalizeSnapshot(snapshot);
        var selectedTruckId = truckIdOf(selectedTruck);
        catalog = snapshot.trucks.filter(function (item) {
            return truckIdOf(item) && numberOf(item);
        });
        selectedTruck = selectedTruckId
            ? catalog.find(function (item) { return truckIdOf(item) === selectedTruckId; }) || null
            : null;
        catalogMeta = snapshot;
        renderSearch();
    }

    function hydrateCatalog(currentShell) {
        var scope = catalogScope();
        var embedded = readEmbeddedCatalog(currentShell);
        if (embedded) {
            useSnapshot(embedded);
            localStorageWrite(scope, embedded);
            idbWrite(scope, embedded).catch(function () {});
            return Promise.resolve(embedded);
        }
        return idbRead(scope).catch(function () { return null; }).then(function (stored) {
            stored = stored || localStorageRead(scope);
            if (stored) useSnapshot(stored);
            return stored;
        });
    }

    function catalogAgeLabel() {
        var parsed = Date.parse(catalogMeta.updated_at || "");
        if (!parsed) return "Локально";
        var ageMinutes = Math.max(0, Math.floor((Date.now() - parsed) / 60000));
        if (ageMinutes < 2) return "Актуально";
        if (ageMinutes < 60) return ageMinutes + " мин";
        return "Устарело";
    }

    function clearNode(node) {
        while (node && node.firstChild) node.removeChild(node.firstChild);
    }

    function make(tag, className, value) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (value !== undefined) node.textContent = text(value);
        return node;
    }

    function searchMatches(query) {
        var needle = normalize(query);
        if (!needle) return [];
        var digits = needle.replace(/\D+/g, "");
        return catalog.filter(function (item) {
            var value = normalize(numberOf(item));
            if (value.indexOf(needle) >= 0) return true;
            return Boolean(digits && value.replace(/\D+/g, "").indexOf(digits) >= 0);
        }).sort(function (left, right) {
            var leftNumber = normalize(numberOf(left));
            var rightNumber = normalize(numberOf(right));
            var leftExact = leftNumber === needle ? 0 : 1;
            var rightExact = rightNumber === needle ? 0 : 1;
            return leftExact - rightExact || leftNumber.localeCompare(rightNumber, "ru", {numeric: true});
        });
    }

    function resultDescription(item) {
        var pieces = [];
        var type = text(item.truck_type || item.type || item.model || "Самосвал");
        var assignment = text(item.primary_assignment_label || item.primary_assignment || item.excavator_label);
        var availability = text(item.availability_label || item.availability || item.state_label);
        if (type) pieces.push(type);
        if (assignment) pieces.push("Основное: " + assignment);
        if (availability) pieces.push(availability);
        if (item.is_active === false) pieces.push("Неактивен");
        return pieces.join(" · ");
    }

    function itemCanBeAccepted(item) {
        return Boolean(item && item.is_active !== false && item.can_accept_free_bucket !== false);
    }

    function appendActiveItems() {
        if (!results) return 0;
        var cards = Array.prototype.slice.call(document.querySelectorAll(
            "[data-eo-truck-card][data-eo-free-bucket='1']:not([hidden])"
        )).filter(function (card) {
            return card.dataset.eoFreeBucketUsed !== "1";
        });
        if (!cards.length) return 0;
        results.appendChild(make("h3", "eo-free-bucket__active-title", "Уже приняты"));
        cards.forEach(function (card) {
            var row = make("div", "eo-free-bucket__active-item");
            var copy = make("div");
            copy.appendChild(make("strong", "", "№ " + text(card.dataset.eoTruckNumber)));
            copy.appendChild(make("span", "", card.classList.contains("is-saved-on-device")
                ? "Сохранено на телефоне" : "Подтверждено сервером"));
            var remove = make("button", "eo-free-bucket__remove", "Убрать");
            remove.type = "button";
            remove.dataset.eoFreeBucketRemove = "1";
            remove.dataset.truckId = text(card.dataset.truckId);
            remove.dataset.acceptanceId = text(card.dataset.eoFreeBucketAcceptanceId);
            remove.dataset.acceptanceLocalId = text(card.dataset.eoFreeBucketAcceptanceLocalId);
            remove.setAttribute("aria-label", "Убрать самосвал № " + text(card.dataset.eoTruckNumber) + " из свободного ковша");
            row.appendChild(copy);
            row.appendChild(remove);
            results.appendChild(row);
        });
        return cards.length;
    }

    function renderSearch() {
        if (!results || !input) return;
        clearNode(results);
        var query = text(input.value);
        var activeCount = appendActiveItems();
        if (!query) {
            if (!activeCount) results.appendChild(make("p", "eo-free-bucket__message", "Введите бортовой номер."));
            selectedTruck = null;
            if (acceptButton) acceptButton.disabled = true;
            return;
        }
        if (!catalog.length) {
            results.appendChild(make("p", "eo-free-bucket__message is-error", "Локальный справочник самосвалов ещё не загружен."));
            selectedTruck = null;
            if (acceptButton) acceptButton.disabled = true;
            return;
        }
        var matches = searchMatches(query);
        if (!matches.length) {
            results.appendChild(make("p", "eo-free-bucket__message is-error", "Самосвал не найден в локальном справочнике. Номер сохранён в поле."));
            selectedTruck = null;
            if (acceptButton) acceptButton.disabled = true;
            return;
        }
        if (matches.length === 1 && itemCanBeAccepted(matches[0])) selectedTruck = matches[0];
        else if (!selectedTruck || matches.indexOf(selectedTruck) < 0) selectedTruck = null;
        if (selectedTruck && !itemCanBeAccepted(selectedTruck)) selectedTruck = null;
        matches.forEach(function (item) {
            var button = make("button", "eo-free-bucket__result" + (selectedTruck === item ? " is-selected" : ""));
            button.type = "button";
            button.dataset.eoFreeBucketResultId = truckIdOf(item);
            button.disabled = !itemCanBeAccepted(item);
            var copy = make("div");
            copy.appendChild(make("strong", "", "№ " + numberOf(item)));
            copy.appendChild(make("span", "", resultDescription(item) || "Самосвал"));
            button.appendChild(copy);
            button.appendChild(make("b", "eo-free-bucket__result-status", catalogAgeLabel()));
            results.appendChild(button);
        });
        if (acceptButton) acceptButton.disabled = !selectedTruck;
    }

    function setUnderlyingBlocked(blocked) {
        var current = document.querySelector("[data-eo-shell]");
        if (!current) return;
        if (blocked) {
            current.setAttribute("inert", "");
            current.setAttribute("aria-hidden", "true");
        } else {
            current.removeAttribute("inert");
            current.removeAttribute("aria-hidden");
        }
    }

    function finishClose() {
        if (!modal || modal.hidden) return;
        modal.hidden = true;
        modal.setAttribute("aria-hidden", "true");
        document.body.classList.remove("eo-free-bucket-open");
        setUnderlyingBlocked(false);
        historyOwned = false;
        closing = false;
        if (opener && document.contains(opener)) opener.focus();
        opener = null;
        if (root.AppRealtime && typeof root.AppRealtime.wake === "function") {
            root.AppRealtime.wake("free_bucket_modal_closed");
        }
    }

    function requestClose() {
        if (!modal || modal.hidden || closing) return;
        if (historyOwned && history.state && history.state[OPEN_STATE_KEY]) {
            closing = true;
            history.back();
        } else {
            finishClose();
        }
    }

    function openModal(button) {
        if (!modal || !shell || shell.dataset.eoCurrentExcavatorId === "") return;
        if (root.ExcavatorHourlyReport && root.ExcavatorHourlyReport.isOpen()) return;
        opener = button;
        selectedTruck = null;
        input.value = "";
        renderSearch();
        modal.hidden = false;
        modal.setAttribute("aria-hidden", "false");
        document.body.classList.add("eo-free-bucket-open");
        setUnderlyingBlocked(true);
        if (!(history.state && history.state[OPEN_STATE_KEY])) {
            var state = Object.assign({}, history.state || {});
            state[OPEN_STATE_KEY] = true;
            history.pushState(state, "", location.href);
            historyOwned = true;
        }
        input.focus({preventScroll: true});
    }

    function cardForTruck(truckId) {
        var current = document.querySelector("[data-eo-shell]");
        return current && current.querySelector('[data-eo-truck-card][data-truck-id="' + String(truckId).replace(/"/g, "") + '"]');
    }

    function highlightCard(card) {
        if (!card) return;
        card.classList.remove("is-free-bucket-highlight");
        void card.offsetWidth;
        card.classList.add("is-free-bucket-highlight");
        card.focus({preventScroll: true});
        root.setTimeout(function () { card.classList.remove("is-free-bucket-highlight"); }, 900);
    }

    function gridForShell() {
        var current = document.querySelector("[data-eo-shell]");
        return current && current.querySelector(".eo-dashboard-truck-grid");
    }

    function normalizeGrid() {
        var grid = gridForShell();
        if (!grid) return;
        var cards = Array.prototype.filter.call(grid.querySelectorAll("[data-eo-truck-card]"), function (card) {
            return !card.hidden;
        });
        grid.classList.toggle("is-empty", cards.length === 0);
        grid.classList.toggle("is-many", cards.length > 9);
        // Ряды считаются по числу видимых карточек, как в шаблоне: три
        // зарезервированных ряда на один-два самосвала сплющивали карточку
        // до квадрата и оставляли пустую полосу над точками разгрузки.
        grid.classList.toggle("is-rows-4", cards.length > 9);
        grid.classList.toggle("is-rows-3", cards.length > 6 && cards.length <= 9);
        grid.classList.toggle("is-rows-2", cards.length > 3 && cards.length <= 6);
        grid.classList.toggle("is-rows-1", cards.length > 0 && cards.length <= 3);
        grid.classList.toggle("is-free-bucket-overflow", cards.length > 12);
        var empty = grid.querySelector(".eo-dashboard-empty");
        if (empty) empty.hidden = cards.length > 0;
    }

    function iconUrl(item) {
        var value = text(item.icon_url || item.icon);
        if (/^(?:https?:|\/)/.test(value)) return value;
        if (value) return "/static/" + value.replace(/^static\//, "");
        var current = document.querySelector("[data-eo-shell]");
        return current ? current.dataset.eoTruckGreenIcon || "/static/img/equipment/truck-gray.png" : "/static/img/equipment/truck-gray.png";
    }

    function renderLocalCard(item, event, conflict) {
        var truckId = truckIdOf(item) || text(event && event.payload && event.payload.truck_id);
        if (!truckId) return null;
        var existing = cardForTruck(truckId);
        if (existing) {
            if (existing.dataset.eoFreeBucket === "1" && event && event.event_id) {
                existing.dataset.eoFreeBucketAcceptanceLocalId = event.event_id;
                existing.classList.toggle("is-free-bucket-conflict", !!conflict);
            }
            return existing;
        }
        var grid = gridForShell();
        if (!grid) return null;
        var number = numberOf(item) || text(event && event.payload && event.payload.truck_number) || truckId;
        var statusKey = text(item.status_key || "blue").toLowerCase();
        if (["green", "yellow", "blue", "orange", "red", "gray"].indexOf(statusKey) < 0) statusKey = "blue";
        var card = make("button", "eo-truck-card eo-dashboard-truck-card status-" + statusKey + " is-free-bucket is-saved-on-device" + (conflict ? " is-free-bucket-conflict" : ""));
        card.type = "button";
        card.draggable = true;
        card.dataset.eoTruckCard = "";
        card.dataset.eoDashboardTruck = "";
        card.dataset.eoFreeBucket = "1";
        card.dataset.eoFreeBucketAcceptanceLocalId = text(event && event.event_id);
        card.dataset.truckId = truckId;
        card.dataset.eoTruckNumber = number;
        card.dataset.eoTruckDetailId = truckId;
        card.dataset.eoEquipmentState = text(item.state_code || "assigned");
        card.dataset.eoCanLoad = conflict ? "0" : "1";
        card.dataset.eoManualAvailable = "0";
        card.dataset.eoOpenTripId = "";
        card.dataset.eoPlanPercent = "";
        card.dataset.planPercent = "";
        card.dataset.planStatus = "no_plan_group";
        card.dataset.planProgressStatus = "empty";
        card.style.setProperty("--eo-truck-progress", "0%");
        card.style.setProperty("--eo-truck-total-progress", "0%");
        card.style.setProperty("--plan-progress", "0%");
        card.style.setProperty("--truck-plan-percent", "0");
        var presence = make("b", "eo-driver-presence presence-" + text(item.driver_presence_code || "offline"));
        presence.title = text(item.driver_presence_label || "Связь не проверена");
        presence.setAttribute("aria-label", "Водитель: " + presence.title);
        var icon = make("i");
        icon.setAttribute("aria-hidden", "true");
        var image = document.createElement("img");
        image.src = iconUrl(item);
        image.alt = "";
        icon.appendChild(image);
        card.appendChild(presence);
        card.appendChild(icon);
        card.appendChild(make("strong", "", number));
        card.appendChild(make("span", "", conflict ? "Требуется сверка" : text(item.state_label || item.availability_label || "Принят временно")));
        card.appendChild(make("small", "", "—"));
        card.appendChild(make("div", "eo-free-bucket-card-marker", conflict ? "Свободный ковш · конфликт" : "Свободный ковш"));
        var accent = make("u", "eo-free-bucket-card-accent");
        accent.setAttribute("aria-hidden", "true");
        card.appendChild(accent);
        grid.insertBefore(card, grid.firstElementChild);
        if (typeof bindTruckCard === "function") bindTruckCard(card);
        normalizeGrid();
        return card;
    }

    function findCatalogItem(truckId, payload) {
        return catalog.find(function (item) { return truckIdOf(item) === text(truckId); }) || {
            id: truckId,
            number: payload && payload.truck_number,
            truck_type: payload && payload.truck_type,
            primary_assignment_label: payload && payload.primary_assignment_label
        };
    }

    function acceptanceEvents(events) {
        return (events || []).filter(function (event) {
            return event.event_type === "excavator.free_bucket.accepted";
        });
    }

    function cancellationReferences(events) {
        var refs = Object.create(null);
        (events || []).forEach(function (event) {
            if (event.event_type !== "excavator.free_bucket.cancelled") return;
            if (["pending", "syncing"].indexOf(event.sync_state) < 0) return;
            refs[text(event.payload && (event.payload.free_bucket_acceptance_local_id || event.payload.free_bucket_acceptance_id))] = true;
        });
        return refs;
    }

    function cardForAcceptance(reference) {
        reference = text(reference);
        if (!reference) return null;
        return Array.prototype.find.call(
            document.querySelectorAll("[data-eo-truck-card][data-eo-free-bucket='1']"),
            function (card) {
                return text(card.dataset.eoFreeBucketAcceptanceLocalId) === reference
                    || text(card.dataset.eoFreeBucketAcceptanceId) === reference;
            }
        ) || null;
    }

    function terminalAttention(event) {
        return ["conflict", "invalid", "auth_required"].indexOf(event && event.sync_state) >= 0;
    }

    function rejectedAcceptance(event, result) {
        var status = text(result && result.status) || text(event && event.sync_state);
        return Boolean(
            event
            && event.event_type === "excavator.free_bucket.accepted"
            && ["conflict", "invalid"].indexOf(status) >= 0
        );
    }

    function reconcileEvents(events) {
        var cancelled = cancellationReferences(events);
        (events || []).forEach(function (event) {
            if (event.event_type !== "excavator.free_bucket.cancelled") return;
            var payload = event.payload || {};
            var reference = text(payload.free_bucket_acceptance_local_id || payload.free_bucket_acceptance_id);
            var card = reference ? cardForAcceptance(reference) : cardForTruck(payload.truck_id);
            if (!card || card.dataset.eoFreeBucket !== "1") return;
            if (["pending", "syncing"].indexOf(event.sync_state) >= 0) card.remove();
            else if (terminalAttention(event)) markAttention(event, {});
        });
        acceptanceEvents(events).forEach(function (event) {
            if (cancelled[event.event_id]) return;
            if (rejectedAcceptance(event)) {
                var rejectedCard = cardForAcceptance(event.event_id);
                if (rejectedCard) rejectedCard.remove();
                return;
            }
            var payload = event.payload || {};
            var card = renderLocalCard(findCatalogItem(payload.truck_id, payload), event,
                terminalAttention(event));
            if (card && ["pending", "syncing"].indexOf(event.sync_state) >= 0) card.classList.add("is-saved-on-device");
        });
        (events || []).forEach(function (event) {
            if (event.event_type !== "excavator.free_bucket.loaded") return;
            var payload = event.payload || {};
            var reference = text(payload.free_bucket_acceptance_id || payload.free_bucket_acceptance_local_id);
            var card = reference ? cardForAcceptance(reference) : cardForTruck(payload.truck_id);
            if (["pending", "syncing"].indexOf(event.sync_state) >= 0) {
                storeConsumedReferences(eventAcceptanceReferences(payload), true);
                if (card) removeLoadedCard(card);
            } else if (terminalAttention(event)) {
                storeConsumedReferences(eventAcceptanceReferences(payload), false);
                renderEmbeddedCards(shell);
                markAttention(event, {});
            }
        });
        normalizeGrid();
    }

    function reconcileConfirmed(records, snapshot) {
        (records || []).slice().sort(function (left, right) {
            return Number(left && left.event && left.event.sequence || 0)
                - Number(right && right.event && right.event.sequence || 0);
        }).forEach(function (record) {
            var event = record && record.event;
            var result = record && record.result || {};
            if (!event || event.event_type.indexOf("excavator.free_bucket.") !== 0) return;
            var payload = event.payload || {};
            if (event.event_type === "excavator.free_bucket.accepted") {
                if (confirmedAcceptanceIsAbsent(record, snapshot)) {
                    var absentServerId = text(result.server_ids && result.server_ids.free_bucket_acceptance_id);
                    storeConsumedReferences([event.event_id, absentServerId], true);
                    var absentCard = absentServerId ? cardForAcceptance(absentServerId) : cardForAcceptance(event.event_id);
                    if (absentCard && absentCard.dataset.eoFreeBucket === "1") absentCard.remove();
                    return;
                }
                var acceptedCard = renderLocalCard(findCatalogItem(payload.truck_id, payload), event, false);
                if (acceptedCard) {
                    acceptedCard.classList.remove("is-saved-on-device");
                    acceptedCard.dataset.eoFreeBucketAcceptanceLocalId = event.event_id;
                    acceptedCard.dataset.eoFreeBucketAcceptanceId = text(
                        result.server_ids && result.server_ids.free_bucket_acceptance_id
                    );
                }
                return;
            }
            var reference = text(payload.free_bucket_acceptance_id || payload.free_bucket_acceptance_local_id);
            var card = reference ? cardForAcceptance(reference) : cardForTruck(payload.truck_id);
            if (event.event_type === "excavator.free_bucket.loaded") {
                storeConsumedReferences(eventAcceptanceReferences(payload), true);
                removeLoadedCard(card);
            } else if (event.event_type === "excavator.free_bucket.cancelled") {
                if (card && card.dataset.eoFreeBucket === "1") card.remove();
            }
        });
        normalizeGrid();
    }

    function reconcileDurableState(snapshot) {
        if (!fieldOutbox) return Promise.resolve();
        var confirmed = typeof fieldOutbox.confirmed === "function"
            ? fieldOutbox.confirmed()
            : Promise.resolve([]);
        return confirmed.then(function (records) { return reconcileConfirmed(records, snapshot); }).then(function () {
            if (typeof fieldOutbox.pending === "function") {
                return fieldOutbox.pending().then(reconcileEvents);
            }
        });
    }

    function acceptSelected() {
        if (!selectedTruck || !queueEvent || !acceptButton) return;
        var existing = cardForTruck(truckIdOf(selectedTruck));
        if (existing) {
            requestClose();
            root.setTimeout(function () { highlightCard(existing); }, 80);
            return;
        }
        acceptButton.disabled = true;
        acceptButton.textContent = "Сохраняем…";
        var item = selectedTruck;
        if (typeof invalidateRefresh === "function") invalidateRefresh();
        queueEvent("excavator.free_bucket.accepted", {
            truck_id: Number(truckIdOf(item)),
            truck_number: numberOf(item),
            truck_type: text(item.truck_type || item.type || item.model),
            primary_assignment_label: text(item.primary_assignment_label || item.primary_assignment || item.excavator_label),
            catalog_version: Number(catalogMeta.version || 0),
            catalog_updated_at: text(catalogMeta.updated_at)
        }, {idPrefix: "free-bucket-accept", noDependency: true}).then(function (event) {
            var card = renderLocalCard(item, event, false);
            requestClose();
            root.setTimeout(function () { highlightCard(card); }, 80);
            if (typeof showNotice === "function") showNotice(navigator.onLine === false
                ? "Свободный ковш сохранён на телефоне" : "Самосвал принят под свободный ковш");
        }).catch(function (error) {
            acceptButton.disabled = false;
            var message = make("p", "eo-free-bucket__message is-error", error.message || "Не удалось сохранить приём на телефоне.");
            results.appendChild(message);
        }).finally(function () {
            acceptButton.textContent = "Принять на погрузку";
        });
    }

    function cancelAcceptedCard(card) {
        if (!queueEvent || !card || card.dataset.eoFreeBucket !== "1") return Promise.resolve(false);
        if (card.dataset.eoFreeBucketCancelPending === "1" || card.dataset.eoFreeBucketUsed === "1") {
            return Promise.resolve(false);
        }
        var localId = text(card.dataset.eoFreeBucketAcceptanceLocalId);
        var serverId = text(card.dataset.eoFreeBucketAcceptanceId);
        var reference = localId || serverId;
        var truckId = text(card.dataset.truckId);
        if (!reference) {
            if (typeof showNotice === "function") showNotice("Не найден идентификатор временного приёма.");
            return Promise.resolve(false);
        }
        var previousCanLoad = text(card.dataset.eoCanLoad);
        card.dataset.eoFreeBucketCancelPending = "1";
        card.dataset.eoCanLoad = "0";
        card.classList.add("is-free-bucket-cancel-pending");
        if (typeof invalidateRefresh === "function") invalidateRefresh();
        return queueEvent("excavator.free_bucket.cancelled", {
            free_bucket_acceptance_id: serverId,
            free_bucket_acceptance_local_id: serverId ? "" : localId,
            truck_id: Number(truckId)
        }, {idPrefix: "free-bucket-cancel", dependsOn: serverId ? [] : [localId]}).then(function () {
            var current = cardForAcceptance(reference);
            if (current && current.dataset.eoFreeBucket === "1") current.remove();
            normalizeGrid();
            renderSearch();
            if (typeof showNotice === "function") showNotice("Самосвал убран из свободного ковша");
            return true;
        }).catch(function (error) {
            delete card.dataset.eoFreeBucketCancelPending;
            card.dataset.eoCanLoad = previousCanLoad;
            card.classList.remove("is-free-bucket-cancel-pending");
            if (typeof showNotice === "function") showNotice(error.message || "Не удалось сохранить отмену.");
            return false;
        });
    }

    function removeAcceptance(button) {
        var reference = text(button && button.dataset.acceptanceLocalId);
        var card = reference ? cardForAcceptance(reference) : cardForTruck(button && button.dataset.truckId);
        if (!card) return;
        button.disabled = true;
        cancelAcceptedCard(card).then(function (cancelled) {
            if (!cancelled && button.isConnected) button.disabled = false;
        });
    }

    function removeLoadedCard(card) {
        if (!card || card.dataset.eoFreeBucket !== "1") return;
        storeConsumedReferences([
            card.dataset.eoFreeBucketAcceptanceId,
            card.dataset.eoFreeBucketAcceptanceLocalId
        ], true);
        card.remove();
        normalizeGrid();
    }

    function renderEmbeddedCards(currentShell) {
        readEmbeddedCards(currentShell).forEach(function (item) {
            if (item.is_used || itemWasConsumed(item)) return;
            var cardItem = Object.assign({}, item, {id: item.truck_id});
            var event = {
                event_id: text(item.client_acceptance_id || item.free_bucket_acceptance_local_id),
                event_type: "excavator.free_bucket.accepted",
                payload: {truck_id: item.truck_id, truck_number: item.number}
            };
            var card = renderLocalCard(cardItem, event, false);
            if (card) {
                card.classList.remove("is-saved-on-device");
                card.dataset.eoFreeBucketAcceptanceId = text(item.id || item.free_bucket_acceptance_id);
                card.dataset.eoFreeBucketAcceptanceLocalId = event.event_id;
            }
        });
    }

    function handleConfirmed(event, result) {
        if (!event) return;
        var payload = event.payload || {};
        if (event.event_type === "excavator.free_bucket.accepted") {
            var card = cardForTruck(payload.truck_id);
            if (card) {
                card.classList.remove("is-saved-on-device");
                card.dataset.eoFreeBucketAcceptanceLocalId = event.event_id;
                card.dataset.eoFreeBucketAcceptanceId = text(result && result.server_ids && result.server_ids.free_bucket_acceptance_id);
            }
        } else if (event.event_type === "excavator.free_bucket.loaded") {
            var loadedReference = text(payload.free_bucket_acceptance_id || payload.free_bucket_acceptance_local_id);
            storeConsumedReferences(eventAcceptanceReferences(payload), true);
            removeLoadedCard(loadedReference ? cardForAcceptance(loadedReference) : cardForTruck(payload.truck_id));
        } else if (event.event_type === "excavator.free_bucket.cancelled") {
            var cancelledReference = text(payload.free_bucket_acceptance_local_id || payload.free_bucket_acceptance_id);
            var cancelled = cancelledReference ? cardForAcceptance(cancelledReference) : cardForTruck(payload.truck_id);
            if (cancelled && cancelled.dataset.eoFreeBucket === "1") cancelled.remove();
            normalizeGrid();
        }
    }

    function markAttention(event, result) {
        if (!event || event.event_type.indexOf("excavator.free_bucket.") !== 0) return;
        var payload = event.payload || {};
        if (rejectedAcceptance(event, result)) {
            var rejectedCard = cardForAcceptance(event.event_id);
            if (rejectedCard) rejectedCard.remove();
            normalizeGrid();
            renderSearch();
            if (typeof showNotice === "function") showNotice((result && (result.message || result.error)) || "Самосвал нельзя принять под свободный ковш.");
            return;
        }
        var card = cardForTruck(payload.truck_id);
        if (card) {
            card.classList.add("is-free-bucket-conflict");
            card.dataset.eoCanLoad = "0";
            var marker = card.querySelector(".eo-free-bucket-card-marker");
            if (marker) marker.textContent = "Свободный ковш · конфликт";
        }
        if (typeof showNotice === "function") showNotice((result && (result.message || result.error)) || "Свободный ковш требует сверки.");
    }

    function attachShell(options) {
        options = options || {};
        shell = options.shell || document.querySelector("[data-eo-shell]");
        queueEvent = options.queueEvent || queueEvent;
        fieldOutbox = options.fieldOutbox || fieldOutbox;
        bindTruckCard = options.bindTruckCard || bindTruckCard;
        showNotice = options.showNotice || showNotice;
        invalidateRefresh = options.invalidateRefresh || invalidateRefresh;
        var snapshot = serverAcceptanceSnapshot(shell);
        renderEmbeddedCards(shell);
        hydrateCatalog(shell);
        normalizeGrid();
        reconcileDurableState(snapshot).catch(function () {});
        if (modal && !modal.hidden) setUnderlyingBlocked(true);
        return {
            reconcileEvents: reconcileEvents,
            handleConfirmed: handleConfirmed,
            markAttention: markAttention,
            markLoaded: removeLoadedCard,
            cancelAccepted: cancelAcceptedCard,
            normalizeGrid: normalizeGrid
        };
    }

    function init() {
        modal = document.querySelector("[data-eo-free-bucket-modal]");
        if (!modal || modal.dataset.eoFreeBucketBound === "1") return;
        modal.dataset.eoFreeBucketBound = "1";
        input = modal.querySelector("[data-eo-free-bucket-input]");
        results = modal.querySelector("[data-eo-free-bucket-results]");
        acceptButton = modal.querySelector("[data-eo-free-bucket-accept]");
        document.addEventListener("click", function (event) {
            var open = event.target.closest && event.target.closest("[data-eo-free-bucket-open]");
            if (open) { event.preventDefault(); openModal(open); return; }
            if (modal.hidden) return;
            var dismiss = event.target.closest && event.target.closest("[data-eo-free-bucket-dismiss]");
            if (dismiss) { event.preventDefault(); requestClose(); return; }
            var key = event.target.closest && event.target.closest("[data-eo-free-bucket-key]");
            if (key) {
                event.preventDefault();
                var value = key.dataset.eoFreeBucketKey;
                if (value === "clear") input.value = "";
                else if (value === "erase") input.value = input.value.slice(0, -1);
                else if (/^\d$/.test(value) && input.value.length < 16) input.value += value;
                selectedTruck = null;
                renderSearch();
                input.focus({preventScroll: true});
                return;
            }
            var result = event.target.closest && event.target.closest("[data-eo-free-bucket-result-id]");
            if (result) {
                selectedTruck = catalog.find(function (item) { return truckIdOf(item) === result.dataset.eoFreeBucketResultId; }) || null;
                if (selectedTruck && !itemCanBeAccepted(selectedTruck)) selectedTruck = null;
                renderSearch();
                return;
            }
            var remove = event.target.closest && event.target.closest("[data-eo-free-bucket-remove]");
            if (remove) { removeAcceptance(remove); return; }
            if (event.target.closest && event.target.closest("[data-eo-free-bucket-accept]")) acceptSelected();
        });
        document.addEventListener("keydown", function (event) {
            if (modal.hidden) return;
            if (event.key === "Escape") { event.preventDefault(); requestClose(); return; }
            if (/^\d$/.test(event.key) && input.value.length < 16) {
                event.preventDefault(); input.value += event.key; selectedTruck = null; renderSearch(); return;
            }
            if (event.key === "Backspace") {
                event.preventDefault(); input.value = input.value.slice(0, -1); selectedTruck = null; renderSearch(); return;
            }
            if (event.key === "Enter" && selectedTruck && !acceptButton.disabled) {
                event.preventDefault(); acceptSelected(); return;
            }
            if (event.key === "Tab") {
                var focusable = Array.prototype.filter.call(modal.querySelectorAll("button:not([disabled]), [tabindex]:not([tabindex='-1'])"), function (node) {
                    return !node.hidden;
                });
                if (!focusable.length) return;
                var first = focusable[0], last = focusable[focusable.length - 1];
                if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
                else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
            }
        });
        window.addEventListener("popstate", function () { if (!modal.hidden) finishClose(); });
        window.addEventListener("operational-state-refresh-applied", function () {
            shell = document.querySelector("[data-eo-shell]");
            var snapshot = serverAcceptanceSnapshot(shell);
            hydrateCatalog(shell);
            renderEmbeddedCards(shell);
            normalizeGrid();
            reconcileDurableState(snapshot).catch(function () {});
            if (modal && !modal.hidden) setUnderlyingBlocked(true);
        });
    }

    if (typeof document !== "undefined") {
        if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init, {once: true});
        else init();
    }

    root.ExcavatorFreeBucket = {
        init: init,
        attachShell: attachShell,
        reconcileEvents: reconcileEvents,
        reconcileConfirmed: reconcileConfirmed,
        handleConfirmed: handleConfirmed,
        markAttention: markAttention,
        markLoaded: removeLoadedCard,
        cancelAccepted: cancelAcceptedCard,
        normalizeGrid: normalizeGrid,
        isOpen: function () { return Boolean(modal && !modal.hidden); }
    };

    if (typeof module !== "undefined" && module.exports) {
        module.exports = {
            confirmedAcceptanceIsAbsent: confirmedAcceptanceIsAbsent
        };
    }
})(typeof window !== "undefined" ? window : globalThis);
