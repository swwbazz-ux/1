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
        qaDayCount: root.querySelector("[data-qa-day-count]"),
        qaDaySelect: root.querySelector("[data-qa-day-select]"),
        qaReplayStatus: root.querySelector("[data-qa-replay-status]"),
        qaBackward: root.querySelector("[data-qa-backward]"),
        qaPause: root.querySelector("[data-qa-pause]"),
        qaStep: root.querySelector("[data-qa-step]"),
        qaForward: root.querySelector("[data-qa-forward]"),
        qaSpeed: root.querySelector("[data-qa-speed]")
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
        qaReplayEnabled: config.qaReplayEnabled === true,
        qaReplayKind: ["visual", "formula"].includes(config.qaReplayKind)
            ? config.qaReplayKind
            : "",
        qaFormulaEnabledShiftTypes: Array.isArray(
            config.qaFormulaEnabledShiftTypes
        )
            ? config.qaFormulaEnabledShiftTypes.filter(function (value) {
                return value === "day" || value === "night";
            })
            : [],
        qaReplay: null,
        qaReplayPhase: config.qaPreview === true ? "BOOTING" : "DISABLED",
        qaReplayDay: 1,
        qaReplayDayCount: Number(config.qaDayCount) || 30,
        qaReplayDirection: 0,
        qaReplayLastDirection: 1,
        qaReplaySpeed: 1,
        qaReplayBaseStepMs: 3000,
        qaPlaybackGeneration: 0,
        qaReplayTimerId: null,
        qaRequestGeneration: 0,
        qaRequestController: null,
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

    function updateQaShiftControl() {
        if (!elements.shiftType || !state.qaPreview) return;
        if (state.qaReplayKind !== "formula") {
            elements.shiftType.disabled = true;
            return;
        }
        Array.from(elements.shiftType.children).forEach(
            function (option) {
                option.disabled = (
                    !state.qaFormulaEnabledShiftTypes.includes(
                        option.value
                    )
                );
            }
        );
        elements.shiftType.disabled = (
            state.qaFormulaEnabledShiftTypes.length < 2
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
        if (state.qaPreview) {
            if (elements.period) elements.period.disabled = true;
            if (elements.composition) elements.composition.disabled = true;
            updateQaShiftControl();
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
        var normalizedEmployeeId = Number(employeeId);
        if (
            !config.photoUrlTemplate
            || !Number.isSafeInteger(normalizedEmployeeId)
            || normalizedEmployeeId <= 0
        ) {
            return "";
        }
        return String(config.photoUrlTemplate).replace(
            "__employee_id__",
            encodeURIComponent(String(normalizedEmployeeId))
        );
    }

    function movementFor(entry) {
        if (
            entry.row_status === "withheld"
            || entry.row_status === "not_observed"
        ) {
            return null;
        }
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
        movement.setAttribute(
            "aria-hidden",
            delta == null ? "true" : "false"
        );
        if (delta == null) {
            movement.classList.add("is-unranked");
            movement.textContent = "";
        } else if (delta > 0) {
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
        var rowStatus = entry.row_status || "";
        var isWithheld = rowStatus === "withheld";
        var isNotObserved = rowStatus === "not_observed";
        var isUnranked = isWithheld || isNotObserved;
        row.className = "rating-tv__row";
        row.dataset.employeeId = String(entry.employee_id || "");
        row.dataset.place = String(entry.place == null ? "" : entry.place);
        row.dataset.rowStatus = rowStatus;
        row.dataset.displayOrder = String(
            entry.display_order == null ? "" : entry.display_order
        );
        if (isWithheld) {
            row.classList.add("is-withheld");
        } else if (isNotObserved) {
            row.classList.add("is-not-observed");
        }
        if (!isUnranked && place >= 1 && place <= 5) {
            row.classList.add("is-premium", "is-place-" + place);
        }

        var placeNode = row._ratingPlaceNode;
        if (!placeNode) {
            placeNode = document.createElement("span");
            placeNode.className = "rating-tv__place";
            row._ratingPlaceNode = placeNode;
        }
        placeNode.replaceChildren();
        if (!isUnranked) {
            var gem = document.createElement("b");
            gem.textContent = "◆";
            gem.setAttribute("aria-hidden", "true");
            placeNode.appendChild(gem);
        }
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
        var scoreLabel = document.createElement("small");
        if (isWithheld) {
            score.classList.add("is-status", "is-withheld");
            scoreValue.textContent = "Удержан";
            scoreLabel.textContent = "проверка данных";
        } else if (isNotObserved) {
            score.classList.add("is-status", "is-not-observed");
            scoreValue.textContent = "Нет смен";
            scoreLabel.textContent = "за период";
        } else {
            scoreValue.textContent = (
                entry.score == null || entry.score === ""
                    ? "—"
                    : String(entry.score)
            );
            scoreLabel.textContent = "балл";
        }
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
        if (!state.qaPreview) {
            ensureInitialProgramGroup();
        }

        if (elements.status) {
            if (state.qaPreview) {
                elements.status.textContent = payload.formula_evaluated
                    ? "Виртуальный расчёт — неофициально"
                    : "Визуальный replay — не KPI";
            } else {
                elements.status.textContent = payload.official
                    ? "Подтверждённый результат"
                    : "Предварительный результат";
            }
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
            var employeeId = String(row.dataset.employeeId);
            setMovementContent(
                movement,
                deltas.has(employeeId)
                    ? deltas.get(employeeId)
                    : 0
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

    function replayError(message) {
        state.qaReplay = null;
        state.qaReplayPhase = "ERROR";
        state.qaReplayDirection = 0;
        if (elements.qaReplayStatus) {
            elements.qaReplayStatus.textContent = (
                message
                || "Сохранённое воспроизведение не прошло проверку."
            );
        }
        updateQaReplayControls();
        showMessage(
            "Тестовое воспроизведение недоступно",
            "Сохранённые снимки не прошли проверку целостности."
        );
    }

    function sameJson(left, right) {
        return JSON.stringify(left) === JSON.stringify(right);
    }

    function validateVisualQaReplay(document) {
        if (!document || typeof document !== "object") return false;
        if (document.schema !== config.qaReplaySchema) return false;
        if (
            document.schema_version !== 1
            ||
            document.synthetic !== true
            || document.official !== false
            || document.official_rating_eligible !== false
        ) {
            return false;
        }
        var replay = document.replay;
        var scope = document.scope;
        var snapshots = document.snapshots;
        if (
            !replay
            || replay.rating_mode !== "qa_saved_replay"
            || replay.synthetic !== true
            || replay.official !== false
            || replay.day_count !== 30
            || replay.expected_employee_count !== 53
            || replay.initial_day !== 1
            || !scope
            || !scope.rating_period
            || !scope.watch_composition
            || !["day", "night"].includes(scope.shift_type)
            || !Array.isArray(snapshots)
            || snapshots.length !== 30
        ) {
            return false;
        }

        var baselineIds = null;
        var previousPlaces = null;
        for (var index = 0; index < snapshots.length; index += 1) {
            var snapshot = snapshots[index];
            var day = index + 1;
            var payload = snapshot && snapshot.payload;
            if (
                !snapshot
                || snapshot.day !== day
                || !payload
                || payload.available !== true
                || payload.rating_mode !== "qa_saved_replay"
                || payload.synthetic !== true
                || payload.official !== false
                || payload.official_rating_eligible !== false
                || payload.scope_type !== "rating_period"
                || payload.qa_day !== day
                || payload.qa_day_count !== 30
                || payload.replay_run_id !== replay.id
                || payload.shift_type !== scope.shift_type
                || !sameJson(payload.rating_period, scope.rating_period)
                || !sameJson(
                    payload.watch_composition,
                    scope.watch_composition
                )
                || !Array.isArray(payload.entries)
                || payload.entries.length !== 53
            ) {
                return false;
            }
            var ids = [];
            var places = new Map();
            var seenIds = new Set();
            var seenOrders = new Set();
            var rankedEntries = [];
            for (
                var entryIndex = 0;
                entryIndex < payload.entries.length;
                entryIndex += 1
            ) {
                var entry = payload.entries[entryIndex];
                if (
                    !entry
                    || !Number.isInteger(entry.employee_id)
                    || entry.employee_id >= 0
                    || seenIds.has(entry.employee_id)
                    || !Number.isInteger(entry.place)
                    || entry.place < 1
                    || !Number.isInteger(entry.shared_score_place)
                    || entry.shared_score_place < 1
                    || !Number.isInteger(entry.display_order)
                    || entry.display_order < 1
                    || seenOrders.has(entry.display_order)
                    || !Number.isInteger(entry.position_delta)
                    || typeof entry.score !== "string"
                    || !/^(?:0|[1-9][0-9]{0,2})\.[0-9]{2}$/.test(
                        entry.score
                    )
                    || !Number.isFinite(Number(entry.score))
                    || Number(entry.score) < 0
                    || Number(entry.score) > 100
                    || typeof entry.level !== "string"
                ) {
                    return false;
                }
                seenIds.add(entry.employee_id);
                seenOrders.add(entry.display_order);
                ids.push(entry.employee_id);
                places.set(entry.employee_id, entry.place);
                rankedEntries.push(entry);
                var expectedDelta = previousPlaces === null
                    ? 0
                    : previousPlaces.get(entry.employee_id) - entry.place;
                if (entry.position_delta !== expectedDelta) {
                    return false;
                }
            }
            ids.sort(function (left, right) {
                return left - right;
            });
            if (baselineIds === null) {
                baselineIds = ids;
            } else if (!sameJson(ids, baselineIds)) {
                return false;
            }
            rankedEntries.sort(function (left, right) {
                return left.display_order - right.display_order;
            });
            var ratingLevels = {
                1: "Алмазный уровень",
                2: "Платиновый уровень",
                3: "Золотой уровень",
                4: "Серебряный уровень",
                5: "Медный уровень"
            };
            var previousScore = null;
            var densePlace = 0;
            for (
                var rankIndex = 0;
                rankIndex < rankedEntries.length;
                rankIndex += 1
            ) {
                var rankedEntry = rankedEntries[rankIndex];
                var numericScore = Number(rankedEntry.score);
                if (
                    rankedEntry.display_order !== rankIndex + 1
                    || (
                        previousScore !== null
                        && numericScore > previousScore
                    )
                ) {
                    return false;
                }
                if (
                    previousScore === null
                    || numericScore !== previousScore
                ) {
                    densePlace += 1;
                }
                if (
                    rankedEntry.place !== densePlace
                    || rankedEntry.shared_score_place !== densePlace
                    || rankedEntry.level !== (ratingLevels[densePlace] || "")
                ) {
                    return false;
                }
                previousScore = numericScore;
            }
            previousPlaces = places;
        }
        return true;
    }

    function isNonnegativeInteger(value) {
        return (
            Number.isInteger(value)
            && value >= 0
        );
    }

    function isFormulaScore(value) {
        return (
            typeof value === "string"
            && /^(?:0|[1-9][0-9]{0,2})\.[0-9]{4}$/.test(value)
            && Number.isFinite(Number(value))
            && Number(value) >= 0
            && Number(value) <= 100
        );
    }

    function isFormulaQuantity(value) {
        return (
            typeof value === "string"
            && /^(?:0|[1-9][0-9]*)\.[0-9]{2}$/.test(value)
        );
    }

    function validateFormulaQaReplay(replayDocument) {
        if (!replayDocument || typeof replayDocument !== "object") {
            return false;
        }
        if (replayDocument.schema !== config.qaReplaySchema) return false;
        if (
            replayDocument.schema_version !== 1
            || replayDocument.synthetic !== true
            || replayDocument.formula_evaluated !== true
            || replayDocument.official !== false
            || replayDocument.official_rating_eligible !== false
        ) {
            return false;
        }
        var replay = replayDocument.replay;
        var scope = replayDocument.scope;
        var snapshots = replayDocument.snapshots;
        if (
            !replay
            || replay.rating_mode !== "qa_formula_replay"
            || replay.synthetic !== true
            || replay.formula_evaluated !== true
            || replay.official !== false
            || replay.day_count !== 30
            || replay.expected_employee_count !== 53
            || replay.initial_day !== 1
            || !scope
            || scope.shift_type !== state.shiftType
            || !["day", "night"].includes(scope.shift_type)
            || !scope.rating_period
            || !scope.watch_composition
            || !Array.isArray(scope.cohort)
            || scope.cohort.length !== 53
            || !Array.isArray(snapshots)
            || snapshots.length !== 30
        ) {
            return false;
        }

        var cohortNames = new Map();
        for (
            var cohortIndex = 0;
            cohortIndex < scope.cohort.length;
            cohortIndex += 1
        ) {
            var cohortEntry = scope.cohort[cohortIndex];
            if (
                !cohortEntry
                || !Number.isInteger(cohortEntry.employee_id)
                || cohortEntry.employee_id >= 0
                || cohortNames.has(cohortEntry.employee_id)
                || typeof cohortEntry.full_name !== "string"
                || !cohortEntry.full_name.trim()
            ) {
                return false;
            }
            cohortNames.set(
                cohortEntry.employee_id,
                cohortEntry.full_name
            );
        }

        var blockNames = [
            "production",
            "work_time",
            "stability",
            "assignments",
            "digital_accounting"
        ];
        var levelNames = {
            1: "Алмазный уровень",
            2: "Платиновый уровень",
            3: "Золотой уровень",
            4: "Серебряный уровень",
            5: "Медный уровень"
        };
        var previousStatuses = new Map();
        var previousPlaces = new Map();

        for (
            var snapshotIndex = 0;
            snapshotIndex < snapshots.length;
            snapshotIndex += 1
        ) {
            var snapshot = snapshots[snapshotIndex];
            var day = snapshotIndex + 1;
            var payload = snapshot && snapshot.payload;
            if (
                !snapshot
                || snapshot.day !== day
                || !payload
                || payload.available !== true
                || typeof payload.calculation_available !== "boolean"
                || payload.rating_mode !== "qa_formula_replay"
                || payload.synthetic !== true
                || payload.formula_evaluated !== true
                || payload.official !== false
                || payload.official_rating_eligible !== false
                || payload.scope_type !== "rating_period"
                || payload.qa_day !== day
                || payload.qa_day_count !== 30
                || payload.replay_run_id !== replay.id
                || payload.shift_type !== scope.shift_type
                || !sameJson(payload.rating_period, scope.rating_period)
                || !sameJson(
                    payload.watch_composition,
                    scope.watch_composition
                )
                || !Array.isArray(payload.entries)
                || payload.entries.length !== 53
            ) {
                return false;
            }

            var seenIds = new Set();
            var seenOrders = new Set();
            var orderedEntries = payload.entries.slice().sort(
                function (left, right) {
                    return left.display_order - right.display_order;
                }
            );
            var currentStatuses = new Map();
            var currentPlaces = new Map();
            var previousScore = null;
            var densePlace = 0;
            var foundUnrated = false;

            for (
                var entryIndex = 0;
                entryIndex < orderedEntries.length;
                entryIndex += 1
            ) {
                var entry = orderedEntries[entryIndex];
                if (
                    !entry
                    || !Number.isInteger(entry.employee_id)
                    || entry.employee_id >= 0
                    || seenIds.has(entry.employee_id)
                    || cohortNames.get(entry.employee_id)
                        !== entry.full_name
                    || !Number.isInteger(entry.display_order)
                    || entry.display_order !== entryIndex + 1
                    || seenOrders.has(entry.display_order)
                    || !["rated", "withheld", "not_observed"].includes(
                        entry.row_status
                    )
                    || !isNonnegativeInteger(entry.shift_count)
                    || !isNonnegativeInteger(entry.withheld_shift_count)
                    || typeof entry.level !== "string"
                ) {
                    return false;
                }
                seenIds.add(entry.employee_id);
                seenOrders.add(entry.display_order);
                currentStatuses.set(
                    entry.employee_id,
                    entry.row_status
                );

                if (entry.row_status !== "rated") {
                    foundUnrated = true;
                    if (
                        entry.ranking_eligible !== false
                        || entry.score !== null
                        || entry.blocks !== null
                        || entry.confidence !== null
                        || entry.trip_count !== null
                        || entry.volume_m3 !== null
                        || entry.tonnage_t !== null
                        || entry.place !== null
                        || entry.shared_score_place !== null
                        || entry.position_delta !== null
                        || entry.level !== ""
                    ) {
                        return false;
                    }
                    if (
                        entry.row_status === "withheld"
                        && (
                            entry.shift_count < 1
                            || entry.withheld_shift_count < 1
                        )
                    ) {
                        return false;
                    }
                    if (
                        entry.row_status === "not_observed"
                        && (
                            entry.shift_count !== 0
                            || entry.withheld_shift_count !== 0
                        )
                    ) {
                        return false;
                    }
                    continue;
                }

                if (
                    foundUnrated
                    || entry.ranking_eligible !== true
                    || entry.shift_count < 1
                    || entry.withheld_shift_count !== 0
                    || !isNonnegativeInteger(entry.trip_count)
                    || !isFormulaQuantity(entry.volume_m3)
                    || !isFormulaQuantity(entry.tonnage_t)
                    || !isFormulaScore(entry.score)
                    || !isFormulaScore(entry.confidence)
                    || !entry.blocks
                    || typeof entry.blocks !== "object"
                    || Object.keys(entry.blocks).length !== blockNames.length
                    || !blockNames.every(function (name) {
                        return (
                            Object.prototype.hasOwnProperty.call(
                                entry.blocks,
                                name
                            )
                            && isFormulaScore(entry.blocks[name])
                        );
                    })
                    || !Number.isInteger(entry.place)
                    || entry.place < 1
                    || entry.shared_score_place !== entry.place
                ) {
                    return false;
                }
                if (
                    previousScore !== null
                    && Number(entry.score) > Number(previousScore)
                ) {
                    return false;
                }
                if (
                    previousScore === null
                    || entry.score !== previousScore
                ) {
                    densePlace += 1;
                }
                if (
                    entry.place !== densePlace
                    || entry.level !== (levelNames[densePlace] || "")
                ) {
                    return false;
                }
                var expectedDelta = (
                    previousStatuses.get(entry.employee_id) === "rated"
                        ? (
                            previousPlaces.get(entry.employee_id)
                            - entry.place
                        )
                        : null
                );
                if (entry.position_delta !== expectedDelta) {
                    return false;
                }
                currentPlaces.set(entry.employee_id, entry.place);
                previousScore = entry.score;
            }

            if (
                seenIds.size !== cohortNames.size
                || Array.from(cohortNames.keys()).some(function (employeeId) {
                    return !seenIds.has(employeeId);
                })
            ) {
                return false;
            }
            previousStatuses = currentStatuses;
            previousPlaces = currentPlaces;
        }
        return true;
    }

    function validatePinnedQaReplay(replayDocument) {
        if (state.qaReplayKind === "visual") {
            return validateVisualQaReplay(replayDocument);
        }
        if (state.qaReplayKind === "formula") {
            return validateFormulaQaReplay(replayDocument);
        }
        return false;
    }

    function populateQaDaySelect() {
        if (!elements.qaDaySelect) return;
        var fragment = document.createDocumentFragment();
        for (var day = 1; day <= state.qaReplayDayCount; day += 1) {
            var option = document.createElement("option");
            option.value = String(day);
            option.textContent = String(day);
            option.selected = day === state.qaReplayDay;
            fragment.appendChild(option);
        }
        elements.qaDaySelect.replaceChildren(fragment);
        elements.qaDaySelect.value = String(state.qaReplayDay);
    }

    function qaReplayIsPlaying() {
        return (
            state.qaReplayPhase === "PLAYING_FORWARD"
            || state.qaReplayPhase === "PLAYING_BACKWARD"
        );
    }

    function qaReplayIsReady() {
        return Boolean(
            state.qaReplay
            && ["PAUSED", "PLAYING_FORWARD", "PLAYING_BACKWARD"].includes(
                state.qaReplayPhase
            )
        );
    }

    function qaReplayStatusText(message) {
        if (!elements.qaReplayStatus) return;
        var prefix = state.qaReplay
            && state.qaReplay.replay.formula_evaluated
            ? "Синтетический расчёт"
            : "Визуальная синтетика — KPI не рассчитывался";
        elements.qaReplayStatus.textContent = message
            ? prefix + ". " + message
            : prefix;
    }

    function updateQaReplayControls() {
        if (!state.qaPreview) return;
        var ready = qaReplayIsReady();
        var playing = qaReplayIsPlaying();
        var atStart = state.qaReplayDay <= 1;
        var atEnd = state.qaReplayDay >= state.qaReplayDayCount;
        if (elements.qaDaySelect) {
            elements.qaDaySelect.disabled = !ready;
            elements.qaDaySelect.value = String(state.qaReplayDay);
        }
        if (elements.qaBackward) {
            elements.qaBackward.disabled = !ready || atStart;
        }
        if (elements.qaForward) {
            elements.qaForward.disabled = !ready || atEnd;
        }
        if (elements.qaStep) {
            elements.qaStep.disabled = !ready || atEnd;
        }
        if (elements.qaSpeed) {
            elements.qaSpeed.disabled = !ready;
            elements.qaSpeed.value = String(state.qaReplaySpeed);
        }
        if (elements.qaPause) {
            elements.qaPause.disabled = !ready;
            var icon = elements.qaPause.querySelector("span");
            var label = elements.qaPause.querySelector("b");
            if (icon) icon.textContent = playing ? "Ⅱ" : "▶";
            if (label) label.textContent = playing ? "Пауза" : "Продолжить";
        }
    }

    function cancelQaReplayTimer() {
        state.qaPlaybackGeneration += 1;
        if (state.qaReplayTimerId != null) {
            window.clearTimeout(state.qaReplayTimerId);
            state.qaReplayTimerId = null;
        }
    }

    function renderQaReplayDay(day) {
        if (!state.qaReplay) return false;
        var normalizedDay = Math.max(
            1,
            Math.min(state.qaReplayDayCount, Number(day) || 1)
        );
        var snapshot = state.qaReplay.snapshots[normalizedDay - 1];
        if (!snapshot || snapshot.day !== normalizedDay) {
            replayError("В сохранённом прогоне отсутствует выбранный день.");
            return false;
        }
        state.qaReplayDay = normalizedDay;
        renderPayload(snapshot.payload);
        if (elements.qaDay) {
            elements.qaDay.textContent = String(normalizedDay);
        }
        if (elements.qaDayCount) {
            elements.qaDayCount.textContent = String(
                state.qaReplayDayCount
            );
        }
        if (elements.qaDaySelect) {
            elements.qaDaySelect.value = String(normalizedDay);
        }
        updateQaReplayControls();
        return true;
    }

    function pauseQaReplay(message) {
        if (!qaReplayIsReady()) return;
        cancelQaReplayTimer();
        state.qaReplayPhase = "PAUSED";
        state.qaReplayDirection = 0;
        qaReplayStatusText(
            message || "Пауза на дне " + state.qaReplayDay + "."
        );
        updateQaReplayControls();
    }

    function scheduleQaReplayStep(generation) {
        if (!qaReplayIsPlaying()) return;
        var delay = Math.max(
            100,
            Math.round(
                state.qaReplayBaseStepMs / state.qaReplaySpeed
            )
        );
        state.qaReplayTimerId = window.setTimeout(function () {
            if (
                generation !== state.qaPlaybackGeneration
                || !qaReplayIsPlaying()
            ) {
                return;
            }
            state.qaReplayTimerId = null;
            var nextDay = (
                state.qaReplayDay + state.qaReplayDirection
            );
            if (
                nextDay < 1
                || nextDay > state.qaReplayDayCount
            ) {
                pauseQaReplay(
                    state.qaReplayDirection > 0
                        ? "Прогон завершён на дне 30."
                        : "Достигнут день 1."
                );
                return;
            }
            if (!renderQaReplayDay(nextDay)) return;
            if (
                nextDay === 1
                || nextDay === state.qaReplayDayCount
            ) {
                pauseQaReplay(
                    nextDay === state.qaReplayDayCount
                        ? "Прогон завершён на дне 30."
                        : "Достигнут день 1."
                );
                return;
            }
            scheduleQaReplayStep(generation);
        }, delay);
    }

    function startQaReplay(direction) {
        if (!qaReplayIsReady()) return;
        var normalizedDirection = direction < 0 ? -1 : 1;
        if (
            (normalizedDirection < 0 && state.qaReplayDay <= 1)
            || (
                normalizedDirection > 0
                && state.qaReplayDay >= state.qaReplayDayCount
            )
        ) {
            pauseQaReplay(
                normalizedDirection > 0
                    ? "Прогон уже находится на дне 30."
                    : "Прогон уже находится на дне 1."
            );
            return;
        }
        cancelQaReplayTimer();
        state.qaReplayDirection = normalizedDirection;
        state.qaReplayLastDirection = normalizedDirection;
        state.qaReplayPhase = normalizedDirection > 0
            ? "PLAYING_FORWARD"
            : "PLAYING_BACKWARD";
        qaReplayStatusText(
            normalizedDirection > 0
                ? "Воспроизведение вперёд."
                : "Воспроизведение назад."
        );
        updateQaReplayControls();
        scheduleQaReplayStep(state.qaPlaybackGeneration);
    }

    function toggleQaReplayPause() {
        if (!qaReplayIsReady()) return;
        if (qaReplayIsPlaying()) {
            pauseQaReplay();
            return;
        }
        var direction = state.qaReplayLastDirection || 1;
        if (
            (direction > 0 && state.qaReplayDay >= state.qaReplayDayCount)
            || (direction < 0 && state.qaReplayDay <= 1)
        ) {
            direction *= -1;
        }
        startQaReplay(direction);
    }

    function stepQaReplay(direction) {
        if (!qaReplayIsReady()) return;
        pauseQaReplay();
        var targetDay = state.qaReplayDay + (direction < 0 ? -1 : 1);
        if (
            targetDay < 1
            || targetDay > state.qaReplayDayCount
        ) {
            updateQaReplayControls();
            return;
        }
        renderQaReplayDay(targetDay);
        qaReplayStatusText("Пауза на дне " + targetDay + ".");
    }

    function showQaReplayFallback() {
        state.qaReplayPhase = "ERROR";
        if (previewPayload) {
            renderPayload(previewPayload);
            if (elements.qaReplayStatus) {
                elements.qaReplayStatus.textContent = (
                    "Статический визуальный макет. "
                    + "Сохранённое воспроизведение выключено."
                );
            }
        } else {
            replayError(
                "Сохранённое воспроизведение выключено."
            );
        }
        updateQaReplayControls();
    }

    function qaReplayRequestUrl() {
        var url = new URL(
            config.qaReplayUrl,
            window.location.origin
        );
        if (state.qaReplayKind === "formula") {
            url.searchParams.set("shift_type", state.shiftType);
        }
        return url.toString();
    }

    async function loadQaReplay(options) {
        options = options || {};
        if (
            !state.qaReplayEnabled
            || !config.qaReplayUrl
            || !["visual", "formula"].includes(state.qaReplayKind)
        ) {
            showQaReplayFallback();
            return;
        }
        var preservedDay = options.preserveDay
            ? state.qaReplayDay
            : null;
        if (state.qaRequestController) {
            state.qaRequestController.abort();
        }
        cancelQaReplayTimer();
        state.qaReplayPhase = "BOOTING";
        state.qaReplayDirection = 0;
        if (
            options.preserveDay
            && state.qaReplayKind === "formula"
        ) {
            showMessage(
                "Загружаем выбранную смену",
                "Проверяем сохранённый формульный снимок."
            );
        }
        updateQaReplayControls();
        state.qaRequestGeneration += 1;
        var requestGeneration = state.qaRequestGeneration;
        var requestedShiftType = state.shiftType;
        var controller = new AbortController();
        state.qaRequestController = controller;
        try {
            var response = await window.fetch(
                qaReplayRequestUrl(),
                {
                    method: "GET",
                    credentials: "same-origin",
                    cache: "no-store",
                    headers: {"Accept": "application/json"},
                    signal: controller.signal
                }
            );
            if (
                requestGeneration !== state.qaRequestGeneration
                || controller.signal.aborted
            ) {
                return;
            }
            var replayDocument = await response.json();
            if (
                requestGeneration !== state.qaRequestGeneration
                || controller.signal.aborted
            ) {
                return;
            }
            if (!response.ok) {
                replayError(
                    replayDocument.error
                    || "Сервер не отдал сохранённое воспроизведение."
                );
                return;
            }
            if (
                state.qaReplayKind === "formula"
                && (
                    state.shiftType !== requestedShiftType
                    || !replayDocument.scope
                    || replayDocument.scope.shift_type
                        !== requestedShiftType
                )
            ) {
                replayError(
                    "Формульное воспроизведение не соответствует выбранной смене."
                );
                return;
            }
            if (!validatePinnedQaReplay(replayDocument)) {
                replayError(
                    "Сохранённое воспроизведение имеет неверную схему."
                );
                return;
            }
            state.qaReplay = replayDocument;
            state.qaReplayDayCount = replayDocument.replay.day_count;
            state.qaReplayDay = preservedDay == null
                ? replayDocument.replay.initial_day
                : Math.max(
                    1,
                    Math.min(
                        state.qaReplayDayCount,
                        Number(preservedDay) || 1
                    )
                );
            state.qaReplayBaseStepMs = replayDocument.replay.base_step_ms;
            state.qaReplayPhase = "PAUSED";
            state.qaReplayDirection = 0;
            state.qaReplayLastDirection = 1;
            populateQaDaySelect();
            renderQaReplayDay(state.qaReplayDay);
            qaReplayStatusText(
                "Пауза на дне " + state.qaReplayDay + "."
            );
            updateQaReplayControls();
        } catch (_error) {
            if (
                requestGeneration !== state.qaRequestGeneration
                || controller.signal.aborted
            ) {
                return;
            }
            replayError(
                "Не удалось получить проверенные сохранённые снимки."
            );
        } finally {
            if (
                requestGeneration === state.qaRequestGeneration
                && state.qaRequestController === controller
            ) {
                state.qaRequestController = null;
            }
        }
    }

    async function loadRating(options) {
        options = options || {};
        if (state.qaPreview) {
            if (state.qaReplay) {
                renderQaReplayDay(state.qaReplayDay);
            }
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
            elements.refreshCountdown.textContent = state.qaPreview
                ? (
                    state.qaReplayKind === "formula"
                        ? "Формульный снимок"
                        : "Сохранённый снимок"
                )
                : formatSeconds(state.refreshRemaining);
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
                if (elements.fullscreen) {
                    elements.fullscreen.title = (
                        "Полноэкранный режим недоступен"
                    );
                }
            });
        }
    }

    function qaReplayHiddenByViewport() {
        return (
            Number(window.innerWidth) <= 1100
            || Number(window.innerHeight) <= 650
        );
    }

    function handleViewportChange() {
        layoutGrid();
        if (
            state.qaPreview
            && qaReplayIsPlaying()
            && qaReplayHiddenByViewport()
        ) {
            pauseQaReplay(
                "Воспроизведение остановлено на узком экране."
            );
        }
    }

    function interactiveKeyboardTarget(target) {
        var tagName = target && target.tagName
            ? String(target.tagName).toLowerCase()
            : "";
        return ["button", "select", "input", "textarea"].includes(
            tagName
        );
    }

    function handleQaReplayKeyboard(event) {
        if (
            !state.qaPreview
            || !qaReplayIsReady()
            || qaReplayHiddenByViewport()
            || interactiveKeyboardTarget(event.target)
            || event.ctrlKey
            || event.altKey
            || event.metaKey
        ) {
            return;
        }
        var handled = true;
        if (event.key === " " || event.key === "Spacebar") {
            toggleQaReplayPause();
        } else if (event.key === "ArrowLeft") {
            stepQaReplay(-1);
        } else if (event.key === "ArrowRight") {
            stepQaReplay(1);
        } else if (event.key === "Home") {
            pauseQaReplay();
            renderQaReplayDay(1);
            qaReplayStatusText("Пауза на дне 1.");
        } else if (event.key === "End") {
            pauseQaReplay();
            renderQaReplayDay(state.qaReplayDayCount);
            qaReplayStatusText(
                "Пауза на дне " + state.qaReplayDayCount + "."
            );
        } else if (
            event.key === "f"
            || event.key === "F"
        ) {
            toggleFullscreen();
        } else {
            handled = false;
        }
        if (handled && typeof event.preventDefault === "function") {
            event.preventDefault();
        }
    }

    function updateFullscreenControl() {
        if (!elements.fullscreen) return;
        var label = elements.fullscreen.querySelector("b");
        var icon = elements.fullscreen.querySelector("span");
        var active = Boolean(document.fullscreenElement);
        if (label) {
            label.textContent = active
                ? "Выйти из полного экрана"
                : "Полный экран";
        }
        if (icon) icon.textContent = active ? "⤢" : "⛶";
    }

    function bindEvents() {
        if (elements.period && !state.qaPreview) {
            elements.period.addEventListener("change", function () {
                state.selectedPeriod = elements.period.value;
                state.selectedComposition = "";
                state.presentationPlaylist = [];
                state.refreshRemaining = state.refreshSeconds;
                updateProgramUi();
                loadRating({replaceRequest: true});
            });
        }
        if (elements.composition && !state.qaPreview) {
            elements.composition.addEventListener("change", function () {
                state.selectedComposition = elements.composition.value;
                state.rotationRemaining = state.rotationSeconds;
                updateProgramUi();
                loadRating({replaceRequest: true});
            });
        }
        if (elements.shiftType) {
            elements.shiftType.addEventListener("change", function () {
                if (state.qaPreview) {
                    if (state.qaReplayKind !== "formula") return;
                    if (
                        !state.qaFormulaEnabledShiftTypes.includes(
                            elements.shiftType.value
                        )
                    ) {
                        elements.shiftType.value = state.shiftType;
                        return;
                    }
                    state.shiftType = elements.shiftType.value;
                    loadQaReplay({
                        preserveDay: true,
                        replaceRequest: true
                    });
                    return;
                }
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
        if (elements.qaBackward) {
            elements.qaBackward.addEventListener("click", function () {
                startQaReplay(-1);
            });
        }
        if (elements.qaPause) {
            elements.qaPause.addEventListener(
                "click",
                toggleQaReplayPause
            );
        }
        if (elements.qaStep) {
            elements.qaStep.addEventListener("click", function () {
                stepQaReplay(1);
            });
        }
        if (elements.qaForward) {
            elements.qaForward.addEventListener("click", function () {
                startQaReplay(1);
            });
        }
        if (elements.qaDaySelect) {
            elements.qaDaySelect.addEventListener("change", function () {
                if (!qaReplayIsReady()) return;
                var selectedDay = Number(elements.qaDaySelect.value);
                pauseQaReplay();
                renderQaReplayDay(selectedDay);
                qaReplayStatusText(
                    "Пауза на дне " + state.qaReplayDay + "."
                );
            });
        }
        if (elements.qaSpeed) {
            elements.qaSpeed.addEventListener("change", function () {
                var speed = Number(elements.qaSpeed.value);
                if (![0.5, 1, 2, 4].includes(speed)) {
                    elements.qaSpeed.value = String(
                        state.qaReplaySpeed
                    );
                    return;
                }
                state.qaReplaySpeed = speed;
                if (qaReplayIsPlaying()) {
                    startQaReplay(state.qaReplayDirection);
                } else {
                    updateQaReplayControls();
                }
            });
        }
        window.addEventListener("resize", handleViewportChange);
        if (document.addEventListener) {
            document.addEventListener(
                "keydown",
                handleQaReplayKeyboard
            );
            document.addEventListener("visibilitychange", function () {
                if (document.hidden && qaReplayIsPlaying()) {
                    pauseQaReplay(
                        "Воспроизведение остановлено в скрытой вкладке."
                    );
                }
            });
            document.addEventListener(
                "fullscreenchange",
                updateFullscreenControl
            );
        }
    }

    bindEvents();
    if (elements.shiftType) {
        elements.shiftType.value = state.shiftType;
    }
    updateQaShiftControl();
    updateCountdowns();
    if (state.qaPreview) {
        loadQaReplay();
    } else {
        loadRating();
        state.timerId = window.setInterval(tick, 1000);
    }

    window.RatingTvScreen = {
        formatSeconds: formatSeconds,
        layoutGrid: layoutGrid,
        loadRating: loadRating,
        loadQaReplay: loadQaReplay,
        moveGroup: moveGroup,
        pauseQaReplay: pauseQaReplay,
        renderQaReplayDay: renderQaReplayDay,
        renderPayload: renderPayload,
        startQaReplay: startQaReplay,
        stepQaReplay: stepQaReplay,
        state: state
    };
})(window, document);
