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
    var reserveThreeColumnLayout = /(?:^|[?&])layout=three(?:&|$)/.test(
        String(window.location && window.location.search || "")
    );
    root.dataset.ratingLayout = reserveThreeColumnLayout
        ? "three-reserve"
        : "four";

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
        qaSpeed: root.querySelector("[data-qa-speed]"),
        qaLiveStep: root.querySelector("[data-qa-live-step]"),
        qaLiveVirtualAt: root.querySelector("[data-qa-live-virtual-at]"),
        qaLiveShift: root.querySelector("[data-qa-live-shift]"),
        qaLiveRevision: root.querySelector("[data-qa-live-revision]"),
        qaLiveSourceFingerprint: root.querySelector(
            "[data-qa-live-source-fingerprint]"
        ),
        qaLiveScoreFingerprint: root.querySelector(
            "[data-qa-live-score-fingerprint]"
        ),
        qaLiveStateStatus: root.querySelector(
            "[data-qa-live-state-status]"
        )
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
        qaLive: config.qaLive === true,
        qaLiveState: null,
        qaLiveRequestGeneration: 0,
        qaLiveRequestController: null,
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
        if (state.qaPreview || state.qaLive) {
            if (elements.period) elements.period.disabled = true;
            if (elements.composition) elements.composition.disabled = true;
            if (elements.shiftType && state.qaLive) {
                elements.shiftType.disabled = true;
            } else {
                updateQaShiftControl();
            }
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
            "false"
        );
        if (delta == null) {
            movement.classList.add("is-unranked");
            movement.textContent = "—";
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
        avatar.className = "rating-tv__avatar is-placeholder";
        avatar.setAttribute("aria-hidden", "true");
        var url = photoUrl(entry.employee_id);
        if (!url) return avatar;

        var image = document.createElement("img");
        image.alt = "";
        image.loading = "eager";
        image.decoding = "async";
        image.addEventListener("load", function () {
            avatar.textContent = "";
            avatar.classList.remove("is-placeholder");
            avatar.appendChild(image);
        });
        image.addEventListener("error", function () {
            image.remove();
        });
        image.src = url;
        return avatar;
    }

    function ratingDisplayName(entry) {
        return String(
            entry.display_name
            || entry.full_name
            || "Сотрудник не указан"
        ).trim();
    }

    function ratingTruckLabel(entry) {
        var equipment = Array.isArray(entry.equipment)
            ? entry.equipment[0]
            : entry.equipment;
        var normalized = String(equipment || "").trim();
        if (!normalized) return "№ —";
        var truckMatch = normalized.match(/(?:№\s*|\s)(\d[\w-]*)\s*$/i);
        return truckMatch ? "№ " + truckMatch[1] : "№ —";
    }

    function createTruckIcon() {
        var namespace = "http://www.w3.org/2000/svg";
        var icon = document.createElementNS(namespace, "svg");
        icon.classList.add("rating-tv__truck-icon");
        icon.setAttribute("viewBox", "0 0 24 16");
        icon.setAttribute("aria-hidden", "true");
        icon.setAttribute("focusable", "false");

        var body = document.createElementNS(namespace, "path");
        body.setAttribute(
            "d",
            "M1.5 3.5h10.2l2.2 6H4.4L1.5 3.5Zm12.4 2h4l3.1 3V12h-7.1V5.5ZM3.7 12h17.6"
        );
        var wheels = document.createElementNS(namespace, "path");
        wheels.setAttribute(
            "d",
            "M7.3 12.2a2 2 0 1 1-4 0m16.7 0a2 2 0 1 1-4 0"
        );
        icon.append(body, wheels);
        return icon;
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
        placeNode.setAttribute(
            "aria-label",
            entry.place == null
                ? "Место не определено"
                : "Место " + String(entry.place)
        );
        placeNode.appendChild(
            document.createTextNode(
                entry.place == null ? "—" : String(entry.place)
            )
        );

        var identity = document.createElement("span");
        identity.className = "rating-tv__identity";

        var name = document.createElement("strong");
        name.className = "rating-tv__name";
        name.textContent = ratingDisplayName(entry);
        name.title = name.textContent;

        var equipment = document.createElement("span");
        equipment.className = "rating-tv__equipment";
        equipment.textContent = ratingTruckLabel(entry);
        equipment.title = equipment.textContent;
        equipment.setAttribute("aria-label", equipment.textContent);
        equipment.prepend(createTruckIcon());

        var score = null;
        if (isWithheld) {
            score = document.createElement("span");
            score.className = "rating-tv__score";
            score.classList.add("is-status", "is-withheld");
            var scoreValue = document.createElement("b");
            scoreValue.textContent = "Удержан";
            var scoreLabel = document.createElement("small");
            scoreLabel.textContent = "проверка данных";
            score.append(scoreValue, scoreLabel);
        } else if (isNotObserved) {
            score = document.createElement("span");
            score.className = "rating-tv__score";
            score.classList.add("is-status", "is-not-observed");
            var noShiftValue = document.createElement("b");
            noShiftValue.textContent = "Нет смен";
            var noShiftLabel = document.createElement("small");
            noShiftLabel.textContent = "за период";
            score.append(noShiftValue, noShiftLabel);
        }

        var movement = document.createElement("span");
        setMovementContent(movement, movementFor(entry));

        var service = document.createElement("span");
        service.className = "rating-tv__service";
        if (score) {
            row.classList.add("has-service-status");
            service.classList.add("has-status");
            service.appendChild(score);
        }
        service.appendChild(movement);

        identity.append(createAvatar(entry), name, equipment, service);
        row.replaceChildren(placeNode, identity);
        return row;
    }

    function createRatingRow(entry) {
        return updateRatingRow(document.createElement("li"), entry);
    }

    function clearGridPlacement() {
        Array.from(elements.grid.children).forEach(function (row) {
            row.style.setProperty("grid-column", "");
            row.style.setProperty("grid-row", "");
        });
    }

    function layoutThreeColumnReserve(count, boardHeight) {
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
        clearGridPlacement();
        elements.grid.style.setProperty("--rating-columns", String(columns));
        elements.grid.style.setProperty("--rating-rows", String(rows));
        elements.grid.classList.remove("is-four-column-layout");
        elements.grid.classList.add("is-three-column-reserve");
        elements.grid.classList.toggle(
            "is-dense",
            boardHeight / rows < 42
        );
    }

    function layoutFourColumns(count, boardHeight) {
        var columns = Math.min(4, count);
        var baseSize = Math.floor(count / columns);
        var remainder = count % columns;
        var columnSizes = Array.from(
            {length: columns},
            function (_unused, index) {
                return baseSize + (index < remainder ? 1 : 0);
            }
        );
        var rows = Math.max.apply(null, columnSizes);
        var entryIndex = 0;

        columnSizes.forEach(function (columnSize, columnIndex) {
            for (var rowIndex = 0; rowIndex < columnSize; rowIndex += 1) {
                var row = elements.grid.children[entryIndex];
                row.style.setProperty(
                    "grid-column",
                    String(columnIndex + 1)
                );
                row.style.setProperty("grid-row", String(rowIndex + 1));
                entryIndex += 1;
            }
        });

        elements.grid.style.setProperty("--rating-columns", String(columns));
        elements.grid.style.setProperty("--rating-rows", String(rows));
        elements.grid.classList.remove("is-three-column-reserve");
        elements.grid.classList.add("is-four-column-layout");
        elements.grid.classList.toggle(
            "is-dense",
            boardHeight / rows < 42
        );
    }

    function layoutGrid() {
        if (!elements.grid || elements.grid.hidden) return;
        var count = elements.grid.children.length;
        if (!count) return;
        var boardHeight = elements.grid.clientHeight
            || elements.grid.parentElement.clientHeight
            || window.innerHeight;

        if (window.innerWidth <= 600) {
            clearGridPlacement();
            elements.grid.style.setProperty("--rating-columns", "1");
            elements.grid.style.setProperty("--rating-rows", String(count));
            elements.grid.classList.remove(
                "is-four-column-layout",
                "is-three-column-reserve",
                "is-dense"
            );
            return;
        }

        if (reserveThreeColumnLayout) {
            layoutThreeColumnReserve(count, boardHeight);
            return;
        }
        layoutFourColumns(count, boardHeight);
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

    function shortFingerprint(value) {
        var normalized = String(value || "").trim();
        return normalized ? normalized.slice(0, 10) : "—";
    }

    function renderQaLiveStateMetadata(qaState) {
        if (!qaState) return;
        if (elements.qaLiveStep) {
            elements.qaLiveStep.textContent = String(qaState.step);
        }
        if (elements.qaLiveVirtualAt) {
            elements.qaLiveVirtualAt.textContent = formatDateTime(
                qaState.virtual_at
            );
        }
        if (elements.qaLiveShift) {
            elements.qaLiveShift.textContent = qaState.shift_type === "day"
                ? "Дневная"
                : "Ночная";
        }
        if (elements.qaLiveStateStatus) {
            elements.qaLiveStateStatus.textContent = (
                "Получен шаг "
                + qaState.step
                + " · ждём только закрытые смены"
            );
        }
    }

    function applyQaLivePlaceholders(payload) {
        if (
            !state.qaLive
            || !state.qaLiveState
            || !Array.isArray(state.qaLiveState.placeholders)
        ) {
            return payload;
        }
        var entries = Array.isArray(payload.entries)
            ? payload.entries.map(function (entry) {
                return Object.assign({}, entry);
            })
            : [];
        var existingIds = new Set(entries.map(function (entry) {
            return String(entry.employee_id);
        }));
        var nextDisplayOrder = entries.reduce(function (maximum, entry) {
            return Math.max(
                maximum,
                Number(entry.display_order || entry.place || 0)
            );
        }, 0);
        state.qaLiveState.placeholders.forEach(function (placeholder) {
            var employeeId = String(placeholder.employee_id);
            if (existingIds.has(employeeId)) return;
            nextDisplayOrder += 1;
            entries.push({
                employee_id: placeholder.employee_id,
                full_name: (
                    placeholder.full_name
                    || "Синтетический сотрудник №"
                        + placeholder.employee_id
                ),
                equipment: [],
                row_status: placeholder.status,
                ranking_eligible: false,
                shift_count: null,
                withheld_shift_count: (
                    placeholder.status === "withheld" ? 1 : 0
                ),
                withheld_reasons: {},
                quality_flags: Array.isArray(placeholder.reasons)
                    ? placeholder.reasons.slice()
                    : [],
                score: null,
                blocks: null,
                confidence: null,
                place: null,
                shared_score_place: null,
                display_order: nextDisplayOrder,
                level: "",
                position_delta: null
            });
            existingIds.add(employeeId);
        });
        return Object.assign({}, payload, {entries: entries});
    }

    function renderPayloadMetadata(payload) {
        state.payload = payload;
        updateScopeControls(payload);
        if (!state.qaPreview && !state.qaLive) {
            ensureInitialProgramGroup();
        }

        if (elements.status) {
            if (state.qaLive) {
                elements.status.textContent = (
                    "Синтетический live-тест — неофициально"
                );
            } else if (state.qaPreview) {
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
        if (elements.qaLiveRevision) {
            elements.qaLiveRevision.textContent = (
                payload.snapshot_revision == null
                    ? "—"
                    : String(payload.snapshot_revision)
            );
        }
        if (elements.qaLiveSourceFingerprint) {
            elements.qaLiveSourceFingerprint.textContent = shortFingerprint(
                payload.source_fingerprint
            );
        }
        if (elements.qaLiveScoreFingerprint) {
            elements.qaLiveScoreFingerprint.textContent = shortFingerprint(
                payload.shift_score_fingerprint
            );
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
        if (state.qaLive && state.qaLiveState) {
            url.searchParams.set(
                "qa_run_id",
                state.qaLiveState.run_id
            );
            url.searchParams.set(
                "qa_step",
                String(state.qaLiveState.step)
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

    function qaLiveStateGroupKey(qaState) {
        if (!qaState) return "";
        return groupKey(
            qaState.rating_period_id,
            qaState.watch_composition_id,
            qaState.shift_type
        );
    }

    function validateQaLiveState(documentValue) {
        if (!documentValue || typeof documentValue !== "object") {
            return false;
        }
        var expectedKeys = [
            "schema",
            "schema_version",
            "synthetic",
            "official",
            "official_rating_eligible",
            "run_id",
            "site_code",
            "rating_period_id",
            "watch_composition_id",
            "step",
            "virtual_at",
            "shift_type",
            "placeholders"
        ].sort();
        if (
            Object.keys(documentValue).sort().join("|")
            !== expectedKeys.join("|")
        ) {
            return false;
        }
        if (
            documentValue.schema
                !== "driver-rating-qa-live-state"
            || documentValue.schema_version !== 1
            || documentValue.synthetic !== true
            || documentValue.official !== false
            || documentValue.official_rating_eligible !== false
            || documentValue.run_id !== config.qaLiveRunId
            || documentValue.site_code !== config.qaLiveSiteCode
            || !Number.isSafeInteger(documentValue.step)
            || documentValue.step < 0
            || !["day", "night"].includes(documentValue.shift_type)
            || !Number.isSafeInteger(documentValue.rating_period_id)
            || documentValue.rating_period_id <= 0
            || !Number.isSafeInteger(documentValue.watch_composition_id)
            || documentValue.watch_composition_id <= 0
            || !parseDate(documentValue.virtual_at)
            || !Array.isArray(documentValue.placeholders)
        ) {
            return false;
        }
        var forbiddenKeys = [
            "score",
            "place",
            "shared_score_place",
            "blocks",
            "kpi",
            "weights",
            "confidence",
            "source_fingerprint",
            "shift_score_fingerprint",
            "payload_fingerprint",
            "snapshot_revision"
        ];
        return documentValue.placeholders.every(function (placeholder) {
            if (!placeholder || typeof placeholder !== "object") {
                return false;
            }
            var placeholderKeys = Object.keys(placeholder).sort();
            if (
                placeholderKeys.join("|")
                !== [
                    "employee_id",
                    "full_name",
                    "reasons",
                    "status"
                ].sort().join("|")
            ) {
                return false;
            }
            return (
                Number.isSafeInteger(placeholder.employee_id)
                && placeholder.employee_id > 0
                && ["withheld", "not_observed"].includes(
                    placeholder.status
                )
                && Array.isArray(placeholder.reasons)
                && !forbiddenKeys.some(function (key) {
                    return Object.prototype.hasOwnProperty.call(
                        placeholder,
                        key
                    );
                })
            );
        });
    }

    function clearQaLiveMaterializedMetadata() {
        if (elements.updatedAt) {
            elements.updatedAt.textContent = "—";
        }
        if (elements.qaLiveRevision) {
            elements.qaLiveRevision.textContent = "—";
        }
        if (elements.qaLiveSourceFingerprint) {
            elements.qaLiveSourceFingerprint.textContent = "—";
        }
        if (elements.qaLiveScoreFingerprint) {
            elements.qaLiveScoreFingerprint.textContent = "—";
        }
    }

    function showQaLiveWaiting(message) {
        state.payload = null;
        state.activeGroupKey = "";
        state.sourceFingerprint = "";
        clearQaLiveMaterializedMetadata();
        if (elements.status) {
            elements.status.textContent = "Ожидание QA-live шага";
        }
        if (elements.qaLiveStateStatus) {
            elements.qaLiveStateStatus.textContent = (
                message || "Ожидание первого шага"
            );
        }
        showMessage(
            "Ожидание первого закрытого шага",
            "Рейтинг появится после публикации материализованного снимка."
        );
    }

    async function loadQaLive() {
        if (
            !state.qaLive
            || !config.qaLiveStateUrl
            || !config.qaLiveRunId
        ) {
            showQaLiveWaiting("QA-live режим не настроен.");
            return;
        }
        if (state.qaLiveRequestController) {
            state.qaLiveRequestController.abort();
        }
        state.qaLiveRequestGeneration += 1;
        var generation = state.qaLiveRequestGeneration;
        var controller = new AbortController();
        state.qaLiveRequestController = controller;
        try {
            var response = await window.fetch(
                config.qaLiveStateUrl,
                {
                    method: "GET",
                    credentials: "same-origin",
                    cache: "no-store",
                    headers: {"Accept": "application/json"},
                    signal: controller.signal
                }
            );
            var qaState = await response.json();
            if (
                generation !== state.qaLiveRequestGeneration
                || controller.signal.aborted
            ) {
                return;
            }
            if (!response.ok || !validateQaLiveState(qaState)) {
                state.qaLiveState = null;
                showQaLiveWaiting(
                    qaState.error || "Ожидание актуального QA-live шага."
                );
                return;
            }
            var previousGroupKey = qaLiveStateGroupKey(
                state.qaLiveState
            );
            var nextGroupKey = qaLiveStateGroupKey(qaState);
            if (previousGroupKey && previousGroupKey !== nextGroupKey) {
                state.payload = null;
                state.activeGroupKey = "";
                showMessage(
                    "Переключаем закрытую смену",
                    "Ожидаем снимок новой QA-live группы."
                );
            }
            state.qaLiveState = qaState;
            state.selectedPeriod = String(qaState.rating_period_id);
            state.selectedComposition = String(
                qaState.watch_composition_id
            );
            state.shiftType = qaState.shift_type;
            if (elements.shiftType) {
                elements.shiftType.value = state.shiftType;
                elements.shiftType.disabled = true;
            }
            renderQaLiveStateMetadata(qaState);
            await loadRating({
                forceRefresh: true,
                replaceRequest: true
            });
        } catch (error) {
            if (
                error
                && error.name === "AbortError"
            ) {
                return;
            }
            if (generation !== state.qaLiveRequestGeneration) return;
            state.qaLiveState = null;
            showQaLiveWaiting(
                "Нет актуального состояния синтетического прогона."
            );
        } finally {
            if (
                generation === state.qaLiveRequestGeneration
                && state.qaLiveRequestController === controller
            ) {
                state.qaLiveRequestController = null;
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
        var requestedIdentityComplete = Boolean(
            state.selectedPeriod && state.selectedComposition
        );
        var requestedQaLiveStep = state.qaLiveState
            ? state.qaLiveState.step
            : null;
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
        if (
            state.activeGroupKey
            && state.activeGroupKey !== requestedGroupKey
            && !cachedGroup
        ) {
            showMessage(
                "Ожидаем снимок выбранной группы",
                "Данные другой смены на этом месте не показываются."
            );
            state.payload = null;
            state.activeGroupKey = "";
        }

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
            if (
                generation !== state.requestGeneration
                || (
                    state.qaLive
                    && (
                        !state.qaLiveState
                        || state.qaLiveState.step !== requestedQaLiveStep
                        || qaLiveStateGroupKey(state.qaLiveState)
                            !== requestedGroupKey
                    )
                )
            ) {
                return;
            }

            if (!response.ok) {
                if (
                    response.status === 400
                    && bootstrapSelection(payload)
                ) {
                    state.requestInFlight = false;
                    await loadRating({replaceRequest: true});
                    return;
                }
                if (
                    state.qaLive
                    && [409, 503].includes(response.status)
                ) {
                    state.payload = null;
                    state.activeGroupKey = "";
                    state.sourceFingerprint = "";
                    state.groupCache.delete(requestedGroupKey);
                    clearQaLiveMaterializedMetadata();
                    showMessage(
                        "Ожидаем согласованный QA-live снимок",
                        payload.error
                        || "Текущий шаг будет прочитан повторно."
                    );
                    return;
                }
                var hasRequestedGroupPayload = Boolean(
                    state.payload
                    && state.activeGroupKey === requestedGroupKey
                );
                if ([401, 403, 409].includes(response.status)) {
                    state.payload = null;
                    state.activeGroupKey = "";
                    if ([401, 403].includes(response.status)) {
                        state.groupCache.clear();
                    } else {
                        state.groupCache.delete(requestedGroupKey);
                    }
                    showMessage(
                        "Рейтинг временно недоступен",
                        payload.error || "Проверьте доступ и выбранную группу."
                    );
                } else if (hasRequestedGroupPayload && elements.status) {
                    elements.status.textContent = "Показан последний снимок";
                } else {
                    state.payload = null;
                    state.activeGroupKey = "";
                    showMessage(
                        "Не удалось получить рейтинг",
                        payload.error || "Сервер не вернул готовый снимок."
                    );
                }
                return;
            }

            var fingerprint = String(payload.source_fingerprint || "");
            var responseGroupKey = payloadGroupKey(payload);
            if (
                (
                    (state.qaLive || requestedIdentityComplete)
                    && responseGroupKey !== requestedGroupKey
                )
                || (
                    state.qaLive
                    && responseGroupKey
                        !== qaLiveStateGroupKey(state.qaLiveState)
                )
            ) {
                state.payload = null;
                state.activeGroupKey = "";
                state.sourceFingerprint = "";
                if (state.qaLive) {
                    state.groupCache.delete(requestedGroupKey);
                    clearQaLiveMaterializedMetadata();
                }
                showMessage(
                    "Снимок другой группы отклонён",
                    "Ожидаем результат выбранной закрытой смены."
                );
                return;
            }
            payload = applyQaLivePlaceholders(payload);
            var previousCachedGroup = state.groupCache.get(responseGroupKey);
            if (
                !state.qaLive
                &&
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
            if (
                state.payload
                && state.activeGroupKey === requestedGroupKey
                && elements.status
            ) {
                elements.status.textContent = "Показан последний снимок";
            } else {
                state.payload = null;
                state.activeGroupKey = "";
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
            if (state.qaLive) {
                loadQaLive();
            } else {
                loadRating();
            }
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
            Number(window.innerWidth) <= 1180
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
        if (elements.period && !state.qaPreview && !state.qaLive) {
            elements.period.addEventListener("change", function () {
                state.selectedPeriod = elements.period.value;
                state.selectedComposition = "";
                state.presentationPlaylist = [];
                state.refreshRemaining = state.refreshSeconds;
                updateProgramUi();
                loadRating({replaceRequest: true});
            });
        }
        if (
            elements.composition
            && !state.qaPreview
            && !state.qaLive
        ) {
            elements.composition.addEventListener("change", function () {
                state.selectedComposition = elements.composition.value;
                state.rotationRemaining = state.rotationSeconds;
                updateProgramUi();
                loadRating({replaceRequest: true});
            });
        }
        if (elements.shiftType) {
            elements.shiftType.addEventListener("change", function () {
                if (state.qaLive) {
                    elements.shiftType.value = state.shiftType;
                    return;
                }
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
    } else if (state.qaLive) {
        loadQaLive();
        state.timerId = window.setInterval(tick, 1000);
    } else {
        loadRating();
        state.timerId = window.setInterval(tick, 1000);
    }

    window.RatingTvScreen = {
        formatSeconds: formatSeconds,
        layoutGrid: layoutGrid,
        loadRating: loadRating,
        loadQaLive: loadQaLive,
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
