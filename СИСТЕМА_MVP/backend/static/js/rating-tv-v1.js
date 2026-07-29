(function (window, document) {
    "use strict";

    function readJson(id, fallback) {
        var node = document.getElementById(id);
        if (!node) return fallback;
        try {
            return JSON.parse(node.textContent || "");
        } catch (_error) {
            return fallback;
        }
    }

    var config = readJson("rating-tv-config", {});
    var previewPayload = readJson("rating-tv-preview-payload", null);
    var root = document.querySelector("[data-rating-tv]");
    if (!root) return;

    var elements = {
        grid: root.querySelector("[data-rating-grid]"),
        message: root.querySelector("[data-rating-message]"),
        status: root.querySelector("[data-rating-status]"),
        updatedAt: root.querySelector("[data-updated-at]"),
        refreshCountdown: root.querySelector("[data-refresh-countdown]"),
        rotationCountdown: root.querySelector("[data-rotation-countdown]"),
        rotationToggle: root.querySelector("[data-rotation-toggle]"),
        previous: root.querySelector("[data-group-previous]"),
        next: root.querySelector("[data-group-next]"),
        programToggle: root.querySelector("[data-program-toggle]"),
        programCount: root.querySelector("[data-program-count]"),
        fullscreen: root.querySelector("[data-fullscreen-toggle]"),
        period: root.querySelector("[data-rating-period]"),
        composition: root.querySelector("[data-watch-composition]"),
        shiftType: root.querySelector("[data-shift-type]"),
        qaDay: root.querySelector("[data-qa-day]"),
        qaDayCount: root.querySelector("[data-qa-day-count]")
    };

    var state = {
        payload: null,
        sourceFingerprint: "",
        activeGroupKey: "",
        selectedPeriod: "",
        selectedComposition: "",
        shiftType: config.initialShiftType || "night",
        availablePeriods: [],
        availableCompositions: [],
        groupCache: new Map(),
        presentationPlaylist: [],
        refreshSeconds: Number(config.refreshSeconds) || 300,
        refreshRemaining: Number(config.refreshSeconds) || 300,
        rotationSeconds: Number(config.rotationSeconds) || 15,
        rotationRemaining: Number(config.rotationSeconds) || 15,
        rotationPlaying: true,
        qaPreview: config.qaPreview === true,
        requestGeneration: 0,
        requestController: null,
        requestInFlight: false,
        timerId: null
    };

    function formatSeconds(value) {
        var seconds = Math.max(0, Math.floor(Number(value) || 0));
        var minutes = Math.floor(seconds / 60);
        var remainder = seconds % 60;
        return (
            String(minutes).padStart(2, "0")
            + ":"
            + String(remainder).padStart(2, "0")
        );
    }

    function parseDate(value) {
        if (!value) return null;
        var date = new Date(value);
        return Number.isNaN(date.getTime()) ? null : date;
    }

    function formatDateTime(value) {
        var date = parseDate(value);
        if (!date) return "—";
        return new Intl.DateTimeFormat("ru-RU", {
            day: "2-digit",
            month: "2-digit",
            year: "numeric",
            hour: "2-digit",
            minute: "2-digit"
        }).format(date);
    }

    function formatPeriod(item) {
        if (!item) return "Текущий период";
        if (item.starts_on && item.ends_before) {
            var starts = String(item.starts_on).split("-");
            var ends = String(item.ends_before).split("-");
            if (starts.length === 3 && ends.length === 3) {
                if (starts[0] === ends[0]) {
                    return (
                        starts[2] + "." + starts[1]
                        + " — "
                        + ends[2] + "." + ends[1] + "." + ends[0]
                    );
                }
                return (
                    starts[2] + "." + starts[1] + "." + starts[0]
                    + " — "
                    + ends[2] + "." + ends[1] + "." + ends[0]
                );
            }
        }
        if (item.name) return item.name;
        return [item.starts_on, item.ends_before]
            .filter(Boolean)
            .join(" — ");
    }

    function setSelectOptions(
        select,
        items,
        selectedValue,
        labelBuilder,
        placeholder
    ) {
        if (!select) return;
        var fragment = document.createDocumentFragment();
        var selectedExists = items.some(function (item) {
            return String(item.id) === String(selectedValue);
        });
        if (!selectedExists) {
            var emptyOption = document.createElement("option");
            emptyOption.value = "";
            emptyOption.textContent = placeholder;
            emptyOption.selected = true;
            fragment.appendChild(emptyOption);
        }
        items.forEach(function (item) {
            var option = document.createElement("option");
            option.value = String(item.id);
            option.textContent = labelBuilder(item);
            if (String(item.id) === String(selectedValue)) {
                option.selected = true;
            }
            fragment.appendChild(option);
        });
        select.replaceChildren(fragment);
        select.disabled = (
            items.length === 0
            || (items.length === 1 && selectedExists)
        );
    }

    function updateScopeControls(payload) {
        state.availablePeriods = Array.isArray(payload.available_rating_periods)
            ? payload.available_rating_periods
            : [];
        state.availableCompositions = Array.isArray(
            payload.available_watch_compositions
        )
            ? payload.available_watch_compositions
            : [];

        if (payload.rating_period && payload.rating_period.id != null) {
            state.selectedPeriod = String(payload.rating_period.id);
        } else if (
            Object.prototype.hasOwnProperty.call(payload, "rating_period")
        ) {
            state.selectedPeriod = "";
        }
        if (
            payload.watch_composition
            && payload.watch_composition.id != null
        ) {
            state.selectedComposition = String(
                payload.watch_composition.id
            );
        } else if (
            Object.prototype.hasOwnProperty.call(
                payload,
                "watch_composition"
            )
        ) {
            state.selectedComposition = "";
        } else if (
            !state.selectedComposition
            && state.availableCompositions.length
        ) {
            state.selectedComposition = String(
                state.availableCompositions[0].id
            );
        }
        if (payload.shift_type === "day" || payload.shift_type === "night") {
            state.shiftType = payload.shift_type;
        }

        setSelectOptions(
            elements.period,
            state.availablePeriods,
            state.selectedPeriod,
            formatPeriod,
            "Период не выбран"
        );
        setSelectOptions(
            elements.composition,
            state.availableCompositions,
            state.selectedComposition,
            function (item) {
                return item.name || item.code || "Состав вахты";
            },
            "Состав не выбран"
        );
        if (elements.shiftType) {
            elements.shiftType.value = state.shiftType;
        }
    }

    function showMessage(title, text) {
        if (!elements.message || !elements.grid) return;
        var titleNode = elements.message.querySelector("strong");
        var textNode = elements.message.querySelector("span");
        if (titleNode) titleNode.textContent = title;
        if (textNode) textNode.textContent = text || "";
        elements.message.hidden = false;
        elements.grid.hidden = true;
    }

    function initials(fullName) {
        return String(fullName || "?")
            .trim()
            .split(/\s+/)
            .slice(0, 2)
            .map(function (part) {
                return part.slice(0, 1).toUpperCase();
            })
            .join("") || "?";
    }

    function photoUrl(employeeId) {
        if (
            !config.photoUrlTemplate
            || Number(employeeId) <= 0
        ) {
            return "";
        }
        return String(config.photoUrlTemplate).replace(
            "__employee_id__",
            encodeURIComponent(String(employeeId))
        );
    }

    function movementFor(entry) {
        if (
            entry.position_delta != null
            && entry.position_delta !== ""
            && Number.isFinite(Number(entry.position_delta))
        ) {
            return Number(entry.position_delta);
        }
        return 0;
    }

    function setMovementContent(movement, delta) {
        movement.className = "rating-tv__movement";
        if (delta > 0) {
            movement.classList.add("is-up");
            movement.textContent = "↑ " + delta;
        } else if (delta < 0) {
            movement.classList.add("is-down");
            movement.textContent = "↓ " + Math.abs(delta);
        } else {
            movement.classList.add("is-same");
            movement.textContent = "—";
        }
    }

    function createAvatar(entry) {
        var avatar = document.createElement("span");
        avatar.className = "rating-tv__avatar";
        avatar.textContent = initials(entry.full_name);
        var url = photoUrl(entry.employee_id);
        if (!url) return avatar;

        var image = document.createElement("img");
        image.alt = "";
        image.loading = "eager";
        image.decoding = "async";
        image.addEventListener("load", function () {
            avatar.textContent = "";
            avatar.appendChild(image);
        });
        image.addEventListener("error", function () {
            image.remove();
        });
        image.src = url;
        return avatar;
    }

    function updateRatingRow(row, entry) {
        var place = Number(entry.place);
        row.className = "rating-tv__row";
        row.dataset.employeeId = String(entry.employee_id || "");
        row.dataset.place = String(entry.place == null ? "" : entry.place);
        row.dataset.displayOrder = String(
            entry.display_order == null ? "" : entry.display_order
        );
        if (place >= 1 && place <= 5) {
            row.classList.add("is-premium", "is-place-" + place);
        }

        var placeNode = row._ratingPlaceNode;
        if (!placeNode) {
            placeNode = document.createElement("span");
            placeNode.className = "rating-tv__place";
            row._ratingPlaceNode = placeNode;
        }
        placeNode.replaceChildren();
        var gem = document.createElement("b");
        gem.textContent = "◆";
        gem.setAttribute("aria-hidden", "true");
        placeNode.appendChild(gem);
        placeNode.appendChild(
            document.createTextNode(String(entry.place == null ? "—" : entry.place))
        );

        var name = document.createElement("strong");
        name.className = "rating-tv__name";
        name.textContent = entry.full_name || "Сотрудник не указан";
        name.title = name.textContent;

        var equipment = document.createElement("span");
        equipment.className = "rating-tv__equipment";
        equipment.textContent = Array.isArray(entry.equipment)
            ? (entry.equipment.join(", ") || "Техника не указана")
            : (entry.equipment || "Техника не указана");
        equipment.title = equipment.textContent;

        var score = document.createElement("span");
        score.className = "rating-tv__score";
        var scoreValue = document.createElement("b");
        scoreValue.textContent = (
            entry.score == null || entry.score === ""
                ? "—"
                : String(entry.score)
        );
        var scoreLabel = document.createElement("small");
        scoreLabel.textContent = "балл";
        score.append(scoreValue, scoreLabel);

        var movement = document.createElement("span");
        setMovementContent(movement, movementFor(entry));

        row.replaceChildren(
            placeNode,
            createAvatar(entry),
            name,
            equipment,
            score,
            movement
        );
        return row;
    }

    function createRatingRow(entry) {
        return updateRatingRow(document.createElement("li"), entry);
    }

    function layoutGrid() {
        if (!elements.grid || elements.grid.hidden) return;
        var count = elements.grid.children.length;
        if (!count) return;
        var boardHeight = elements.grid.clientHeight
            || elements.grid.parentElement.clientHeight
            || window.innerHeight;
        var minimumRowHeight = window.innerHeight < 850 ? 34 : 44;
        var rowLimit = window.innerWidth >= 1700 ? 18 : 14;
        var maximumRows = Math.max(
            1,
            Math.min(
                rowLimit,
                Math.floor(boardHeight / minimumRowHeight)
            )
        );
        var columns = Math.max(1, Math.ceil(count / maximumRows));
        var rows = Math.max(1, Math.ceil(count / columns));
        elements.grid.style.setProperty("--rating-columns", String(columns));
        elements.grid.style.setProperty("--rating-rows", String(rows));
        elements.grid.classList.toggle(
            "is-dense",
            boardHeight / rows < 42
        );
    }

    function renderEntries(entries) {
        if (!elements.grid || !elements.message) return;
        var ordered = entries.slice().sort(function (left, right) {
            var leftOrder = Number(left.display_order || left.place || 0);
            var rightOrder = Number(right.display_order || right.place || 0);
            return leftOrder - rightOrder;
        });
        var existingSlots = Array.from(elements.grid.children);
        var fragment = document.createDocumentFragment();
        ordered.forEach(function (entry, index) {
            var slot = existingSlots[index];
            fragment.appendChild(
                slot
                    ? updateRatingRow(slot, entry)
                    : createRatingRow(entry)
            );
        });
        elements.grid.replaceChildren(fragment);
        elements.message.hidden = true;
        elements.grid.hidden = false;
        layoutGrid();
    }

    function groupKey(periodId, compositionId, shiftType) {
        return [
            String(periodId || ""),
            String(compositionId || ""),
            String(shiftType || "")
        ].join(":");
    }

    function currentGroupKey() {
        return groupKey(
            state.selectedPeriod,
            state.selectedComposition,
            state.shiftType
        );
    }

    function currentGroupDescriptor() {
        return {
            periodId: state.selectedPeriod,
            compositionId: state.selectedComposition,
            shiftType: state.shiftType
        };
    }

    function descriptorKey(item) {
        return groupKey(
            item.periodId,
            item.compositionId,
            item.shiftType
        );
    }

    function russianGroupCount(value) {
        var number = Math.abs(Number(value) || 0);
        var lastTwo = number % 100;
        var last = number % 10;
        if (last === 1 && lastTwo !== 11) return number + " группа";
        if (
            last >= 2
            && last <= 4
            && (lastTwo < 12 || lastTwo > 14)
        ) {
            return number + " группы";
        }
        return number + " групп";
    }

    function updateProgramUi() {
        var currentKey = currentGroupKey();
        var currentIndex = state.presentationPlaylist.findIndex(
            function (item) {
                return descriptorKey(item) === currentKey;
            }
        );
        var currentIsIncluded = currentIndex >= 0;
        var count = state.presentationPlaylist.length;
        if (elements.programCount) {
            elements.programCount.textContent = russianGroupCount(count);
        }
        if (elements.programToggle) {
            var label = elements.programToggle.querySelector("b");
            var icon = elements.programToggle.querySelector("span");
            if (label) {
                label.textContent = currentIsIncluded
                    ? (count > 1 ? "Убрать" : "В показе")
                    : "В показ";
            }
            if (icon) {
                icon.textContent = currentIsIncluded ? "✓" : "＋";
            }
            elements.programToggle.disabled = (
                !state.selectedComposition
                || (currentIsIncluded && count <= 1)
            );
        }
        var hasPresentation = count > 1;
        if (elements.rotationToggle) {
            elements.rotationToggle.disabled = !hasPresentation;
        }
        if (elements.previous) elements.previous.disabled = !hasPresentation;
        if (elements.next) elements.next.disabled = !hasPresentation;
    }

    function ensureInitialProgramGroup() {
        if (
            state.presentationPlaylist.length
            || !state.selectedComposition
        ) {
            updateProgramUi();
            return;
        }
        state.presentationPlaylist.push(currentGroupDescriptor());
        updateProgramUi();
    }

    function toggleCurrentProgramGroup() {
        if (!state.selectedComposition) return;
        var currentKey = currentGroupKey();
        var index = state.presentationPlaylist.findIndex(function (item) {
            return descriptorKey(item) === currentKey;
        });
        if (index >= 0) {
            if (state.presentationPlaylist.length <= 1) return;
            state.presentationPlaylist.splice(index, 1);
            var nextGroup = state.presentationPlaylist[
                index % state.presentationPlaylist.length
            ];
            state.selectedPeriod = nextGroup.periodId;
            state.selectedComposition = nextGroup.compositionId;
            state.shiftType = nextGroup.shiftType;
            if (elements.period) {
                elements.period.value = state.selectedPeriod || "";
            }
            if (elements.composition) {
                elements.composition.value = (
                    state.selectedComposition || ""
                );
            }
            if (elements.shiftType) {
                elements.shiftType.value = state.shiftType;
            }
        } else {
            state.presentationPlaylist.push(currentGroupDescriptor());
        }
        state.rotationRemaining = state.rotationSeconds;
        updateProgramUi();
        updateCountdowns();
        if (index >= 0) {
            loadRating({replaceRequest: true});
        }
    }

    function payloadGroupKey(payload) {
        var periodId = (
            payload.rating_period
            && payload.rating_period.id != null
        )
            ? payload.rating_period.id
            : state.selectedPeriod;
        var compositionId = (
            payload.watch_composition
            && payload.watch_composition.id != null
        )
            ? payload.watch_composition.id
            : state.selectedComposition;
        return groupKey(
            periodId,
            compositionId,
            payload.shift_type || state.shiftType
        );
    }

    function decoratePayloadWithDeltas(payload, previousPayload) {
        var previousPlaces = new Map();
        var previousEntries = (
            previousPayload
            && Array.isArray(previousPayload.entries)
        )
            ? previousPayload.entries
            : [];
        previousEntries.forEach(function (entry) {
            previousPlaces.set(
                String(entry.employee_id),
                Number(entry.place)
            );
        });
        var entries = Array.isArray(payload.entries)
            ? payload.entries.map(function (entry) {
                var decorated = Object.assign({}, entry);
                if (
                    decorated.position_delta == null
                    || decorated.position_delta === ""
                    || !Number.isFinite(Number(decorated.position_delta))
                ) {
                    var previous = previousPlaces.get(
                        String(decorated.employee_id)
                    );
                    decorated.position_delta = previous == null
                        ? 0
                        : Number(previous) - Number(decorated.place);
                }
                return decorated;
            })
            : [];
        return Object.assign({}, payload, {entries: entries});
    }

    function renderPayloadMetadata(payload) {
        state.payload = payload;
        updateScopeControls(payload);
        ensureInitialProgramGroup();

        if (elements.status) {
            elements.status.textContent = payload.official
                ? "Подтверждённый результат"
                : "Предварительный результат";
        }
        if (elements.updatedAt) {
            elements.updatedAt.textContent = formatDateTime(
                payload.generated_at
            );
        }
        if (elements.qaDay && payload.qa_day != null) {
            elements.qaDay.textContent = String(payload.qa_day);
        }
        if (elements.qaDayCount && payload.qa_day_count != null) {
            elements.qaDayCount.textContent = String(payload.qa_day_count);
        }
    }

    function renderPayload(payload) {
        renderPayloadMetadata(payload);
        state.activeGroupKey = payloadGroupKey(payload);

        var entries = Array.isArray(payload.entries) ? payload.entries : [];
        if (!payload.available || !entries.length) {
            showMessage(
                "Рейтинг пока не рассчитан",
                payload.status || "Для выбранной группы ещё нет результата."
            );
            return;
        }
        renderEntries(entries);
    }

    function updateRenderedMovements(entries) {
        if (!elements.grid) return;
        var deltas = new Map(
            entries.map(function (entry) {
                return [
                    String(entry.employee_id),
                    movementFor(entry)
                ];
            })
        );
        Array.from(elements.grid.children).forEach(function (row) {
            var movement = row.querySelector(".rating-tv__movement");
            if (!movement) return;
            setMovementContent(
                movement,
                deltas.get(String(row.dataset.employeeId)) || 0
            );
        });
    }

    function apiUrl() {
        var url = new URL(config.apiUrl, window.location.origin);
        url.searchParams.set("shift_type", state.shiftType);
        if (state.selectedPeriod) {
            url.searchParams.set("rating_period", state.selectedPeriod);
        }
        if (state.selectedComposition) {
            url.searchParams.set(
                "watch_composition",
                state.selectedComposition
            );
        }
        return url.toString();
    }

    function bootstrapSelection(payload) {
        updateScopeControls(payload);
        return (
            state.selectedComposition
            && state.availableCompositions.length > 1
        );
    }

    async function loadRating(options) {
        options = options || {};
        if (state.qaPreview) {
            if (previewPayload) {
                var preview = Object.assign({}, previewPayload, {
                    shift_type: state.shiftType,
                    shift_type_label: (
                        state.shiftType === "day" ? "Дневная" : "Ночная"
                    )
                });
                renderPayload(preview);
            } else {
                showMessage(
                    "Тестовый макет недоступен",
                    "Локальный payload не был передан сервером."
                );
            }
            state.refreshRemaining = state.refreshSeconds;
            return;
        }

        if (options.replaceRequest && state.requestController) {
            state.requestGeneration += 1;
            state.requestController.abort();
            state.requestController = null;
            state.requestInFlight = false;
        }
        var requestedGroupKey = currentGroupKey();
        var cachedGroup = state.groupCache.get(requestedGroupKey);
        var cacheAgeMilliseconds = cachedGroup
            ? Date.now() - cachedGroup.fetchedAt
            : Number.POSITIVE_INFINITY;
        if (
            !options.forceRefresh
            && cachedGroup
            && cacheAgeMilliseconds < state.refreshSeconds * 1000
        ) {
            state.sourceFingerprint = cachedGroup.fingerprint;
            renderPayload(cachedGroup.payload);
            state.refreshRemaining = Math.max(
                1,
                Math.ceil(
                    (
                        state.refreshSeconds * 1000
                        - cacheAgeMilliseconds
                    ) / 1000
                )
            );
            return;
        }
        if (state.requestInFlight) return;

        state.requestGeneration += 1;
        var generation = state.requestGeneration;
        if (state.requestController) {
            state.requestController.abort();
        }
        var controller = new AbortController();
        state.requestController = controller;
        state.requestInFlight = true;

        try {
            var response = await window.fetch(apiUrl(), {
                method: "GET",
                credentials: "same-origin",
                cache: "no-store",
                headers: {"Accept": "application/json"},
                signal: controller.signal
            });
            var payload = await response.json();
            if (generation !== state.requestGeneration) return;

            if (!response.ok) {
                if (
                    response.status === 400
                    && bootstrapSelection(payload)
                ) {
                    state.requestInFlight = false;
                    await loadRating({replaceRequest: true});
                    return;
                }
                if ([401, 403, 409].includes(response.status)) {
                    state.payload = null;
                    state.activeGroupKey = "";
                    state.groupCache.clear();
                    showMessage(
                        "Рейтинг временно недоступен",
                        payload.error || "Проверьте доступ и выбранную группу."
                    );
                } else if (state.payload && elements.status) {
                    elements.status.textContent = "Показан последний снимок";
                } else {
                    showMessage(
                        "Не удалось получить рейтинг",
                        payload.error || "Сервер не вернул готовый снимок."
                    );
                }
                return;
            }

            var fingerprint = String(payload.source_fingerprint || "");
            var responseGroupKey = payloadGroupKey(payload);
            var previousCachedGroup = state.groupCache.get(responseGroupKey);
            if (
                fingerprint
                && previousCachedGroup
                && fingerprint === previousCachedGroup.fingerprint
            ) {
                var unchangedPayload = decoratePayloadWithDeltas(
                    payload,
                    previousCachedGroup.payload
                );
                state.groupCache.set(responseGroupKey, {
                    payload: unchangedPayload,
                    fingerprint: fingerprint,
                    fetchedAt: Date.now()
                });
                state.sourceFingerprint = fingerprint;
                if (
                    state.activeGroupKey === responseGroupKey
                    && state.payload
                ) {
                    renderPayloadMetadata(unchangedPayload);
                    updateRenderedMovements(unchangedPayload.entries);
                } else {
                    renderPayload(unchangedPayload);
                }
            } else {
                var decoratedPayload = decoratePayloadWithDeltas(
                    payload,
                    previousCachedGroup
                        ? previousCachedGroup.payload
                        : null
                );
                state.groupCache.set(responseGroupKey, {
                    payload: decoratedPayload,
                    fingerprint: fingerprint,
                    fetchedAt: Date.now()
                });
                state.sourceFingerprint = fingerprint;
                renderPayload(decoratedPayload);
            }
            state.refreshRemaining = state.refreshSeconds;
        } catch (error) {
            if (error && error.name === "AbortError") return;
            if (state.payload && elements.status) {
                elements.status.textContent = "Показан последний снимок";
            } else {
                showMessage(
                    "Нет связи с сервером",
                    "Последний успешный рейтинг пока не получен."
                );
            }
        } finally {
            if (generation === state.requestGeneration) {
                state.requestInFlight = false;
                state.requestController = null;
            }
        }
    }

    function groupPlaylist() {
        return state.presentationPlaylist.slice();
    }

    function moveGroup(direction) {
        var playlist = groupPlaylist();
        if (!playlist.length) return;
        var index = playlist.findIndex(function (item) {
            return (
                item.periodId === state.selectedPeriod
                &&
                item.compositionId === state.selectedComposition
                && item.shiftType === state.shiftType
            );
        });
        if (index < 0) {
            index = direction >= 0 ? -1 : 0;
        }
        index = (index + direction + playlist.length) % playlist.length;
        state.selectedPeriod = playlist[index].periodId;
        state.selectedComposition = playlist[index].compositionId;
        state.shiftType = playlist[index].shiftType;
        state.rotationRemaining = state.rotationSeconds;
        if (elements.period && state.selectedPeriod) {
            elements.period.value = state.selectedPeriod;
        }
        if (elements.composition && state.selectedComposition) {
            elements.composition.value = state.selectedComposition;
        }
        if (elements.shiftType) {
            elements.shiftType.value = state.shiftType;
        }
        loadRating({replaceRequest: true});
    }

    function updateCountdowns() {
        if (elements.refreshCountdown) {
            elements.refreshCountdown.textContent = formatSeconds(
                state.refreshRemaining
            );
        }
        if (elements.rotationCountdown) {
            if (state.presentationPlaylist.length <= 1) {
                elements.rotationCountdown.textContent = "—";
            } else {
                elements.rotationCountdown.textContent = state.rotationPlaying
                    ? formatSeconds(state.rotationRemaining)
                    : "Пауза";
            }
        }
    }

    function tick() {
        state.refreshRemaining -= 1;
        if (state.refreshRemaining <= 0) {
            state.refreshRemaining = state.refreshSeconds;
            loadRating();
        }
        if (
            state.rotationPlaying
            && state.presentationPlaylist.length > 1
        ) {
            state.rotationRemaining -= 1;
            if (state.rotationRemaining <= 0) {
                moveGroup(1);
            }
        }
        updateCountdowns();
    }

    function toggleRotation() {
        if (state.presentationPlaylist.length <= 1) return;
        state.rotationPlaying = !state.rotationPlaying;
        state.rotationRemaining = state.rotationSeconds;
        var label = elements.rotationToggle
            ? elements.rotationToggle.querySelector("b")
            : null;
        var icon = elements.rotationToggle
            ? elements.rotationToggle.querySelector("span")
            : null;
        if (label) {
            label.textContent = state.rotationPlaying
                ? "Пауза"
                : "Продолжить";
        }
        if (icon) {
            icon.textContent = state.rotationPlaying ? "Ⅱ" : "▶";
        }
        updateCountdowns();
    }

    function toggleFullscreen() {
        var fullscreenPromise = null;
        if (!document.fullscreenElement) {
            if (document.documentElement.requestFullscreen) {
                fullscreenPromise = (
                    document.documentElement.requestFullscreen()
                );
            }
        } else if (document.exitFullscreen) {
            fullscreenPromise = document.exitFullscreen();
        }
        if (
            fullscreenPromise
            && typeof fullscreenPromise.catch === "function"
        ) {
            fullscreenPromise.catch(function () {
                if (elements.status) {
                    elements.status.textContent = (
                        "Полноэкранный режим недоступен"
                    );
                }
            });
        }
    }

    function bindEvents() {
        if (elements.period) {
            elements.period.addEventListener("change", function () {
                state.selectedPeriod = elements.period.value;
                state.selectedComposition = "";
                state.presentationPlaylist = [];
                state.refreshRemaining = state.refreshSeconds;
                updateProgramUi();
                loadRating({replaceRequest: true});
            });
        }
        if (elements.composition) {
            elements.composition.addEventListener("change", function () {
                state.selectedComposition = elements.composition.value;
                state.rotationRemaining = state.rotationSeconds;
                updateProgramUi();
                loadRating({replaceRequest: true});
            });
        }
        if (elements.shiftType) {
            elements.shiftType.addEventListener("change", function () {
                state.shiftType = elements.shiftType.value;
                state.rotationRemaining = state.rotationSeconds;
                updateProgramUi();
                loadRating({replaceRequest: true});
            });
        }
        if (elements.rotationToggle) {
            elements.rotationToggle.addEventListener(
                "click",
                toggleRotation
            );
        }
        if (elements.previous) {
            elements.previous.addEventListener("click", function () {
                moveGroup(-1);
            });
        }
        if (elements.next) {
            elements.next.addEventListener("click", function () {
                moveGroup(1);
            });
        }
        if (elements.programToggle) {
            elements.programToggle.addEventListener(
                "click",
                toggleCurrentProgramGroup
            );
        }
        if (elements.fullscreen) {
            elements.fullscreen.addEventListener(
                "click",
                toggleFullscreen
            );
        }
        window.addEventListener("resize", layoutGrid);
    }

    bindEvents();
    if (elements.shiftType) {
        elements.shiftType.value = state.shiftType;
    }
    updateCountdowns();
    loadRating();
    state.timerId = window.setInterval(tick, 1000);

    window.RatingTvScreen = {
        formatSeconds: formatSeconds,
        layoutGrid: layoutGrid,
        loadRating: loadRating,
        moveGroup: moveGroup,
        renderPayload: renderPayload,
        state: state
    };
})(window, document);
