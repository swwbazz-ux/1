(function () {
    "use strict";

    var root = document.querySelector("[data-settlement-root]");
    if (!root) return;

    var state = {
        dormitory: "5",
        floor: "1",
        search: ""
    };
    var rooms = Array.from(root.querySelectorAll("[data-room-card]"));
    var dormitorySections = Array.from(root.querySelectorAll("[data-dormitory-section]"));
    var floorSections = Array.from(root.querySelectorAll("[data-floor-section]"));
    var selectedRoomCount = root.querySelector("[data-selected-room-count]");
    var selectedTransferredRoomCount = root.querySelector("[data-selected-transferred-room-count]");
    var globalFreeCount = root.querySelector("[data-free-bed-count]");
    var globalOccupiedCount = root.querySelector("[data-occupied-bed-count]");
    var selectedUnavailableBedCount = root.querySelector("[data-selected-unavailable-bed-count]");
    var searchInput = root.querySelector("[data-settlement-search]");
    var panel = root.querySelector("[data-room-panel]");
    var panelBackdrop = root.querySelector("[data-room-panel-backdrop]");
    var panelClose = root.querySelector("[data-room-panel-close]");
    var employeePanel = root.querySelector("[data-employee-panel]");
    var employeePanelBackdrop = root.querySelector("[data-employee-panel-backdrop]");
    var employeePanelClose = root.querySelector("[data-employee-panel-close]");
    var employeePanelTitle = root.querySelector("[data-employee-panel-title]");
    var employeePanelBody = root.querySelector("[data-employee-panel-body]");
    var panelTitle = root.querySelector("[data-room-panel-title]");
    var panelLocation = root.querySelector("[data-room-panel-location]");
    var panelStatus = root.querySelector("[data-room-panel-status]");
    var panelOccupiedCount = root.querySelector("[data-room-occupied-count]");
    var panelFreeCount = root.querySelector("[data-room-free-count]");
    var panelBeds = root.querySelector("[data-room-panel-beds]");
    var placementHint = root.querySelector("[data-placement-hint]");
    var placementPanel = root.querySelector("[data-placement-panel]");
    var settlementForm = root.querySelector("[data-settlement-form]");
    var employeeSearch = root.querySelector("[data-employee-search]");
    var employeeResults = root.querySelector("[data-employee-results]");
    var selectedEmployeeCard = root.querySelector("[data-selected-employee]");
    var selectedEmployeeName = root.querySelector("[data-selected-employee-name]");
    var selectedEmployeeMeta = root.querySelector("[data-selected-employee-meta]");
    var selectedEmployeeCurrentPlace = root.querySelector("[data-selected-employee-current-place]");
    var selectedEmployeeTargetPlace = root.querySelector("[data-selected-employee-target-place]");
    var assignmentType = root.querySelector("[data-assignment-type-select]");
    var assignmentEnd = root.querySelector("[data-assignment-end]");
    var assignmentEndInput = root.querySelector("[data-assignment-end-input]");
    var settleButton = root.querySelector("[data-settle-button]");
    var placementTitle = root.querySelector("[data-placement-title]");
    var occupiedActions = root.querySelector("[data-occupied-actions]");
    var relocateButton = root.querySelector("[data-relocate-button]");
    var releaseButton = root.querySelector("[data-release-button]");
    var placementFeedback = root.querySelector("[data-placement-feedback]");
    var relocationModal = root.querySelector("[data-relocation-modal]");
    var relocationModalBackdrop = root.querySelector("[data-relocation-modal-backdrop]");
    var relocationModalContent = root.querySelector("[data-relocation-modal-content]");
    var relocationModalClose = root.querySelector("[data-relocation-modal-close]");
    var relocationModalCancel = root.querySelector("[data-relocation-modal-cancel]");
    var autoPreviewModal = root.querySelector("[data-auto-preview-modal]");
    var autoPreviewModalBackdrop = root.querySelector("[data-auto-preview-modal-backdrop]");
    var autoPreviewModalOpen = root.querySelector("[data-auto-preview-modal-open]");
    var autoPreviewModalClose = root.querySelector("[data-auto-preview-modal-close]");
    var autoPreviewModalCancel = root.querySelector("[data-auto-preview-modal-cancel]");
    var autoPreviewForm = root.querySelector("[data-auto-preview-form]");
    var unsettledPanelToggles = Array.from(root.querySelectorAll("[data-unsettled-panel-toggle]"));
    var unsettledPanel = root.querySelector("[data-unsettled-panel]");
    var unsettledPanelClose = root.querySelector("[data-unsettled-panel-close]");
    var unsettledSearch = root.querySelector("[data-unsettled-search]");
    var unsettledVisibleCount = root.querySelector("[data-unsettled-visible-count]");
    var unsettledFilterEmpty = root.querySelector("[data-unsettled-filter-empty]");
    var unsettledEmployees = Array.from(root.querySelectorAll("[data-unsettled-employee]"));
    var unsettledShiftButtons = Array.from(root.querySelectorAll("[data-unsettled-shift-filter]"));
    var controlPanel = root.querySelector("[data-control-panel]");
    var controlStatus = root.querySelector("[data-control-status]");
    var controlMessage = root.querySelector("[data-control-message]");
    var controlAcquireButton = root.querySelector("[data-control-acquire]");
    var controlReleaseButton = root.querySelector("[data-control-release]");
    var activeRoom = null;
    var selectedBed = null;
    var selectedEmployee = null;
    var relocationSourceBed = null;
    var searchTimer = null;
    var searchSequence = 0;
    var saving = false;
    var unsettledShift = "all";
    var unsettledPanelTrigger = null;
    var autoPreviewOpenOnceKey = "settlement:auto-preview-open-once";
    var controlHeld = false;
    var controlState = "free";
    var controlActionInFlight = false;
    var heartbeatInFlight = false;
    var heartbeatTimer = null;
    var heartbeatRetryCount = 0;
    var heartbeatRecoveryPending = false;
    var controlAccessDenied = false;
    var heartbeatRetryDelays = [2500, 6000];

    function hasFloorSelection(dormitory, floor) {
        return floorSections.some(function (section) {
            return (
                section.dataset.dormitory === String(dormitory)
                && section.dataset.floorSection === String(floor)
            );
        });
    }

    function firstFloorForDormitory(dormitory) {
        var section = floorSections.find(function (candidate) {
            return candidate.dataset.dormitory === String(dormitory);
        });
        return section ? section.dataset.floorSection : null;
    }

    function syncMapSelectionButtons() {
        root.querySelectorAll("[data-dorm-filter]").forEach(function (button) {
            var active = button.dataset.dormFilter === state.dormitory;
            button.classList.toggle("is-active", active);
            button.setAttribute("aria-pressed", String(active));
        });
        root.querySelectorAll("[data-floor-filter]").forEach(function (button) {
            var active = button.dataset.floorFilter === state.floor;
            button.classList.toggle("is-active", active);
            button.setAttribute("aria-pressed", String(active));
        });
    }

    function restoreMapSelection() {
        var params = new URLSearchParams(window.location.search);
        var requestedDormitory = params.get("dormitory");
        var requestedFloor = params.get("floor");
        if (hasFloorSelection(requestedDormitory, requestedFloor)) {
            state.dormitory = requestedDormitory;
            state.floor = requestedFloor;
        } else {
            state.dormitory = "5";
            state.floor = firstFloorForDormitory(state.dormitory) || "1";
        }
        syncMapSelectionButtons();
    }

    function persistMapSelection() {
        var url = new URL(window.location.href);
        url.searchParams.set("dormitory", state.dormitory);
        url.searchParams.set("floor", state.floor);
        window.history.replaceState({}, "", url.pathname + url.search + url.hash);
    }

    function normalizeSearch(value) {
        return String(value || "").trim().toLocaleLowerCase("ru-RU");
    }

    function showUnsettledPhotoFallback(card) {
        var photo = card.querySelector("[data-unsettled-photo]");
        var fallback = card.querySelector("[data-unsettled-photo-fallback]");
        if (photo) photo.hidden = true;
        if (fallback) fallback.hidden = false;
    }

    function loadUnsettledEmployeePhoto(card) {
        var photo = card.querySelector("[data-unsettled-photo]");
        var fallback = card.querySelector("[data-unsettled-photo-fallback]");
        if (!photo) return;
        var photoUrl = String(photo.dataset.photoUrl || "").trim();
        showUnsettledPhotoFallback(card);
        if (!photoUrl || photo.dataset.photoFailed === photoUrl) return;
        if (photo.dataset.photoSource === photoUrl && photo.complete && photo.naturalWidth) {
            photo.hidden = false;
            if (fallback) fallback.hidden = true;
            return;
        }
        photo.onload = function () {
            if (photo.dataset.photoSource !== photoUrl || !photo.naturalWidth) return;
            photo.dataset.photoFailed = "";
            photo.hidden = false;
            if (fallback) fallback.hidden = true;
        };
        photo.onerror = function () {
            if (photo.dataset.photoSource !== photoUrl) return;
            photo.dataset.photoFailed = photoUrl;
            showUnsettledPhotoFallback(card);
        };
        photo.dataset.photoSource = photoUrl;
        if (photo.getAttribute("src") !== photoUrl) {
            photo.setAttribute("src", photoUrl);
        }
    }

    function renderUnsettledEmployees() {
        var query = normalizeSearch(unsettledSearch ? unsettledSearch.value : "");
        var visible = 0;
        unsettledEmployees.forEach(function (card) {
            var searchMatch = !query || normalizeSearch(card.dataset.searchText).includes(query);
            var shiftMatch = unsettledShift === "all" || card.dataset.shift === unsettledShift;
            card.hidden = !(searchMatch && shiftMatch);
            if (!card.hidden) {
                visible += 1;
                loadUnsettledEmployeePhoto(card);
            }
        });
        if (unsettledVisibleCount) unsettledVisibleCount.textContent = String(visible);
        if (unsettledFilterEmpty) unsettledFilterEmpty.hidden = visible !== 0;
    }

    function closeUnsettledPanel(restoreFocus) {
        if (!unsettledPanel) return;
        unsettledPanel.hidden = true;
        unsettledPanel.setAttribute("aria-hidden", "true");
        unsettledPanelToggles.forEach(function (toggle) {
            toggle.hidden = false;
            toggle.removeAttribute("tabindex");
            toggle.setAttribute("aria-expanded", "false");
            toggle.setAttribute(
                "aria-label",
                "Открыть панель нерасселённых сотрудников"
            );
        });
        document.body.classList.remove("settlement-unsettled-panel-open");
        if (restoreFocus !== false && unsettledPanelTrigger) unsettledPanelTrigger.focus();
        unsettledPanelTrigger = null;
    }

    function openUnsettledPanel(trigger) {
        if (!unsettledPanel) return;
        closePanel();
        unsettledPanelTrigger = trigger || unsettledPanelToggles[0] || null;
        unsettledPanel.hidden = false;
        unsettledPanel.setAttribute("aria-hidden", "false");
        unsettledPanelToggles.forEach(function (toggle) {
            toggle.hidden = true;
            toggle.setAttribute("tabindex", "-1");
            toggle.setAttribute("aria-expanded", "true");
            toggle.setAttribute(
                "aria-label",
                "Открыть панель нерасселённых сотрудников"
            );
        });
        document.body.classList.add("settlement-unsettled-panel-open");
        renderUnsettledEmployees();
        if (unsettledSearch) unsettledSearch.focus();
    }

    function toggleUnsettledPanel(event) {
        if (!unsettledPanel) return;
        if (unsettledPanel.hidden) {
            openUnsettledPanel(event.currentTarget);
        } else {
            closeUnsettledPanel(true);
        }
    }

    function shortPersonName(value) {
        var parts = String(value || "").trim().split(/\s+/).filter(Boolean);
        if (!parts.length || String(value).trim() === "Не указано") return "";
        if (parts.length === 1) return parts[0];
        var initials = parts.slice(1, 3).map(function (part) {
            return part.charAt(0).toUpperCase() + ".";
        }).join("");
        return parts[0] + " " + initials;
    }

    function personInitials(value) {
        var parts = String(value || "").trim().split(/\s+/).filter(Boolean);
        if (!parts.length || String(value).trim() === "Не указано") return "";
        return parts.slice(0, 2).map(function (part) {
            return part.charAt(0).toUpperCase();
        }).join("");
    }

    function compactShiftLabel(value) {
        var label = String(value || "").trim();
        if (!label || label === "Не указано") return "";
        if (label === "День") return "Д";
        if (label === "Ночь") return "Н";
        return label;
    }

    function renderBedHoverCard(bed, occupied) {
        var hoverCard = bed.querySelector("[data-bed-hover-card]");
        if (!occupied) {
            if (hoverCard) hoverCard.remove();
            return;
        }

        if (!hoverCard) {
            hoverCard = document.createElement("span");
            hoverCard.className = "settlement-bed-hover-card";
            hoverCard.setAttribute("data-bed-hover-card", "");
            hoverCard.setAttribute("aria-hidden", "true");
            bed.appendChild(hoverCard);
        }

        var shortName = document.createElement("strong");
        shortName.textContent = shortPersonName(bed.dataset.occupantName);
        var position = document.createElement("span");
        position.textContent = bed.dataset.positionLabel || "";
        var work = document.createElement("span");
        work.textContent = bed.dataset.workLabel || "";
        var shiftLabel = String(bed.dataset.shiftLabel || "").trim();
        hoverCard.replaceChildren(shortName, position, work);
        if (shiftLabel && shiftLabel !== "Не указано") {
            var shift = document.createElement("small");
            shift.textContent = shiftLabel;
            hoverCard.appendChild(shift);
        }
    }

    function isTransferredBed(bed) {
        var room = bed && bed.closest("[data-room-card]");
        return Boolean(
            room
            && room.dataset.transferStatus === "transferred"
            && room.classList.contains("is-transferred")
        );
    }

    function renderMapBed(bed) {
        var occupied = bed.dataset.occupied === "true";
        var unavailable = !isTransferredBed(bed) || Boolean(bed.disabled);
        var occupantName = shortPersonName(bed.dataset.occupantName);
        var photoUrl = occupied ? String(bed.dataset.occupantPhotoUrl || "").trim() : "";
        var shiftLabel = compactShiftLabel(bed.dataset.shiftLabel);
        var photo = bed.querySelector("[data-bed-photo-image]");
        var photoFallback = bed.querySelector("[data-bed-photo-fallback]");
        var avatar = bed.querySelector("[data-bed-avatar-initial]");
        var emptyIcon = bed.querySelector("[data-bed-empty-icon]");
        var person = bed.querySelector("[data-bed-person-label]");
        var status = bed.querySelector("[data-bed-status]");
        var shift = bed.querySelector("[data-bed-shift-badge]");
        var dragHandle = bed.querySelector("[data-bed-photo-slot]");

        bed.dataset.bedState = unavailable ? "unavailable" : (occupied ? "occupied" : "free");
        bed.classList.toggle("is-occupied", occupied);
        bed.classList.toggle("is-free", !occupied);
        bed.classList.remove("is-drag-source", "is-drop-target", "is-dragging");
        if (dragHandle) {
            dragHandle.classList.remove("is-dragging");
            dragHandle.removeAttribute("aria-grabbed");
            dragHandle.style.removeProperty("pointer-events");
            if (controlHeld && occupied && !unavailable) {
                dragHandle.setAttribute("data-bed-drag-handle", "");
                dragHandle.setAttribute("draggable", "true");
            } else {
                dragHandle.removeAttribute("data-bed-drag-handle");
                dragHandle.removeAttribute("draggable");
            }
        }
        if (avatar) {
            avatar.textContent = personInitials(bed.dataset.occupantName);
            avatar.hidden = !occupied;
        }
        if (emptyIcon) emptyIcon.hidden = occupied;
        if (person) {
            person.textContent = occupantName;
            person.hidden = !occupied;
        }
        if (status) {
            status.textContent = unavailable ? "Недоступна" : "Свободна";
            status.hidden = occupied;
        }
        if (shift) {
            shift.textContent = shiftLabel;
            shift.hidden = !occupied || !shiftLabel;
        }
        renderBedHoverCard(bed, occupied);

        function showPhotoFallback() {
            if (photo) {
                photo.classList.remove("is-loaded");
            }
            if (photoFallback) photoFallback.hidden = false;
            bed.classList.remove("has-photo");
            bed.classList.toggle("no-photo", occupied);
        }

        showPhotoFallback();
        if (!photo) return;
        photo.onload = null;
        photo.onerror = null;
        if (!photoUrl) {
            photo.removeAttribute("src");
            photo.dataset.photoSource = "";
            return;
        }
        var floorSection = bed.closest("[data-floor-section]");
        if (floorSection && floorSection.hidden) return;

        photo.dataset.photoSource = photoUrl;
        photo.onload = function () {
            if (photo.dataset.photoSource !== photoUrl) return;
            if (String(bed.dataset.occupantPhotoUrl || "").trim() !== photoUrl) return;
            if (!photo.naturalWidth) return;
            photo.classList.add("is-loaded");
            if (photoFallback) photoFallback.hidden = true;
            bed.classList.add("has-photo");
            bed.classList.remove("no-photo");
        };
        photo.onerror = function () {
            if (photo.dataset.photoSource !== photoUrl) return;
            showPhotoFallback();
        };
        if (photo.getAttribute("src") !== photoUrl) {
            photo.setAttribute("src", photoUrl);
        } else if (photo.complete && photo.naturalWidth) {
            photo.onload();
        }
    }

    function selectFilter(buttons, activeButton, key, value) {
        buttons.forEach(function (button) {
            var active = button === activeButton;
            button.classList.toggle("is-active", active);
            button.setAttribute("aria-pressed", String(active));
        });
        state[key] = value;
        if (key === "dormitory" && !hasFloorSelection(state.dormitory, state.floor)) {
            state.floor = firstFloorForDormitory(state.dormitory) || "1";
            syncMapSelectionButtons();
        }
        if (key === "dormitory" || key === "floor") persistMapSelection();
        applyFilters();
    }

    function matchesRoom(room) {
        var dormitoryMatch = room.dataset.dormitory === state.dormitory;
        var floorMatch = room.dataset.floor === state.floor;
        var searchMatch = (
            !state.search
            || String(room.dataset.search || "").includes(state.search)
        );
        return dormitoryMatch && floorMatch && searchMatch;
    }

    function selectedFloorRooms() {
        return rooms.filter(function (room) {
            return (
                room.dataset.dormitory === state.dormitory
                && room.dataset.floor === state.floor
            );
        });
    }

    function updateSelectionSummary() {
        var selectedRooms = selectedFloorRooms();
        var transferredRooms = selectedRooms.filter(function (room) {
            return room.dataset.transferStatus === "transferred";
        });
        var occupiedBeds = 0;
        var freeBeds = 0;
        var unavailableBeds = 0;

        selectedRooms.forEach(function (room) {
            var beds = Array.from(room.querySelectorAll("[data-bed]"));
            if (room.dataset.transferStatus !== "transferred") {
                unavailableBeds += beds.length;
                return;
            }
            occupiedBeds += beds.filter(function (bed) {
                return bed.dataset.occupied === "true";
            }).length;
            freeBeds += beds.filter(function (bed) {
                return bed.dataset.occupied !== "true";
            }).length;
        });

        if (selectedRoomCount) selectedRoomCount.textContent = String(selectedRooms.length);
        if (selectedTransferredRoomCount) {
            selectedTransferredRoomCount.textContent = String(transferredRooms.length);
        }
        if (globalFreeCount) globalFreeCount.textContent = String(freeBeds);
        if (globalOccupiedCount) globalOccupiedCount.textContent = String(occupiedBeds);
        if (selectedUnavailableBedCount) {
            selectedUnavailableBedCount.textContent = String(unavailableBeds);
        }
    }

    function applyFilters() {
        dormitorySections.forEach(function (section) {
            section.hidden = section.dataset.dormitorySection !== state.dormitory;
        });
        floorSections.forEach(function (section) {
            section.hidden = (
                section.dataset.dormitory !== state.dormitory
                || section.dataset.floorSection !== state.floor
            );
        });
        floorSections.filter(function (section) {
            return !section.hidden;
        }).forEach(function (section) {
            section.querySelectorAll("[data-bed]").forEach(renderMapBed);
        });

        rooms.forEach(function (room) {
            var match = matchesRoom(room);
            var selectedFloor = (
                room.dataset.dormitory === state.dormitory
                && room.dataset.floor === state.floor
            );
            room.classList.toggle("is-filter-muted", selectedFloor && !match);
            room.setAttribute("data-filter-match", String(match));
        });
        if (activeRoom && !selectedFloorRooms().includes(activeRoom)) closePanel();
        updateSelectionSummary();
    }

    function element(tagName, className, text) {
        var node = document.createElement(tagName);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function addBedDetail(list, label, value) {
        var item = document.createElement("div");
        item.appendChild(element("dt", "", label));
        item.appendChild(element("dd", "", value || "Не указано"));
        list.appendChild(item);
    }

    function bedLabel(bed) {
        return "Блок " + bed.dataset.blockLabel + " · " + bed.dataset.positionLabel;
    }

    function renderBedCards() {
        if (!activeRoom || !panelBeds) return;
        panelBeds.replaceChildren();
        Array.from(activeRoom.querySelectorAll("[data-bed]")).forEach(function (bed) {
            var occupied = bed.dataset.occupied === "true";
            var card = element(
                "button",
                "settlement-room-bed-card " + (occupied ? "is-occupied" : "is-free")
            );
            card.type = "button";
            card.dataset.panelBedId = bed.dataset.bedId;
            card.classList.toggle("is-selected", selectedBed === bed);
            card.setAttribute("aria-pressed", String(selectedBed === bed));

            var main = element("span", "settlement-room-bed-card-main");
            main.appendChild(element("strong", "", bedLabel(bed)));
            main.appendChild(element("span", "", occupied ? "Занято" : "Свободно"));
            card.appendChild(main);

            var details = document.createElement("dl");
            addBedDetail(details, "Жилец", bed.dataset.occupantName);
            addBedDetail(details, "День / ночь", bed.dataset.shiftLabel);
            addBedDetail(details, "Техника / должность", bed.dataset.workLabel);
            addBedDetail(details, "Закрепление", bed.dataset.assignmentTypeLabel);
            card.appendChild(details);

            card.addEventListener("click", function () {
                selectBed(bed);
            });
            panelBeds.appendChild(card);
        });
    }

    function resetEmployeeSelection() {
        selectedEmployee = null;
        if (selectedEmployeeCard) selectedEmployeeCard.hidden = true;
        if (selectedEmployeeName) selectedEmployeeName.textContent = "";
        if (selectedEmployeeMeta) selectedEmployeeMeta.textContent = "";
        if (selectedEmployeeCurrentPlace) {
            selectedEmployeeCurrentPlace.textContent = "";
            selectedEmployeeCurrentPlace.hidden = true;
        }
        if (selectedEmployeeTargetPlace) {
            selectedEmployeeTargetPlace.textContent = "";
            selectedEmployeeTargetPlace.hidden = true;
        }
        if (employeeResults) {
            employeeResults.hidden = true;
            employeeResults.replaceChildren();
        }
        if (employeeSearch) employeeSearch.value = "";
        if (assignmentType) assignmentType.value = "";
        if (assignmentEndInput) assignmentEndInput.value = "";
        updateAssignmentEndVisibility();
        updateSettleButton();
    }

    function resetRelocation() {
        relocationSourceBed = null;
        if (panel) panel.classList.remove("is-relocating-with-target");
        resetEmployeeSelection();
    }

    function bedLocation(bed) {
        if (!bed) return "";
        return [
            bed.dataset.dormitoryLabel,
            bed.dataset.floorLabel,
            bed.dataset.roomLabel,
            "блок " + bed.dataset.blockLabel,
            bed.dataset.positionLabel
        ].filter(Boolean).join(", ");
    }

    function updateRelocationDetails() {
        var hasTarget = Boolean(
            relocationSourceBed
            && selectedBed
            && selectedBed.dataset.occupied !== "true"
        );
        if (panel) panel.classList.toggle("is-relocating-with-target", hasTarget);
        if (selectedEmployeeCurrentPlace) {
            selectedEmployeeCurrentPlace.textContent = relocationSourceBed
                ? "Текущее место: " + bedLocation(relocationSourceBed)
                : "";
            selectedEmployeeCurrentPlace.hidden = !relocationSourceBed;
        }
        if (selectedEmployeeTargetPlace) {
            selectedEmployeeTargetPlace.textContent = hasTarget
                ? "Новое место: " + bedLocation(selectedBed)
                : "";
            selectedEmployeeTargetPlace.hidden = !hasTarget;
        }
    }

    function updateAssignmentEndVisibility() {
        if (!assignmentEnd || !assignmentEndInput || !assignmentType) return;
        var temporary = assignmentType.value === "temporary";
        assignmentEnd.hidden = !temporary;
        assignmentEndInput.required = temporary;
        if (!temporary) assignmentEndInput.value = "";
    }

    function updateEmployeeSearchAvailability() {
        if (!employeeSearch) return;
        var label = employeeSearch.closest(".settlement-employee-search");
        if (label) label.hidden = Boolean(relocationSourceBed);
    }

    function showFeedback(message, isError) {
        if (!placementFeedback) return;
        placementFeedback.textContent = message;
        placementFeedback.classList.toggle("is-error", Boolean(isError));
        placementFeedback.hidden = !message;
    }

    function updatePlacementPanel() {
        if (!placementHint || !settlementForm) return;
        updateRelocationDetails();
        showFeedback("", false);
        settlementForm.hidden = true;
        placementHint.hidden = false;
        if (occupiedActions) occupiedActions.hidden = true;
        updateEmployeeSearchAvailability();

        if (!controlHeld) {
            if (placementTitle) placementTitle.textContent = "Действия недоступны";
            placementHint.textContent = "Сначала начните работу и дождитесь подтверждения управления.";
            return;
        }

        if (!selectedBed) {
            if (placementTitle) {
                placementTitle.textContent = relocationSourceBed
                    ? "Переселение сотрудника"
                    : "Заселение сотрудника";
            }
            placementHint.textContent = relocationSourceBed
                ? "Выберите свободное койко-место для переселения выбранного сотрудника."
                : "Выберите свободное койко-место в переданной комнате.";
            return;
        }
        if (selectedBed.dataset.occupied === "true") {
            if (placementTitle) placementTitle.textContent = "Действия с размещением";
            placementHint.textContent = "Это койко-место занято. Можно переселить сотрудника или освободить место.";
            if (occupiedActions) occupiedActions.hidden = false;
            return;
        }
        if (!activeRoom || activeRoom.dataset.transferStatus !== "transferred") {
            placementHint.textContent = "Комната не передана. Заселение в неё недоступно.";
            return;
        }

        if (placementTitle) {
            placementTitle.textContent = relocationSourceBed
                ? "Переселение сотрудника"
                : "Заселение сотрудника";
        }
        placementHint.hidden = true;
        settlementForm.hidden = false;
        if (settleButton) {
            settleButton.textContent = relocationSourceBed ? "Переселить" : "Заселить";
        }
        if (relocationSourceBed && selectedEmployeeCard) {
            selectedEmployeeCard.hidden = false;
        }
        if (employeeSearch && !relocationSourceBed) employeeSearch.focus();
    }

    function selectBed(bed) {
        var changed = selectedBed !== bed;
        if (selectedBed) {
            selectedBed.classList.remove("is-selected");
            selectedBed.setAttribute("aria-pressed", "false");
        }
        selectedBed = bed;
        selectedBed.classList.add("is-selected");
        selectedBed.setAttribute("aria-pressed", "true");
        if (changed && !relocationSourceBed) resetEmployeeSelection();
        renderBedCards();
        updatePlacementPanel();
        updateSettleButton();
    }

    function roomCounts(room) {
        var beds = Array.from(room.querySelectorAll("[data-bed]"));
        var occupied = beds.filter(function (bed) {
            return bed.dataset.occupied === "true";
        }).length;
        return {occupied: occupied, free: beds.length - occupied};
    }

    function renderRoom(room) {
        var counts = roomCounts(room);
        room.dataset.occupiedBeds = String(counts.occupied);
        room.dataset.freeBeds = String(counts.free);
        panelTitle.textContent = "Комната " + room.dataset.roomNumber;
        panelLocation.textContent = "КИС-" + room.dataset.dormitory + " · " + room.dataset.floor + " этаж";
        panelStatus.textContent = (
            room.dataset.transferStatus === "transferred"
                ? "Передана для расселения"
                : "Не передана · действия недоступны"
        );
        panelOccupiedCount.textContent = String(counts.occupied);
        panelFreeCount.textContent = String(counts.free);
        renderBedCards();
        updatePlacementPanel();
    }

    function openRoom(room, bed) {
        closeUnsettledPanel(false);
        activeRoom = room;
        if (selectedBed && !room.contains(selectedBed)) {
            selectedBed.classList.remove("is-selected");
            selectedBed.setAttribute("aria-pressed", "false");
            selectedBed = null;
            resetEmployeeSelection();
        }
        panel.hidden = false;
        panel.setAttribute("aria-hidden", "false");
        panelBackdrop.hidden = false;
        renderRoom(room);
        if (bed) {
            selectBed(bed);
        }
    }

    function closePanel() {
        panel.hidden = true;
        panel.setAttribute("aria-hidden", "true");
        panelBackdrop.hidden = true;
        resetRelocation();
    }

    function closeRelocationModal(force) {
        if (saving && !force) return;
        if (!relocationModal || relocationModal.hidden) return;
        relocationModal.hidden = true;
        relocationModal.setAttribute("aria-hidden", "true");
        if (relocationModalBackdrop) relocationModalBackdrop.hidden = true;
        document.body.classList.remove("settlement-relocation-modal-open");
        if (placementPanel && settlementForm) placementPanel.appendChild(settlementForm);
        if (placementPanel && placementFeedback) placementPanel.appendChild(placementFeedback);
        if (selectedBed) {
            selectedBed.classList.remove("is-selected");
            selectedBed.setAttribute("aria-pressed", "false");
            selectedBed = null;
        }
        resetRelocation();
    }

    function openRelocationModal() {
        if (!relocationModal || !relocationModalBackdrop || !relocationModalContent) return;
        panel.hidden = true;
        panel.setAttribute("aria-hidden", "true");
        panelBackdrop.hidden = true;
        relocationModalContent.appendChild(settlementForm);
        relocationModalContent.appendChild(placementFeedback);
        relocationModal.hidden = false;
        relocationModal.setAttribute("aria-hidden", "false");
        relocationModalBackdrop.hidden = false;
        document.body.classList.add("settlement-relocation-modal-open");
        if (assignmentType) assignmentType.focus();
    }

    function setAutoPreviewModal(open) {
        if (!autoPreviewModal || !autoPreviewModalBackdrop) return;
        autoPreviewModal.hidden = !open;
        autoPreviewModal.setAttribute("aria-hidden", String(!open));
        autoPreviewModalBackdrop.hidden = !open;
        document.body.classList.toggle("settlement-auto-preview-modal-open", open);
        if (autoPreviewModalOpen) autoPreviewModalOpen.setAttribute("aria-expanded", String(open));
    }

    function markAutoPreviewOpenOnce() {
        try {
            window.sessionStorage.setItem(autoPreviewOpenOnceKey, "1");
        } catch (error) {
            // The calculation still works when session storage is unavailable.
        }
    }

    function consumeAutoPreviewOpenOnce() {
        try {
            if (window.sessionStorage.getItem(autoPreviewOpenOnceKey) !== "1") return false;
            window.sessionStorage.removeItem(autoPreviewOpenOnceKey);
            return true;
        } catch (error) {
            return false;
        }
    }

    function closeEmployeePanel() {
        if (!employeePanel || !employeePanelBackdrop) return;
        employeePanel.hidden = true;
        employeePanel.setAttribute("aria-hidden", "true");
        employeePanelBackdrop.hidden = true;
    }

    function renderEmployeePanel(employee) {
        if (!employeePanelBody || !employeePanelTitle) return;
        employeePanelTitle.textContent = employee.full_name;
        employeePanelBody.textContent = "";
        var details = element("dl", "settlement-employee-panel-details");
        [
            ["Табельный номер", employee.personnel_number],
            ["Должность", employee.position],
            ["Подразделение", employee.department],
            ["График", employee.work_schedule],
            ["Бригада", employee.brigade],
            ["Пол", employee.sex],
            ["Телефон", employee.phone],
            ["Фактическое место", employee.residence]
        ].forEach(function (row) {
            details.appendChild(element("dt", "", row[0]));
            details.appendChild(element("dd", "", row[1]));
        });
        employeePanelBody.appendChild(details);
    }

    function openEmployeePanel(employeeId) {
        if (!employeePanel || !employeePanelBackdrop || !employeeId) return;
        var url = String(root.dataset.employeeDetailUrl || "").replace("/0/", "/" + employeeId + "/");
        if (!url) return;
        closePanel();
        fetch(url, {
            credentials: "same-origin",
            headers: {"X-Requested-With": "XMLHttpRequest"}
        })
            .then(function (response) {
                return response.json().then(function (payload) {
                    if (!response.ok || !payload.ok) {
                        throw new Error(payload.error || "Не удалось открыть карточку сотрудника.");
                    }
                    return payload.employee;
                });
            })
            .then(function (employee) {
                renderEmployeePanel(employee);
                employeePanel.hidden = false;
                employeePanel.setAttribute("aria-hidden", "false");
                employeePanelBackdrop.hidden = false;
            })
            .catch(function (error) {
                showFeedback(error.message || "Не удалось открыть карточку сотрудника.", true);
            });
    }

    function employeeMeta(employee) {
        return [
            "Таб. № " + employee.personnel_number,
            employee.shift_label,
            employee.work_label
        ].join(" · ");
    }

    function chooseEmployee(employee) {
        selectedEmployee = employee;
        selectedEmployeeName.textContent = employee.full_name;
        selectedEmployeeMeta.textContent = employeeMeta(employee);
        selectedEmployeeCard.hidden = false;
        employeeResults.hidden = true;
        employeeSearch.value = employee.full_name;
        updateSettleButton();
    }

    function renderEmployeeResults(employees) {
        employeeResults.replaceChildren();
        if (!employees.length) {
            employeeResults.appendChild(element(
                "p",
                "settlement-placement-hint",
                "Активные незаселённые сотрудники не найдены."
            ));
        } else {
            employees.forEach(function (employee) {
                var button = element("button", "settlement-employee-result");
                button.type = "button";
                button.appendChild(element("strong", "", employee.full_name));
                button.appendChild(element("small", "", employeeMeta(employee)));
                button.addEventListener("click", function () {
                    chooseEmployee(employee);
                });
                employeeResults.appendChild(button);
            });
        }
        employeeResults.hidden = false;
    }

    function runEmployeeSearch() {
        var query = employeeSearch.value.trim();
        selectedEmployee = null;
        selectedEmployeeCard.hidden = true;
        updateSettleButton();
        if (query.length < 2) {
            employeeResults.hidden = true;
            employeeResults.replaceChildren();
            return;
        }

        searchSequence += 1;
        var requestSequence = searchSequence;
        fetch(root.dataset.employeeSearchUrl + "?q=" + encodeURIComponent(query), {
            headers: {"X-Requested-With": "XMLHttpRequest"}
        })
            .then(function (response) {
                return response.json().then(function (payload) {
                    if (!response.ok) throw new Error(payload.error || "Не удалось выполнить поиск.");
                    return payload;
                });
            })
            .then(function (payload) {
                if (requestSequence !== searchSequence) return;
                renderEmployeeResults(payload.results || []);
            })
            .catch(function (error) {
                if (requestSequence !== searchSequence) return;
                showFeedback(error.message || "Не удалось выполнить поиск.", true);
            });
    }

    function updateSettleButton() {
        if (!settleButton) return;
        var temporaryNeedsEnd = (
            assignmentType
            && assignmentType.value === "temporary"
            && (!assignmentEndInput || !assignmentEndInput.value)
        );
        settleButton.disabled = Boolean(
            !controlHeld
            || saving
            || !selectedEmployee
            || !selectedBed
            || !assignmentType
            || !assignmentType.value
            || temporaryNeedsEnd
        );
    }

    function configureRelocation(sourceBed, destinationBed) {
        if (
            !controlHeld
            || !sourceBed
            || !isTransferredBed(sourceBed)
            || sourceBed.dataset.occupied !== "true"
            || saving
        ) return;
        if (destinationBed && !isTransferredBed(destinationBed)) return;
        if (destinationBed) {
            closeUnsettledPanel(false);
            activeRoom = destinationBed.closest("[data-room-card]");
            if (selectedBed) {
                selectedBed.classList.remove("is-selected");
                selectedBed.setAttribute("aria-pressed", "false");
            }
            selectedBed = destinationBed;
            selectedBed.classList.add("is-selected");
            selectedBed.setAttribute("aria-pressed", "true");
        }
        relocationSourceBed = sourceBed;
        selectedEmployee = {
            id: Number(sourceBed.dataset.occupantId),
            full_name: sourceBed.dataset.occupantName,
            personnel_number: "Не указано",
            shift_label: sourceBed.dataset.shiftLabel,
            work_label: sourceBed.dataset.workLabel
        };
        if (!selectedEmployee.id) {
            showFeedback("Не удалось определить сотрудника текущего размещения.", true);
            resetRelocation();
            return;
        }
        if (selectedEmployeeName) selectedEmployeeName.textContent = selectedEmployee.full_name;
        if (selectedEmployeeMeta) selectedEmployeeMeta.textContent = employeeMeta(selectedEmployee);
        if (selectedEmployeeCard) selectedEmployeeCard.hidden = false;
        if (assignmentType) {
            assignmentType.value = sourceBed.dataset.assignmentType || "";
        }
        updateAssignmentEndVisibility();
        if (!destinationBed) {
            selectedBed.classList.remove("is-selected");
            selectedBed.setAttribute("aria-pressed", "false");
            selectedBed = null;
        }
        renderBedCards();
        updatePlacementPanel();
        updateSettleButton();
        if (destinationBed) {
            openRelocationModal();
        } else {
            panel.hidden = true;
            panel.setAttribute("aria-hidden", "true");
            panelBackdrop.hidden = true;
        }
    }

    function startRelocation() {
        if (!controlHeld || !selectedBed || selectedBed.dataset.occupied !== "true" || saving) return;
        configureRelocation(selectedBed, null);
    }

    function csrfToken() {
        var input = settlementForm.querySelector("input[name='csrfmiddlewaretoken']");
        if (input && input.value) return input.value;
        var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : "";
    }

    function stopHeartbeatTimer() {
        if (heartbeatTimer !== null) {
            window.clearTimeout(heartbeatTimer);
            heartbeatTimer = null;
        }
    }

    function syncControlInteractivity() {
        if (controlAcquireButton) {
            controlAcquireButton.disabled = Boolean(
                controlHeld
                || controlActionInFlight
                || heartbeatInFlight
                || heartbeatRecoveryPending
                || controlAccessDenied
            );
        }
        if (controlReleaseButton) {
            controlReleaseButton.disabled = Boolean(
                !controlHeld || controlActionInFlight || heartbeatInFlight
            );
        }
        if (relocateButton) relocateButton.disabled = Boolean(!controlHeld || saving);
        if (releaseButton) releaseButton.disabled = Boolean(!controlHeld || saving);
        updateSettleButton();
        root.querySelectorAll("[data-bed]").forEach(renderMapBed);
        if (!controlHeld) clearDragState();
        if (placementHint && settlementForm) updatePlacementPanel();
    }

    function setControlState(nextState, message) {
        var labels = {
            free: "Управление не начато",
            held: "Вы управляете расселением",
            busy: "Управление занято другим сотрудником",
            lost: "Связь с управлением потеряна"
        };
        controlState = labels[nextState] ? nextState : "lost";
        controlHeld = controlState === "held";
        root.dataset.controlState = controlState;
        root.classList.toggle("is-control-held", controlHeld);
        if (controlPanel) controlPanel.dataset.controlState = controlState;
        if (controlStatus) controlStatus.textContent = labels[controlState];
        if (controlMessage) controlMessage.textContent = message || "";
        syncControlInteractivity();
    }

    function controlRequestError(response, payload) {
        var error = new Error(
            payload && payload.error
                ? payload.error
                : "Не удалось подтвердить управление расселением."
        );
        error.controlCode = payload && payload.code ? payload.code : "";
        error.controlStatus = payload && payload.status ? payload.status : "";
        error.httpStatus = response.status;
        error.isControlled = Boolean(error.controlCode || response.status === 403);
        return error;
    }

    function postControl(url) {
        return fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "X-CSRFToken": csrfToken(),
                "X-Requested-With": "XMLHttpRequest"
            }
        }).then(function (response) {
            return response.json()
                .catch(function () { return {}; })
                .then(function (payload) {
                    if (!response.ok) throw controlRequestError(response, payload);
                    return payload;
                });
        });
    }

    function scheduleHeartbeat(expiresAt) {
        stopHeartbeatTimer();
        heartbeatRetryCount = 0;
        heartbeatRecoveryPending = false;
        var expiresAtMs = Date.parse(expiresAt || "");
        var remainingMs = Number.isFinite(expiresAtMs) ? expiresAtMs - Date.now() : 60000;
        var delay = Math.max(1000, Math.min(45000, Math.floor(remainingMs / 2)));
        heartbeatTimer = window.setTimeout(heartbeatControl, delay);
    }

    function applyControlledControlError(error) {
        stopHeartbeatTimer();
        heartbeatRetryCount = 0;
        heartbeatRecoveryPending = false;
        if (error.httpStatus === 403) {
            controlAccessDenied = true;
            setControlState("lost", "Текущая сессия больше не допускает управление расселением.");
            return;
        }
        controlAccessDenied = false;
        if (
            error.controlCode === "settlement.control.busy"
            || error.controlCode === "settlement.control.session_mismatch"
        ) {
            setControlState("busy", error.message);
            return;
        }
        setControlState("free", error.message);
    }

    function scheduleHeartbeatRetry() {
        stopHeartbeatTimer();
        controlHeld = false;
        if (heartbeatRetryCount >= heartbeatRetryDelays.length) {
            heartbeatRecoveryPending = false;
            setControlState(
                "lost",
                "Не удалось восстановить связь. Можно повторить подключение вручную."
            );
            return;
        }
        var delay = heartbeatRetryDelays[heartbeatRetryCount];
        heartbeatRetryCount += 1;
        heartbeatRecoveryPending = true;
        setControlState("lost", "Проверяем связь с управлением повторно…");
        heartbeatTimer = window.setTimeout(function () {
            heartbeatTimer = null;
            heartbeatRecoveryPending = false;
            heartbeatControl();
        }, delay);
    }

    function heartbeatControl() {
        if (heartbeatInFlight || controlActionInFlight) return;
        stopHeartbeatTimer();
        heartbeatInFlight = true;
        syncControlInteractivity();
        postControl(root.dataset.controlHeartbeatUrl)
            .then(function (payload) {
                controlAccessDenied = false;
                setControlState("held", "Управление подтверждено. Изменяющие действия доступны.");
                scheduleHeartbeat(payload.expires_at);
            })
            .catch(function (error) {
                if (error.isControlled) {
                    applyControlledControlError(error);
                    return;
                }
                scheduleHeartbeatRetry();
            })
            .finally(function () {
                heartbeatInFlight = false;
                syncControlInteractivity();
            });
    }

    function acquireControl() {
        if (controlHeld || controlActionInFlight || heartbeatInFlight || controlAccessDenied) return;
        stopHeartbeatTimer();
        controlActionInFlight = true;
        heartbeatRetryCount = 0;
        heartbeatRecoveryPending = false;
        syncControlInteractivity();
        postControl(root.dataset.controlAcquireUrl)
            .then(function (payload) {
                setControlState("held", "Управление получено. Изменяющие действия доступны.");
                scheduleHeartbeat(payload.expires_at);
            })
            .catch(function (error) {
                if (error.isControlled) {
                    applyControlledControlError(error);
                    return;
                }
                scheduleHeartbeatRetry();
            })
            .finally(function () {
                controlActionInFlight = false;
                syncControlInteractivity();
            });
    }

    function releaseControl() {
        if (!controlHeld || controlActionInFlight || heartbeatInFlight) return;
        stopHeartbeatTimer();
        controlActionInFlight = true;
        syncControlInteractivity();
        postControl(root.dataset.controlReleaseUrl)
            .then(function () {
                heartbeatRetryCount = 0;
                heartbeatRecoveryPending = false;
                setControlState("free", "Работа завершена. Карта доступна только для просмотра.");
            })
            .catch(function (error) {
                if (error.isControlled) {
                    applyControlledControlError(error);
                    return;
                }
                scheduleHeartbeatRetry();
            })
            .finally(function () {
                controlActionInFlight = false;
                syncControlInteractivity();
            });
    }

    function initializeControlLifecycle() {
        controlAccessDenied = false;
        heartbeatRecoveryPending = false;
        setControlState("free", "Проверяем состояние текущей сессии…");
        heartbeatControl();
    }

    function updateMapAfterSettlement(payload) {
        var occupancy = payload.occupancy;
        var movedEmployeeId = selectedEmployee && selectedEmployee.id;
        clearDragState();
        if (relocationSourceBed) {
            relocationSourceBed.dataset.occupied = "false";
            relocationSourceBed.dataset.occupantId = "";
            relocationSourceBed.dataset.occupantName = "";
            relocationSourceBed.dataset.occupantPhotoUrl = "";
            relocationSourceBed.dataset.shiftLabel = "";
            relocationSourceBed.dataset.workLabel = "";
            relocationSourceBed.dataset.assignmentTypeLabel = "";
            relocationSourceBed.dataset.assignmentType = "";
            relocationSourceBed.classList.remove("is-occupied");
            relocationSourceBed.classList.add("is-free");
            renderMapBed(relocationSourceBed);
        }
        selectedBed.dataset.occupied = "true";
        selectedBed.dataset.occupantId = String(movedEmployeeId || "");
        selectedBed.dataset.occupantName = occupancy.occupant_name;
        selectedBed.dataset.occupantPhotoUrl = occupancy.photo_url || "";
        selectedBed.dataset.shiftLabel = occupancy.shift_label;
        selectedBed.dataset.workLabel = occupancy.work_label;
        selectedBed.dataset.assignmentTypeLabel = occupancy.assignment_type_label;
        selectedBed.dataset.assignmentType = occupancy.assignment_type || "";
        selectedBed.classList.remove("is-free");
        selectedBed.classList.add("is-occupied");
        renderMapBed(selectedBed);
        selectedBed.setAttribute(
            "aria-label",
            [
                selectedBed.dataset.dormitoryLabel,
                selectedBed.dataset.floorLabel,
                selectedBed.dataset.roomLabel,
                "блок " + selectedBed.dataset.blockLabel,
                selectedBed.dataset.positionLabel,
                "занято",
                occupancy.occupant_name
            ].join(", ")
        );

        updateSelectionSummary();
        renderRoom(activeRoom);
    }

    function submitPlacement() {
        if (!controlHeld || !selectedBed || !selectedEmployee || !assignmentType.value || saving) return;
        saving = true;
        updateSettleButton();
        showFeedback("Сохраняем заселение…", false);

        fetch(root.dataset.occupancyCreateUrl, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrfToken(),
                "X-Requested-With": "XMLHttpRequest"
            },
            body: JSON.stringify({
                action: relocationSourceBed ? "relocate" : "settle",
                bed_stable_id: selectedBed.dataset.bedId,
                employee_id: selectedEmployee.id,
                assignment_type: assignmentType.value,
                ends_at: assignmentEndInput && assignmentEndInput.value
                    ? new Date(assignmentEndInput.value).toISOString()
                    : null
            })
        })
            .then(function (response) {
                return response.json().then(function (payload) {
                    if (!response.ok) throw new Error(payload.error || "Не удалось сохранить заселение.");
                    return payload;
                });
            })
            .then(function (payload) {
                updateMapAfterSettlement(payload);
                if (relocationSourceBed) {
                    closeRelocationModal(true);
                } else {
                    closePanel();
                }
            })
            .catch(function (error) {
                showFeedback(error.message || "Не удалось сохранить заселение.", true);
            })
            .finally(function () {
                saving = false;
                updateSettleButton();
            });
    }

    function confirmPlacement() {
        if (!controlHeld || !selectedBed || !selectedEmployee || !assignmentType.value || saving) return;
        var destination = [
            selectedBed.dataset.dormitoryLabel,
            selectedBed.dataset.floorLabel,
            selectedBed.dataset.roomLabel,
            "блок " + selectedBed.dataset.blockLabel,
            selectedBed.dataset.positionLabel
        ].join(", ");
        var relocating = Boolean(relocationSourceBed);
        var message = (relocating ? "Переселить " : "Заселить ")
            + selectedEmployee.full_name + " в " + destination + "?";
        if (typeof window.openAppConfirmDialog === "function") {
            window.openAppConfirmDialog(
                message,
                submitPlacement,
                0,
                relocating ? "Переселить" : "Заселить",
                {title: relocating ? "Подтвердите переселение" : "Подтвердите заселение"}
            );
            return;
        }
        if (window.confirm(message)) submitPlacement();
    }

    function submitRelease() {
        if (!controlHeld || !selectedBed || selectedBed.dataset.occupied !== "true" || saving) return;
        saving = true;
        if (releaseButton) releaseButton.disabled = true;
        if (relocateButton) relocateButton.disabled = true;
        showFeedback("Освобождаем койко-место…", false);
        fetch(root.dataset.occupancyCreateUrl, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrfToken(),
                "X-Requested-With": "XMLHttpRequest"
            },
            body: JSON.stringify({
                action: "release",
                bed_stable_id: selectedBed.dataset.bedId
            })
        })
            .then(function (response) {
                return response.json().then(function (payload) {
                    if (!response.ok) throw new Error(payload.error || "Не удалось освободить койко-место.");
                    return payload;
                });
            })
            .then(function () {
                showFeedback("Койко-место освобождено. Обновляем карту…", false);
                window.location.reload();
            })
            .catch(function (error) {
                showFeedback(error.message || "Не удалось освободить койко-место.", true);
            })
            .finally(function () {
                saving = false;
                syncControlInteractivity();
            });
    }

    function confirmRelease() {
        if (!controlHeld || !selectedBed || selectedBed.dataset.occupied !== "true" || saving) return;
        var message = "Освободить " + bedLabel(selectedBed) + " и завершить текущее размещение?";
        if (typeof window.openAppConfirmDialog === "function") {
            window.openAppConfirmDialog(
                message,
                submitRelease,
                1,
                "Освободить",
                {title: "Подтвердите освобождение"}
            );
            return;
        }
        if (window.confirm(message)) submitRelease();
    }

    [
        ["[data-dorm-filter]", "dormitory", "dormFilter"],
        ["[data-floor-filter]", "floor", "floorFilter"]
    ].forEach(function (definition) {
        var selector = definition[0];
        var stateKey = definition[1];
        var datasetKey = definition[2];
        var buttons = Array.from(root.querySelectorAll(selector));
        buttons.forEach(function (button) {
            button.addEventListener("click", function () {
                selectFilter(buttons, button, stateKey, button.dataset[datasetKey]);
            });
        });
    });

    if (searchInput) {
        searchInput.addEventListener("input", function () {
            state.search = searchInput.value.trim().toLowerCase();
            applyFilters();
        });
    }

    root.addEventListener("click", function (event) {
        if (event.target.closest("[data-bed-photo-slot], [data-bed-hover-card]")) return;
        var bed = event.target.closest("[data-bed]");
        if (bed && !isTransferredBed(bed)) return;
        if (bed) {
            openRoom(bed.closest("[data-room-card]"), bed);
            return;
        }
        var roomButton = event.target.closest("[data-room-open]");
        if (roomButton) openRoom(roomButton.closest("[data-room-card]"), null);
    });

    root.querySelectorAll("[data-bed]").forEach(function (bed) {
        renderMapBed(bed);
        if (isTransferredBed(bed) && !bed.disabled) {
            bed.setAttribute("aria-pressed", "false");
        }
    });

    var dragSourceBed = null;

    function clearDragState() {
        root.querySelectorAll(".is-drag-source, .is-drop-target").forEach(function (bed) {
            bed.classList.remove("is-drag-source", "is-drop-target");
        });
        dragSourceBed = null;
    }

    root.addEventListener("dragstart", function (event) {
        var handle = event.target.closest("[data-bed-drag-handle]");
        var bed = handle && handle.closest("[data-bed]");
        if (!controlHeld || !bed || !isTransferredBed(bed) || bed.dataset.occupied !== "true" || saving) {
            event.preventDefault();
            return;
        }
        dragSourceBed = bed;
        bed.classList.add("is-drag-source");
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", bed.dataset.bedId);
        event.dataTransfer.setData("application/x-settlement-bed", bed.dataset.bedId);
    });

    root.addEventListener("dragenter", function (event) {
        var bed = event.target.closest("[data-bed]");
        if (bed && !isTransferredBed(bed)) {
            bed.classList.remove("is-drop-target");
        }
    });

    root.addEventListener("dragover", function (event) {
        var bed = event.target.closest("[data-bed]");
        if (
            !controlHeld
            || !dragSourceBed
            || !bed
            || bed === dragSourceBed
            || !isTransferredBed(bed)
            || bed.disabled
            || bed.dataset.occupied === "true"
            || bed.closest("[data-floor-section]").hidden
        ) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
        root.querySelectorAll(".is-drop-target").forEach(function (candidate) {
            if (candidate !== bed) candidate.classList.remove("is-drop-target");
        });
        bed.classList.add("is-drop-target");
    });

    root.addEventListener("drop", function (event) {
        var targetBed = event.target.closest("[data-bed]");
        if (
            !controlHeld
            || !dragSourceBed
            || !targetBed
            || targetBed === dragSourceBed
            || !isTransferredBed(targetBed)
            || targetBed.disabled
            || targetBed.dataset.occupied === "true"
            || targetBed.closest("[data-floor-section]").hidden
        ) return;
        event.preventDefault();
        var sourceBed = dragSourceBed;
        clearDragState();
        configureRelocation(sourceBed, targetBed);
    });

    root.addEventListener("dragend", clearDragState);

    root.addEventListener("dblclick", function (event) {
        var handle = event.target.closest("[data-bed-photo-slot], [data-bed-hover-card]");
        var bed = handle && handle.closest("[data-bed]");
        if (!bed || bed.dataset.occupied !== "true") return;
        event.preventDefault();
        event.stopPropagation();
        openEmployeePanel(Number(bed.dataset.occupantId));
    });
    panelClose.addEventListener("click", closePanel);
    panelBackdrop.addEventListener("click", closePanel);
    if (employeePanelClose) employeePanelClose.addEventListener("click", closeEmployeePanel);
    if (employeePanelBackdrop) employeePanelBackdrop.addEventListener("click", closeEmployeePanel);
    if (relocationModalClose) relocationModalClose.addEventListener("click", closeRelocationModal);
    if (relocationModalCancel) relocationModalCancel.addEventListener("click", closeRelocationModal);
    if (relocationModalBackdrop) relocationModalBackdrop.addEventListener("click", closeRelocationModal);
    if (autoPreviewModalOpen) autoPreviewModalOpen.addEventListener("click", function () {
        setAutoPreviewModal(true);
    });
    if (autoPreviewModalClose) autoPreviewModalClose.addEventListener("click", function () {
        setAutoPreviewModal(false);
    });
    if (autoPreviewModalCancel) autoPreviewModalCancel.addEventListener("click", function () {
        setAutoPreviewModal(false);
    });
    if (autoPreviewForm) autoPreviewForm.addEventListener("submit", markAutoPreviewOpenOnce);
    if (autoPreviewModalBackdrop) autoPreviewModalBackdrop.addEventListener("click", function () {
        setAutoPreviewModal(false);
    });
    if (relocateButton) relocateButton.addEventListener("click", startRelocation);
    if (releaseButton) releaseButton.addEventListener("click", confirmRelease);
    if (controlAcquireButton) controlAcquireButton.addEventListener("click", acquireControl);
    if (controlReleaseButton) controlReleaseButton.addEventListener("click", releaseControl);
    unsettledPanelToggles.forEach(function (toggle) {
        toggle.addEventListener("click", toggleUnsettledPanel);
    });
    if (unsettledPanelClose) {
        unsettledPanelClose.addEventListener("click", function () {
            closeUnsettledPanel(true);
        });
    }
    if (unsettledSearch) {
        unsettledSearch.addEventListener("input", renderUnsettledEmployees);
    }
    unsettledShiftButtons.forEach(function (button) {
        button.addEventListener("click", function () {
            unsettledShift = button.dataset.unsettledShiftFilter;
            unsettledShiftButtons.forEach(function (candidate) {
                var active = candidate === button;
                candidate.classList.toggle("is-active", active);
                candidate.setAttribute("aria-pressed", String(active));
            });
            renderUnsettledEmployees();
        });
    });
    document.addEventListener("keydown", function (event) {
        if (event.key !== "Escape") return;
        if (unsettledPanel && !unsettledPanel.hidden) {
            closeUnsettledPanel(true);
        } else if (autoPreviewModal && !autoPreviewModal.hidden) {
            setAutoPreviewModal(false);
        } else if (relocationModal && !relocationModal.hidden) {
            closeRelocationModal();
        } else if (!panel.hidden) {
            closePanel();
        }
    });
    employeeSearch.addEventListener("input", function () {
        window.clearTimeout(searchTimer);
        searchTimer = window.setTimeout(runEmployeeSearch, 220);
    });
    assignmentType.addEventListener("change", function () {
        updateAssignmentEndVisibility();
        updateSettleButton();
    });
    if (assignmentEndInput) {
        assignmentEndInput.addEventListener("input", updateSettleButton);
    }
    settlementForm.addEventListener("submit", function (event) {
        event.preventDefault();
        if (relocationSourceBed) {
            submitPlacement();
            return;
        }
        confirmPlacement();
    });

    restoreMapSelection();
    applyFilters();
    initializeControlLifecycle();
    if (consumeAutoPreviewOpenOnce()) {
        setAutoPreviewModal(true);
    }
}());
