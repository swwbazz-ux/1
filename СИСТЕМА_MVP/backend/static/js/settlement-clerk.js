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
    var watchExtensionDetails = root.querySelector("[data-watch-extensions]");
    var watchExtensionStateNode = root.querySelector("#settlement-watch-extension-state");
    var watchExtensionList = root.querySelector("[data-watch-extension-list]");
    var watchExtensionCount = root.querySelector("[data-watch-extension-count]");
    var watchExtensionEmpty = root.querySelector("[data-watch-extension-empty]");
    var selectedRoomCount = root.querySelector("[data-selected-room-count]");
    var selectedTransferredRoomCount = root.querySelector("[data-selected-transferred-room-count]");
    var globalFreeCount = root.querySelector("[data-free-bed-count]");
    var globalOccupiedCount = root.querySelector("[data-occupied-bed-count]");
    var selectedUnavailableBedCount = root.querySelector("[data-selected-unavailable-bed-count]");
    var searchInput = root.querySelector("[data-settlement-search]");
    var residentFinder = root.querySelector("#settlement-resident-finder");
    var quickSearchOrigin = null;
    var renderRoomOptions = bindSearchList(searchInput, root.querySelector("[data-room-popup]"), refreshRoomOptions, showFoundRoom);
    var renderResidentOptions = bindSearchList(residentFinder, root.querySelector("[data-resident-popup]"), refreshResidentSearch, showResidentBed);
    var residentMatches = [];
    var chosenRoomId = "";
    var chosenResidentId = "";
    var panel = root.querySelector("[data-room-panel]");
    var panelBackdrop = root.querySelector("[data-room-panel-backdrop]");
    var panelClose = root.querySelector("[data-room-panel-close]");
    var panelKind = panel.querySelector("[data-panel-kind]");
    var panelRoomLink = panel.querySelector("[data-panel-room-link]");
    var panelEmployeeLink = panel.querySelector("[data-panel-employee-link]");
    var panelTrigger = null;
    var avatarClickTimer = null;
    var employeePanel = root.querySelector("[data-employee-panel]");
    var employeePanelBackdrop = root.querySelector("[data-employee-panel-backdrop]");
    var employeePanelClose = root.querySelector("[data-employee-panel-close]");
    var employeePanelTitle = root.querySelector("[data-employee-panel-title]");
    var employeePanelBody = root.querySelector("[data-employee-panel-body]");
    var panelTitle = root.querySelector("[data-room-panel-title]");
    var panelLocation = root.querySelector("[data-room-panel-location]");
    var panelStatus = root.querySelector("[data-room-panel-status]");
    var roomTransferToggle = root.querySelector("[data-room-transfer-toggle]");
    var roomTransferMessage = root.querySelector("[data-room-transfer-message]");
    var panelOccupiedCount = root.querySelector("[data-room-occupied-count]");
    var panelFreeCount = root.querySelector("[data-room-free-count]");
    var panelFreeLabel = root.querySelector("[data-room-free-label]");
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
    var autoLoading = root.querySelector("[data-auto-loading]");
    var autoFeedback = root.querySelector("[data-auto-feedback]");
    var autoEmpty = root.querySelector("[data-auto-empty]");
    var autoContent = root.querySelector("[data-auto-content]");
    var autoCohort = root.querySelector("[data-auto-cohort]");
    var autoStage = root.querySelector("[data-auto-stage]");
    var autoResult = root.querySelector("[data-auto-result]");
    var autoCalculateButton = root.querySelector("[data-auto-calculate]");
    var autoConfirmButton = root.querySelector("[data-auto-confirm]");
    var autoShiftActions = root.querySelector("[data-auto-shift-actions]");
    var autoShiftButtons = Array.from(root.querySelectorAll("[data-auto-apply-shift]"));
    var mapModeButtons = Array.from(root.querySelectorAll("[data-map-mode]"));
    var mapModePanels = Array.from(root.querySelectorAll("[data-map-mode-panel]"));
    var planMap = root.querySelector("[data-plan-map]");
    var planStatus = root.querySelector("[data-plan-status]");
    var planLists = root.querySelector("[data-plan-lists]");
    var planUnresolved = root.querySelector("[data-plan-unresolved]");
    var planExcluded = root.querySelector("[data-plan-excluded]");
    var planHistory = root.querySelector("[data-plan-history]");
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
    var swapDialog = root.querySelector("[data-swap-dialog]");
    var swapSelection = root.querySelector("[data-swap-selection]");
    var swapNotice = root.querySelector("[data-swap-notice]");
    var swapError = swapDialog.querySelector("[data-swap-error]");
    var swapConfirm = swapDialog.querySelector("[data-swap-confirm]");
    var swapSource = null;
    var swapTarget = null;
    var activeRoom = null;
    var selectedBed = null;
    var selectedEmployee = null;
    var relocationSourceBed = null;
    var searchTimer = null;
    var searchSequence = 0;
    var saving = false;
    var roomTransferPending = false;
    var unsettledShift = "all";
    var unsettledPanelTrigger = null;
    var autoPreviewOpenOnceKey = "settlement:auto-preview-open-once";
    var autoStatePayload = null;
    var autoMutationInFlight = false;
    var autoReadSequence = 0;
    var activeMapMode = "current";
    var watchExtensionState = null;
    var watchExtensionClockOffset = 0;
    var watchExtensionTimer = null;
    var watchExtensionInFlight = false;
    var planDragSource = null;
    var controlHeld = false;
    var controlState = "free";
    var controlActionInFlight = false;
    var heartbeatInFlight = false;
    var heartbeatTimer = null;
    var heartbeatRetryCount = 0;
    var heartbeatRecoveryPending = false;
    var controlAccessDenied = false;
    var heartbeatRetryDelays = [2500, 6000];
    var rosterPlacement = root.querySelector("[data-roster-placement]");
    var checkinStart = root.querySelector("[data-checkin-start]");
    var checkin = window.createSettlementBedCheckin && window.createSettlementBedCheckin(
        root.querySelector("[data-bed-checkin]"), updateMapAfterSettlement
    );

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
        if (!label || label === "Не указано") return "?";
        if (label === "День") return "Д";
        if (label === "Ночь") return "Н";
        return "?";
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

        var name = document.createElement("strong");
        name.textContent = bed.dataset.occupantName || "";
        var shift = document.createElement("strong");
        shift.textContent = "Смена: " + (String(bed.dataset.tooltipShiftLabel || bed.dataset.shiftLabel || "").trim() || "Не указано");
        hoverCard.replaceChildren(name);
        if (bed.dataset.tooltipWorkLabel) {
            var work = document.createElement("span");
            work.textContent = bed.dataset.tooltipWorkLabel;
            hoverCard.appendChild(work);
        }
        hoverCard.appendChild(shift);
        if (bed.dataset.shiftConflict) hoverCard.appendChild(element("strong", "", bed.dataset.shiftConflict));
        if (bed.dataset.assignmentWarning) hoverCard.appendChild(element("strong", "", bed.dataset.assignmentWarning));
        if (bed.dataset.onExtension === "true") {
            var extension = document.createElement("span");
            extension.className = "settlement-bed-extension-label";
            extension.textContent = "На продлении";
            hoverCard.appendChild(extension);
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

    function syncBedDragHandle(bed) {
        var dragHandle = bed.querySelector("[data-bed-photo-slot]");
        if (!dragHandle) return;
        if (controlHeld && bed.dataset.occupied === "true" && isTransferredBed(bed) && !bed.disabled) {
            dragHandle.setAttribute("data-bed-drag-handle", "");
            dragHandle.setAttribute("draggable", "true");
        } else {
            dragHandle.removeAttribute("data-bed-drag-handle");
            dragHandle.removeAttribute("draggable");
        }
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
        var label = [
            bed.dataset.dormitoryLabel,
            bed.dataset.floorLabel,
            bed.dataset.roomLabel,
            "блок " + bed.dataset.blockLabel,
            bed.dataset.positionLabel
        ];
        if (occupied) {
            label.push("занято", bed.dataset.occupantName, "смена: " + (bed.dataset.shiftLabel || "Не указано"));
            if (bed.dataset.shiftConflict) label.push(bed.dataset.shiftConflict);
            if (bed.dataset.assignmentWarning) label.push(bed.dataset.assignmentWarning);
            if (bed.dataset.onExtension === "true") label.push("на продлении");
        } else {
            label.push(unavailable ? "недоступно" : "свободно");
        }
        bed.setAttribute("aria-label", label.join(", "));
        bed.dataset.bedState = unavailable ? "unavailable" : (occupied ? "occupied" : "free");
        bed.classList.toggle("is-occupied", occupied);
        bed.classList.toggle("is-free", !occupied);
        syncBedDragHandle(bed);
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
        var searchMatch = /^\d+$/.test(state.search)
            ? Number(room.dataset.roomNumber) === Number(state.search)
            : String(room.dataset.search || "").includes(state.search);
        return dormitoryMatch && floorMatch && searchMatch && (!chosenRoomId || room.dataset.roomId === chosenRoomId);
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
        var floor = floorSections.find(function (section) { return !section.hidden; });
        var floorTransferredCount = floor && floor.querySelector("[data-floor-transferred-count]");
        if (floorTransferredCount) floorTransferredCount.textContent = String(transferredRooms.length);
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

        highlightRooms();
        root.querySelectorAll("[data-plan-room]").forEach(function (room) {
            room.hidden = (
                room.dataset.dormitory !== state.dormitory
                || room.dataset.floor !== state.floor
            );
        });
        if (activeRoom && !selectedFloorRooms().includes(activeRoom)) closePanel();
        updateSelectionSummary();
    }

    function highlightRooms() {
        rooms.forEach(function (room) {
            var match = matchesRoom(room);
            var selectedFloor = room.dataset.dormitory === state.dormitory && room.dataset.floor === state.floor;
            room.classList.toggle("is-room-match", Boolean(state.search) && match);
            room.classList.toggle("is-filter-muted", selectedFloor && !match);
            room.setAttribute("data-filter-match", String(match));
        });
    }

    function bindSearchList(input, popup, refresh, choose) {
        if (!input || !popup) return function () {};
        var list = popup.querySelector('[role="listbox"]');
        var empty = popup.querySelector('[role="status"]');
        var matches = [];
        var active = -1;
        function close() {
            popup.hidden = true;
            input.setAttribute("aria-expanded", "false");
            input.removeAttribute("aria-activedescendant");
            active = -1;
        }
        input.addEventListener("focus", refresh);
        input.addEventListener("click", function () {
            quickSearchOrigin = null;
            if (popup.hidden && input.value) refresh();
        });
        input.addEventListener("blur", function () {
            quickSearchOrigin = null;
            close();
        });
        input.addEventListener("keydown", function (event) {
            if (event.key === "Escape" && !popup.hidden) {
                event.preventDefault();
                close();
                if (quickSearchOrigin) {
                    var origin = quickSearchOrigin;
                    input.blur();
                    origin.focus({preventScroll: true});
                }
            } else if (event.key === "Enter" && !popup.hidden && matches.length) {
                event.preventDefault();
                choose(matches[Math.max(active, 0)]);
            } else if ((event.key === "ArrowDown" || event.key === "ArrowUp") && input.value.trim()) {
                if (popup.hidden) refresh();
                if (!matches.length) return;
                event.preventDefault();
                if (active >= 0) list.children[active].setAttribute("aria-selected", "false");
                if (event.key === "ArrowDown") active = (active + 1) % matches.length;
                else active = active <= 0 ? matches.length - 1 : active - 1;
                var option = list.children[active];
                option.setAttribute("aria-selected", "true");
                input.setAttribute("aria-activedescendant", option.id);
                option.scrollIntoView({block: "nearest"});
            }
        });
        list.addEventListener("pointerdown", function (event) {
            if (event.target.closest('[role="option"]')) event.preventDefault();
        });
        list.addEventListener("click", function (event) {
            var option = event.target.closest("[data-match-index]");
            if (option) choose(matches[Number(option.dataset.matchIndex)]);
        });
        return function render(items, describe) {
            close();
            matches = items;
            list.replaceChildren();
            if (!input.value.trim() || document.activeElement !== input) return;
            matches.forEach(function (match, index) {
                var option = element("div");
                option.id = list.id + "-" + index;
                option.dataset.matchIndex = String(index);
                option.setAttribute("role", "option");
                option.setAttribute("aria-selected", "false");
                var labels = describe(match);
                option.append(element("strong", "", labels[0]), element("small", "", labels[1]));
                list.appendChild(option);
            });
            empty.hidden = matches.length > 0;
            popup.hidden = false;
            popup.scrollTop = 0;
            input.setAttribute("aria-expanded", "true");
        };
    }

    function refreshRoomOptions() {
        var query = state.search;
        var numeric = /^\d+$/.test(query);
        var matches = query ? rooms.filter(function (room) {
            return numeric ? room.dataset.roomNumber.startsWith(query) : String(room.dataset.search || "").includes(query);
        }) : [];
        matches.sort(function (left, right) {
            return Number(left.dataset.roomNumber) - Number(right.dataset.roomNumber)
                || Number(left.dataset.dormitory) - Number(right.dataset.dormitory)
                || Number(left.dataset.floor) - Number(right.dataset.floor);
        });
        renderRoomOptions(matches, function (room) {
            return ["Комната " + room.dataset.roomNumber, "КИС-" + room.dataset.dormitory + " · " + room.dataset.floor + " этаж"];
        });
    }

    function showFoundRoom(room) {
        if (!room.isConnected) {
            var id = room.dataset.roomId;
            room = rooms.find(function (current) { return current.dataset.roomId === id; });
            if (!room) return refreshRoomOptions();
        }
        chosenRoomId = room.dataset.roomId;
        state.search = room.dataset.roomNumber;
        searchInput.value = state.search;
        showMapFloor(room);
        var header = room.querySelector("[data-room-open]");
        header.focus({preventScroll: true});
        room.scrollIntoView({block: "nearest", inline: "nearest"});
    }

    function residentSearchName(value) {
        return normalizeSearch(value).replace(/ё/g, "е").replace(/\s+/g, " ");
    }

    function rankedResidentMatches(query) {
        if (!query) return [];
        var tokens = query.split(" ");
        var matches = [];
        root.querySelectorAll('[data-room-card] [data-occupied="true"]').forEach(function (bed) {
            var name = residentSearchName(bed.dataset.occupantName);
            if (!tokens.every(function (token) { return name.includes(token); })) return;
            var surname = name.split(" ", 1)[0];
            var rank = 3;
            if (name === query) rank = 0;
            else if (surname === tokens[0]) rank = 1;
            else if (surname.startsWith(tokens[0])) rank = 2;
            matches.push({bed: bed, name: name, rank: rank});
        });
        return matches.sort(function (left, right) {
            return left.rank - right.rank || left.name.localeCompare(right.name, "ru");
        });
    }

    function refreshResidentSearch() {
        if (!residentFinder) return;
        var query = residentSearchName(residentFinder.value);
        residentMatches.forEach(function (match) { match.bed.classList.remove("is-resident-match"); });
        residentMatches = rankedResidentMatches(query).filter(function (match) {
            return !swapSource || canSwapWith(match.bed);
        });
        residentMatches.forEach(function (match) {
            var highlighted = chosenResidentId
                ? match.bed.dataset.residentId === chosenResidentId
                : match.rank === residentMatches[0].rank;
            match.bed.classList.toggle("is-resident-match", highlighted);
        });
        renderResidentOptions(residentMatches, function (match) {
            var bed = match.bed;
            var location = [bed.dataset.dormitoryLabel, bed.dataset.floorLabel, bed.dataset.roomLabel,
                "блок " + bed.dataset.blockLabel, bed.dataset.positionLabel].join(" · ");
            return [bed.dataset.occupantName, location];
        });
    }

    function showMapFloor(room) {
        state.dormitory = room.dataset.dormitory;
        state.floor = room.dataset.floor;
        syncMapSelectionButtons();
        persistMapSelection();
        applyFilters();
    }

    function showResidentBed(match) {
        var bed = match.bed;
        if (swapSource) {
            openSwapConfirmation(bed);
            return;
        }
        showMapFloor(bed.closest("[data-room-card]"));
        chosenResidentId = bed.dataset.residentId;
        residentFinder.value = bed.dataset.occupantName;
        residentFinder.blur();
        refreshResidentSearch();
        bed.focus({preventScroll: true});
        bed.scrollIntoView({block: "nearest", inline: "nearest"});
    }

    function element(tagName, className, text) {
        var node = document.createElement(tagName);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function renderWatchExtensions(entries) {
        var markedOccupancies = new Set(entries.map(function (entry) { return String(entry.occupancy_id); }));
        rooms.forEach(function (room) {
            room.querySelectorAll('[data-occupied="true"]').forEach(function (bed) {
                var onExtension = markedOccupancies.has(bed.dataset.occupancyId);
                if (onExtension === (bed.dataset.onExtension === "true")) return;
                bed.dataset.onExtension = String(onExtension);
                renderMapBed(bed);
            });
        });

        var items = entries.map(function (entry) {
            var item = element("li");
            item.append(
                element("strong", "", entry.name),
                element("small", "", entry.location + " · по " + entry.ends_on)
            );
            return item;
        });
        watchExtensionList.replaceChildren(...items);
        watchExtensionCount.textContent = String(items.length);
        watchExtensionEmpty.hidden = items.length > 0;
    }

    function updateWatchExtensions() {
        window.clearTimeout(watchExtensionTimer);
        if (!watchExtensionState || document.hidden) return;
        var now = Date.now() + watchExtensionClockOffset;
        renderWatchExtensions(watchExtensionState.entries.filter(function (entry) {
            return Date.parse(entry.expires_at) > now;
        }));
        var refreshAt = Date.parse(watchExtensionState.refresh_at);
        if (refreshAt <= now) {
            refreshWatchExtensions();
            refreshAt = now + 60000;
        }
        watchExtensionTimer = window.setTimeout(updateWatchExtensions, refreshAt - now);
    }

    function acceptWatchExtensions(payload, requestedAt) {
        var serverTime = Date.parse(payload.server_time);
        if (!Number.isFinite(serverTime) || !Number.isFinite(Date.parse(payload.refresh_at)) || !Array.isArray(payload.entries)) return;
        watchExtensionClockOffset = serverTime - (requestedAt + Date.now()) / 2;
        watchExtensionState = payload;
        updateWatchExtensions();
    }

    async function refreshWatchExtensions() {
        if (!watchExtensionState || document.hidden || watchExtensionInFlight) return;
        watchExtensionInFlight = true;
        var requestedAt = Date.now();
        try {
            var response = await fetch(root.dataset.watchExtensionsUrl, {cache: "no-store"});
            if (response.ok && !response.redirected) {
                acceptWatchExtensions(await response.json(), requestedAt);
            }
        } catch (error) {
            // The deadlines already received from the server still apply offline.
        } finally {
            watchExtensionInFlight = false;
        }
    }

    function resumeWatchExtensions() {
        updateWatchExtensions();
        refreshWatchExtensions();
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
        var beds = selectedBed ? [selectedBed] : activeRoom.querySelectorAll("[data-bed]");
        beds.forEach(function (bed) {
            var occupied = bed.dataset.occupied === "true";
            var available = isTransferredBed(bed);
            if (selectedBed && !occupied) return;
            var card = element(
                selectedBed ? "article" : "button",
                "settlement-room-bed-card " + (occupied ? "is-occupied" : available ? "is-free" : "")
            );
            card.dataset.panelBedId = bed.dataset.bedId;
            if (!selectedBed) {
                card.type = "button";
                var main = element("span", "settlement-room-bed-card-main");
                main.appendChild(element("strong", "", bedLabel(bed)));
                main.appendChild(element("span", "", occupied ? "Занято" : available ? "Свободно" : "Недоступно"));
                card.appendChild(main);
                card.addEventListener("click", function () { openRoom(activeRoom, bed); });
            }
            if (occupied) {
                var details = document.createElement("dl");
                addBedDetail(details, "Жилец", bed.dataset.occupantName);
                addBedDetail(details, "День / ночь", bed.dataset.tooltipShiftLabel || bed.dataset.shiftLabel);
                if (bed.dataset.shiftConflict) addBedDetail(details, "Конфликт смен", bed.dataset.shiftConflict);
                addBedDetail(details, bed.dataset.workCaption || "Техника / должность", bed.dataset.workLabel);
                if (bed.dataset.assignmentWarning) addBedDetail(details, "Требует внимания", bed.dataset.assignmentWarning);
                addBedDetail(details, "Закрепление", bed.dataset.assignmentTypeLabel);
                if (bed.dataset.stayStart) {
                    addBedDetail(details, "Заселение на койку", bed.dataset.stayStart);
                    addBedDetail(details, "Выезд с койки", bed.dataset.stayEnd || "Дата не указана");
                }
                card.appendChild(details);
            }
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

    function swapParticipant(bed) {
        return {
            bed: bed,
            occupancyId: Number(bed.dataset.occupancyId),
            bedId: bed.dataset.bedId,
            name: bed.dataset.occupantName,
            address: bedLocation(bed)
        };
    }

    function canSwapWith(bed) {
        return swapSource && bed !== swapSource.bed && isTransferredBed(bed)
            && !bed.disabled && bed.dataset.occupied === "true" && Number(bed.dataset.occupancyId);
    }

    function startBedSwap(bed, destination) {
        if (!controlHeld || saving || !bed || !isTransferredBed(bed) || !Number(bed.dataset.occupancyId)) return;
        swapSource = swapParticipant(bed);
        swapNotice.hidden = true;
        closePanel();
        if (destination) {
            openSwapConfirmation(destination);
            return;
        }
        swapSelection.querySelector("[data-swap-selection-label]").textContent =
            "Выберите, с кем поменять жильца " + swapSource.name;
        swapSelection.hidden = false;
        chosenResidentId = "";
        residentFinder.value = "";
        residentFinder.focus();
        refreshResidentSearch();
    }

    function openSwapConfirmation(bed) {
        if (saving || !controlHeld || swapDialog.open || !canSwapWith(bed)) return;
        swapTarget = swapParticipant(bed);
        var entries = [[swapSource, swapTarget], [swapTarget, swapSource]].map(function (pair) {
            var item = element("li", "");
            item.append(
                element("strong", "", pair[0].name),
                element("span", "", "Сейчас: " + pair[0].address),
                element("span", "", "После обмена: " + pair[1].address)
            );
            return item;
        });
        swapDialog.querySelector("[data-swap-addresses]").replaceChildren(...entries);
        swapError.hidden = true;
        swapDialog.showModal();
    }

    function cancelBedSwap() {
        if (saving) return;
        swapSource = swapTarget = null;
        swapSelection.hidden = true;
        if (swapDialog.open) swapDialog.close();
        refreshResidentSearch();
    }

    async function submitBedSwap() {
        if (!swapSource || !swapTarget || saving) return;
        if (!controlHeld) {
            swapError.textContent = "Управление завершено. Начните работу заново.";
            swapError.hidden = false;
            return;
        }
        saving = swapConfirm.disabled = true;
        swapError.hidden = true;
        try {
            var response = await fetch(root.dataset.occupancyCreateUrl, {
                method: "POST",
                credentials: "same-origin",
                headers: {"Content-Type": "application/json", "X-CSRFToken": csrfToken()},
                body: JSON.stringify({
                    action: "swap",
                    first_occupancy_id: swapSource.occupancyId, first_bed_id: swapSource.bedId,
                    second_occupancy_id: swapTarget.occupancyId, second_bed_id: swapTarget.bedId
                })
            });
            var body = await response.json();
            if (!response.ok || !body.ok) throw new Error(body.error || "Не удалось выполнить обмен.");
            updateBedOccupancy(swapTarget.bed, body.placements[0].occupancy);
            updateBedOccupancy(swapSource.bed, body.placements[1].occupancy);
            residentFinder.value = "";
            updateShiftConflicts();
            updateSelectionSummary();
            refreshWatchExtensions();
            swapNotice.textContent = "Жильцы поменялись местами.";
            swapNotice.hidden = false;
            swapDialog.close();
            swapSource = swapTarget = null;
            swapSelection.hidden = true;
            refreshResidentSearch();
        } catch (error) {
            swapError.textContent = error.message || "Не удалось выполнить обмен.";
            swapError.hidden = false;
        } finally {
            saving = swapConfirm.disabled = false;
        }
    }

    root.querySelector("[data-swap-start]").addEventListener("click", function () { startBedSwap(selectedBed); });
    root.querySelector("[data-swap-selection-cancel]").addEventListener("click", cancelBedSwap);
    swapDialog.querySelector("[data-swap-cancel]").addEventListener("click", cancelBedSwap);
    swapDialog.addEventListener("cancel", function (event) {
        event.preventDefault();
        cancelBedSwap();
    });
    swapConfirm.addEventListener("click", submitBedSwap);

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
        var temporary = !relocationSourceBed && assignmentType.value === "temporary";
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
        var freeBed = selectedBed && isTransferredBed(selectedBed) && selectedBed.dataset.occupied !== "true";
        if (checkin) checkin.select(controlHeld && freeBed && !relocationSourceBed ? selectedBed : null);
        syncCheckinStart();
        if (rosterPlacement && relocationSourceBed) rosterPlacement.open = true;
        updateRelocationDetails();
        showFeedback("", false);
        settlementForm.hidden = true;
        placementHint.hidden = false;
        if (occupiedActions) occupiedActions.hidden = true;
        updateEmployeeSearchAvailability();

        if (activeRoom && activeRoom.dataset.transferStatus !== "transferred") {
            if (placementTitle) placementTitle.textContent = "Заселение сотрудника";
            placementHint.textContent = "Комната не передана. Заселение в неё недоступно.";
            return;
        }
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
        if (employeeSearch && !relocationSourceBed && (!rosterPlacement || rosterPlacement.open)) employeeSearch.focus();
    }

    function selectBed(bed) {
        var changed = selectedBed !== bed;
        if (selectedBed) {
            selectedBed.classList.remove("is-selected");
            selectedBed.setAttribute("aria-pressed", "false");
        }
        selectedBed = bed;
        if (bed) {
            bed.classList.add("is-selected");
            bed.setAttribute("aria-pressed", "true");
        }
        if (changed && rosterPlacement && !relocationSourceBed) rosterPlacement.open = false;
        if (changed && !relocationSourceBed) resetEmployeeSelection();
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
        var transferred = room.dataset.transferStatus === "transferred";
        room.dataset.occupiedBeds = String(counts.occupied);
        room.dataset.freeBeds = String(counts.free);
        var roomLabel = "Комната " + room.dataset.roomNumber;
        panel.dataset.panelView = selectedBed ? "bed" : "room";
        panelKind.textContent = selectedBed ? "Койко-место" : "Комната";
        panelTitle.textContent = selectedBed ? bedLabel(selectedBed) : roomLabel;
        panelRoomLink.hidden = !selectedBed;
        panelRoomLink.textContent = roomLabel;
        panelEmployeeLink.hidden = !selectedBed || !Number(selectedBed.dataset.occupantId);
        panelLocation.textContent = "КИС-" + room.dataset.dormitory + " · " + room.dataset.floor + " этаж";
        panelStatus.textContent = transferred ? "Передана" : "Не передана";
        if (roomTransferToggle) roomTransferToggle.setAttribute("aria-checked", String(transferred));
        panelOccupiedCount.textContent = String(counts.occupied);
        panelFreeCount.textContent = String(counts.free);
        if (panelFreeLabel) panelFreeLabel.textContent = transferred ? "свободно" : "недоступно";
        renderBedCards();
        updatePlacementPanel();
    }

    function openRoom(room, bed) {
        clearTimeout(avatarClickTimer);
        closeUnsettledPanel(false);
        if (activeRoom !== room || panel.hidden) showRoomTransferMessage("");
        if (panel.hidden) panelTrigger = bed || room.querySelector("[data-room-open]");
        activeRoom = room;
        selectBed(bed);
        panel.hidden = false;
        panel.setAttribute("aria-hidden", "false");
        panelBackdrop.hidden = false;
        renderRoom(room);
        updateSettleButton();
        panel.querySelector(".settlement-room-panel-body").scrollTop = 0;
        panelTitle.focus({preventScroll: true});
    }

    function showRoomTransferMessage(message, isError) {
        if (!roomTransferMessage) return;
        roomTransferMessage.hidden = !message;
        roomTransferMessage.textContent = message;
        roomTransferMessage.classList.toggle("is-error", Boolean(isError));
    }

    async function changeRoomTransfer() {
        if (!activeRoom || roomTransferPending || saving || relocationSourceBed) return;
        var room = activeRoom;
        var transferStatus = room.dataset.transferStatus === "transferred" ? "not_transferred" : "transferred";
        var failure = "Не удалось изменить статус комнаты. Повторите попытку.";
        roomTransferPending = true;
        roomTransferToggle.disabled = true;
        showRoomTransferMessage("Сохраняем статус комнаты…");
        try {
            var response = await fetch(root.dataset.roomTransferUrl, {
                method: "POST",
                credentials: "same-origin",
                headers: {"X-CSRFToken": csrfToken()},
                body: new URLSearchParams({room: room.dataset.roomId, status: transferStatus})
            });
            var payload = await response.json().catch(function () { return {}; });
            if (!response.ok) {
                if (activeRoom === room) showRoomTransferMessage(payload.error || failure, true);
                return;
            }
            var fragment = document.createElement("template");
            fragment.innerHTML = payload.room_card || "";
            var updatedRoom = fragment.content.firstElementChild;
            if (!updatedRoom || updatedRoom.dataset.roomId !== room.dataset.roomId) throw new Error(failure);
            rooms[rooms.indexOf(room)] = updatedRoom;
            room.replaceWith(updatedRoom);
            if (activeRoom === room) {
                activeRoom = updatedRoom;
                if (panelTrigger && room.contains(panelTrigger)) {
                    panelTrigger = panelTrigger.matches("[data-bed]")
                        ? updatedRoom.querySelector('[data-bed-id="' + CSS.escape(panelTrigger.dataset.bedId) + '"]')
                        : updatedRoom.querySelector("[data-room-open]");
                }
                if (selectedBed && room.contains(selectedBed)) {
                    selectBed(updatedRoom.querySelector('[data-bed-id="' + CSS.escape(selectedBed.dataset.bedId) + '"]'));
                }
                renderRoom(updatedRoom);
                showRoomTransferMessage("");
            }
            applyFilters();
        } catch (error) {
            if (activeRoom === room) showRoomTransferMessage(failure, true);
        } finally {
            roomTransferPending = false;
            roomTransferToggle.disabled = false;
        }
    }

    function closePanel() {
        clearTimeout(avatarClickTimer);
        if (!panel.hidden && panelTrigger && panelTrigger.isConnected) panelTrigger.focus({preventScroll: true});
        panelTrigger = null;
        selectBed(null);
        if (checkin) checkin.select(null);
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
        if (rosterPlacement && settlementForm) rosterPlacement.appendChild(settlementForm);
        else if (placementPanel && settlementForm) placementPanel.appendChild(settlementForm);
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

    var autoReasonLabels = {
        cohort_not_approved: "Состав заезда ещё не утверждён.",
        resolver_not_configured: "Для этого случая ещё не настроено правило размещения.",
        resident_inactive: "Карточка жильца неактивна.",
        incomplete_authoritative_context: "Не хватает подтверждённых исходных данных.",
        stale_calendar_relation: "Календарное основание устарело.",
        invalid_existing_binding: "Существующее закрепление противоречит текущим данным.",
        no_compatible_place: "Подходящее свободное место не найдено.",
        equal_priority_conflict: "Найдено несколько равноприоритетных вариантов.",
        hard_rule_conflict: "Размещение нарушает обязательное правило.",
        not_approved: "Состав заезда не утверждён.",
        invalid_state: "Операция недоступна в текущем состоянии.",
        stale_source: "Исходные данные изменились. Выполните расчёт заново.",
        concurrent_confirmation: "Результат уже изменён другой операцией.",
        incomplete_result: "Результат расчёта неполон.",
        stale_preview: "Подтверждённый результат устарел. Выполните расчёт заново.",
        incomplete_preview: "Подтверждённый результат неполон.",
        hard_conflict: "Фактическое расселение изменилось и конфликтует с результатом.",
        manual_replacement_confirmation_required: "Нужно отдельно подтвердить замену ручных изменений."
    };
    var autoSourceLabels = {
        binding: "Закрепление",
        equipment: "Техника",
        position: "Должность",
        residual: "Свободный остаток"
    };

    function autoShortCode(code) {
        var parts = String(code || "").split(".");
        return parts[parts.length - 1] || "unknown";
    }

    function autoSetFeedback(message, kind, code) {
        if (!autoFeedback) return;
        autoFeedback.hidden = !message;
        autoFeedback.className = "settlement-auto-feedback" + (kind ? " is-" + kind : "");
        autoFeedback.textContent = "";
        if (!message) return;
        autoFeedback.appendChild(element("strong", "", message));
    }

    function autoSyncButtons() {
        var preview = autoStatePayload && autoStatePayload.preview;
        var canMutate = controlHeld && !autoMutationInFlight;
        if (autoCalculateButton) {
            autoCalculateButton.disabled = !canMutate || !autoStatePayload || autoStatePayload.state === "no_cohort";
        }
        if (autoConfirmButton) {
            autoConfirmButton.hidden = !preview || preview.status !== "draft";
            autoConfirmButton.disabled = !canMutate;
        }
        autoShiftButtons.forEach(function (button) {
            var shiftState = preview && preview.shift_apply && preview.shift_apply[button.dataset.autoApplyShift];
            button.disabled = !canMutate || !shiftState || shiftState.status !== "ready";
        });
        syncPlanInteractivity();
    }

    function autoSummaryCard(label, value, className) {
        var card = element("article", className || "");
        card.appendChild(element("span", "", label));
        card.appendChild(element("strong", "", String(value)));
        return card;
    }

    function autoResidentLabel(resident) {
        var label = resident.name + " · " + resident.kind_label;
        if (resident.organization) label += " · " + resident.organization;
        return label;
    }

    function autoDateLabel(value) {
        if (!value) return "—";
        var parts = String(value).split("-");
        return parts.length === 3 ? parts[2] + "." + parts[1] + "." + parts[0] : value;
    }

    function setMapMode(mode) {
        activeMapMode = mode === "next" ? "next" : "current";
        if (watchExtensionDetails) watchExtensionDetails.hidden = activeMapMode === "next";
        mapModeButtons.forEach(function (button) {
            var active = button.dataset.mapMode === activeMapMode;
            button.classList.toggle("is-active", active);
            button.setAttribute("aria-pressed", String(active));
        });
        mapModePanels.forEach(function (section) {
            section.hidden = section.dataset.mapModePanel !== activeMapMode;
        });
        clearDragState();
        clearPlanDragState();
        if (activeMapMode === "next") {
            cancelBedSwap();
            loadAutoState(autoCohort && autoCohort.value, 0);
        }
    }

    function planResidentCard(row) {
        var card = element("article", "settlement-plan-resident");
        card.dataset.planResident = String(row.resident.id);
        card.dataset.planEditable = String(Boolean(row.editable));
        card.dataset.planDraggable = String(Boolean(row.editable && !row.excluded));
        card.dataset.planApplied = String(Boolean(row.shift_applied));
        card.dataset.planResidentName = row.resident.name;
        var heading = element("div", "settlement-plan-resident-heading");
        heading.appendChild(element("strong", "", row.resident.name));
        heading.appendChild(element("span", "settlement-plan-shift", row.work_shift_label));
        card.appendChild(heading);
        if (row.manually_changed) {
            card.appendChild(element("small", "settlement-plan-manual", "Изменено вручную"));
        }
        if (row.original_place) {
            card.appendChild(element("small", "", "Исходное место: " + row.original_place));
        }
        if (row.shift_applied) {
            card.appendChild(element("small", "settlement-plan-readonly", "Смена уже применена. Используйте переселение на карте текущей вахты."));
        }
        var actions = element("div", "settlement-plan-resident-actions");
        if (!row.shift_applied && !row.excluded) {
            var exclude = element("button", "", "Исключить из плана");
            exclude.type = "button";
            exclude.dataset.planExclude = String(row.resident.id);
            actions.appendChild(exclude);
        }
        if (row.can_restore) {
            var restore = element("button", "", "Вернуть исходное решение");
            restore.type = "button";
            restore.dataset.planRestore = String(row.resident.id);
            actions.appendChild(restore);
        }
        if (actions.children.length) card.appendChild(actions);
        return card;
    }

    function renderPlanList(container, title, rows, kind) {
        if (!container) return;
        container.replaceChildren();
        var section = element("section", "settlement-plan-side-list is-" + kind);
        section.appendChild(element("h3", "", title));
        if (!rows.length) {
            section.appendChild(element("p", "settlement-plan-list-empty", "Список пуст."));
        }
        rows.forEach(function (row) {
            var card = planResidentCard(row);
            card.appendChild(element("p", "settlement-plan-reason", row.reason));
            section.appendChild(card);
        });
        container.appendChild(section);
    }

    function renderPlanHistory(rows) {
        if (!planHistory) return;
        planHistory.replaceChildren();
        var section = element("section", "settlement-plan-history");
        section.appendChild(element("h3", "", "История точечных изменений"));
        if (!rows.length) {
            section.appendChild(element("p", "settlement-plan-list-empty", "Ручных изменений ещё нет."));
        }
        rows.forEach(function (row) {
            var item = element("article", "settlement-plan-history-item");
            item.appendChild(element("strong", "", row.action + " · " + row.resident));
            item.appendChild(element("span", "", row.work_shift));
            item.appendChild(element("span", "", row.previous_place + " → " + row.new_place));
            item.appendChild(element("small", "", row.actor + " · " + new Date(row.created_at).toLocaleString("ru-RU")));
            item.appendChild(element("p", "", "Причина: " + row.reason));
            section.appendChild(item);
        });
        planHistory.appendChild(section);
    }

    function renderPlanMap(preview) {
        if (!planMap) return;
        planMap.replaceChildren();
        var plan = preview && preview.effective_plan;
        if (planLists) planLists.hidden = !plan;
        if (!plan) {
            if (planStatus) {
                planStatus.textContent = preview && preview.status === "draft"
                    ? "Подтвердите рассчитанный план, чтобы открыть плановую карту."
                    : "Нет утверждённого состава заезда или подтверждённого плана.";
            }
            return;
        }
        if (planStatus) {
            planStatus.textContent = controlHeld
                ? "Переместить в плане: перетащите жильца на нужную койку и укажите причину."
                : "План доступен для просмотра. Начните работу, чтобы вносить изменения.";
        }
        var placementsByBed = {};
        plan.placements.forEach(function (row) {
            placementsByBed[String(row.target_physical_bed_id)] = row;
        });
        plan.rooms.forEach(function (room) {
            var roomCard = element("article", "settlement-plan-room");
            roomCard.dataset.planRoom = room.stable_id;
            roomCard.dataset.dormitory = room.dormitory;
            roomCard.dataset.floor = String(room.floor);
            roomCard.appendChild(element("h3", "", room.display));
            var bedList = element("div", "settlement-plan-beds");
            room.beds.forEach(function (bed) {
                var bedNode = element("div", "settlement-plan-bed");
                bedNode.dataset.planBed = bed.stable_id;
                bedNode.dataset.targetCalendarSlotId = String(bed.target_calendar_slot_id || "");
                bedNode.dataset.targetPhysicalBedId = String(bed.target_physical_bed_id || "");
                bedNode.classList.toggle("is-unavailable", !bed.target_calendar_slot_id);
                bedNode.appendChild(element("span", "settlement-plan-bed-label", "Койка " + bed.display));
                var placement = placementsByBed[String(bed.target_physical_bed_id)];
                if (placement) bedNode.appendChild(planResidentCard(placement));
                bedList.appendChild(bedNode);
            });
            roomCard.appendChild(bedList);
            planMap.appendChild(roomCard);
        });
        renderPlanList(planUnresolved, "Требуют проверки", plan.unresolved, "unresolved");
        renderPlanList(planExcluded, "Исключены из плана", plan.excluded, "excluded");
        renderPlanHistory(plan.history);
        syncPlanInteractivity();
        applyFilters();
    }

    function syncPlanInteractivity() {
        root.querySelectorAll("[data-plan-resident]").forEach(function (card) {
            var editable = card.dataset.planEditable === "true";
            var allowed = controlHeld && editable && !autoMutationInFlight;
            if (allowed && card.dataset.planDraggable === "true") {
                card.setAttribute("draggable", "true");
            } else {
                card.removeAttribute("draggable");
            }
            card.querySelectorAll("button").forEach(function (button) {
                button.disabled = !allowed;
            });
        });
        root.querySelectorAll("[data-plan-bed]").forEach(function (bed) {
            bed.classList.toggle(
                "is-drop-disabled",
                !controlHeld || autoMutationInFlight || !bed.dataset.targetCalendarSlotId
            );
        });
    }

    function clearPlanDragState() {
        root.querySelectorAll(".is-plan-drag-source, .is-plan-drop-target").forEach(function (node) {
            node.classList.remove("is-plan-drag-source", "is-plan-drop-target");
        });
        planDragSource = null;
    }

    function askPlanReason() {
        var value = window.prompt("Причина изменения");
        if (value === null) return null;
        value = String(value).trim();
        if (!value) {
            if (planStatus) planStatus.textContent = "Причина изменения обязательна.";
            return null;
        }
        return value;
    }

    function runPlanCorrection(url, payload) {
        if (!controlHeld || autoMutationInFlight) return;
        if (planStatus) planStatus.textContent = "Сохраняем точечное изменение…";
        runAutoMutation(url, payload)
            .then(function (body) {
                if (!body) return null;
                return loadAutoState(autoCohort && autoCohort.value, 0);
            })
            .catch(function (error) {
                if (planStatus) planStatus.textContent = error.message || "Изменение не сохранено.";
            });
    }

    function renderAutoShiftState(preview) {
        if (!autoShiftActions) return;
        autoShiftActions.hidden = !preview;
        ["night", "day"].forEach(function (workShift) {
            var state = preview && preview.shift_apply && preview.shift_apply[workShift];
            var card = root.querySelector('[data-auto-shift-card="' + workShift + '"]');
            var message = root.querySelector('[data-auto-shift-message="' + workShift + '"]');
            var counts = root.querySelector('[data-auto-shift-counts="' + workShift + '"]');
            if (!card || !message || !counts) return;
            card.className = state ? "is-" + state.status : "is-not-ready";
            counts.hidden = true;
            counts.textContent = "";
            if (!state || state.status === "not_ready") {
                message.textContent = "Сначала подтвердите актуальный результат.";
            } else if (state.status === "too_early") {
                message.textContent = "Можно применить не раньше " + autoDateLabel(state.allowed_date) + ".";
            } else if (state.status === "ready") {
                message.textContent = "Готово к применению с " + autoDateLabel(state.allowed_date) + ".";
            } else if (state.status === "applied") {
                message.textContent = "Применено " + new Date(state.applied_at).toLocaleString("ru-RU") + ".";
                counts.hidden = false;
                [["Создано", state.counts.created], ["Сохранено", state.counts.reused], ["Заменено автоматически", state.counts.replaced_auto], ["Заменено вручную", state.counts.replaced_manual], ["Без места", state.counts.unresolved]].forEach(function (row) {
                    counts.appendChild(autoSummaryCard(row[0], row[1]));
                });
            }
        });
    }

    function renderAutoPreview(preview) {
        if (!autoStage || !autoResult) return;
        autoStage.textContent = "";
        autoResult.textContent = "";
        if (!preview) {
            autoStage.appendChild(element("p", "settlement-auto-preview-empty", "Расчёт ещё не выполнен. Выберите состав и запустите расчёт."));
            renderAutoShiftState(null);
            autoSyncButtons();
            return;
        }
        var statusLabels = {draft: "Черновик", confirmed: "Подтверждён", superseded: "Заменён"};
        var heading = element("div", "settlement-auto-run-heading");
        var title = element("div", "");
        title.appendChild(element("strong", "", "Расчёт № " + preview.version));
        title.appendChild(element("span", "", "Создан " + new Date(preview.created_at).toLocaleString("ru-RU") + " · " + preview.created_by));
        heading.appendChild(title);
        heading.appendChild(element("span", "settlement-auto-status is-" + preview.status, statusLabels[preview.status] || preview.status));
        autoStage.appendChild(heading);
        var summary = element("section", "settlement-auto-preview-summary");
        summary.appendChild(autoSummaryCard("Участников", preview.member_count));
        summary.appendChild(autoSummaryCard("Размещено", preview.placement_count, "is-success"));
        summary.appendChild(autoSummaryCard("Без места", preview.unresolved_count, preview.unresolved_count ? "is-conflict" : ""));
        autoStage.appendChild(summary);
        if (preview.stale) {
            autoSetFeedback("Результат устарел. Выполните расчёт заново.", "warning", "settlement.preview.stale_source");
        }
        if (preview.placements.length) {
            var placed = element("section", "settlement-auto-list");
            placed.appendChild(element("h3", "", "Предлагаемое размещение"));
            preview.placements.forEach(function (row) {
                var item = element("article", "settlement-auto-row");
                var copy = element("div", "");
                copy.appendChild(element("strong", "", autoResidentLabel(row.resident)));
                copy.appendChild(element("span", "", row.room + " · койка " + row.bed));
                copy.appendChild(element("small", "", row.arrival_at + " — " + row.departure_at));
                item.appendChild(copy);
                item.appendChild(element("b", "", autoSourceLabels[row.source] || row.source));
                placed.appendChild(item);
            });
            autoResult.appendChild(placed);
        }
        if (preview.unresolved.length) {
            var unresolved = element("section", "settlement-auto-list is-unresolved");
            unresolved.appendChild(element("h3", "", "Пока без места"));
            preview.unresolved.forEach(function (row) {
                var item = element("article", "settlement-auto-row");
                var copy = element("div", "");
                copy.appendChild(element("strong", "", autoResidentLabel(row.resident)));
                copy.appendChild(element("span", "", row.reason || "Требуется уточнение данных."));
                item.appendChild(copy);
                unresolved.appendChild(item);
            });
            autoResult.appendChild(unresolved);
        }
        renderAutoShiftState(preview);
        renderPlanMap(preview);
        autoSyncButtons();
    }

    function renderAutoState(payload) {
        autoStatePayload = payload;
        if (autoLoading) autoLoading.hidden = true;
        if (autoEmpty) autoEmpty.hidden = payload.state !== "no_cohort";
        if (autoContent) autoContent.hidden = payload.state === "no_cohort";
        if (payload.state === "no_cohort") {
            renderPlanMap(null);
            autoSyncButtons();
            return;
        }
        if (autoCohort) {
            var selected = String(payload.selected_cohort_id || "");
            autoCohort.textContent = "";
            payload.cohorts.forEach(function (cohort) {
                var period = cohort.watch_period;
                var option = new Option(
                    period.name + " · " + period.starts_on + " — " + period.ends_on + " · v" + cohort.version + " · " + cohort.member_count + " чел.",
                    String(cohort.id),
                    false,
                    String(cohort.id) === selected
                );
                autoCohort.appendChild(option);
            });
        }
        renderAutoPreview(payload.preview);
    }

    function autoResponseError(response, payload) {
        var error = new Error(payload && payload.error ? payload.error : "Не удалось выполнить операцию авторасселения.");
        error.code = payload && payload.code ? payload.code : "";
        error.details = payload && payload.details ? payload.details : {};
        error.httpStatus = response.status;
        error.status = payload && payload.status ? payload.status : "";
        return error;
    }

    function loadAutoState(cohortId, retryCount) {
        if (!root.dataset.autoStateUrl) return Promise.resolve();
        var sequence = ++autoReadSequence;
        if (autoLoading) autoLoading.hidden = false;
        var url = root.dataset.autoStateUrl;
        if (cohortId) url += "?cohort_id=" + encodeURIComponent(cohortId);
        return fetch(url, {credentials: "same-origin", headers: {"X-Requested-With": "XMLHttpRequest"}})
            .then(function (response) {
                return response.json().then(function (payload) {
                    if (!response.ok || !payload.ok) throw autoResponseError(response, payload);
                    return payload;
                });
            })
            .then(function (payload) {
                if (sequence !== autoReadSequence) return;
                autoSetFeedback("", "", "");
                renderAutoState(payload);
            })
            .catch(function (error) {
                if (sequence !== autoReadSequence) return;
                if ((retryCount || 0) < 1 && !error.httpStatus) {
                    return loadAutoState(cohortId, (retryCount || 0) + 1);
                }
                if (autoLoading) autoLoading.hidden = true;
                autoSetFeedback(error.message || "Не удалось загрузить состояние авторасселения.", "error", error.code || "network_error");
            });
    }

    function runAutoMutation(url, payload) {
        if (autoMutationInFlight || !controlHeld) return Promise.resolve(null);
        autoMutationInFlight = true;
        autoSyncButtons();
        return fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: {"Content-Type": "application/json", "X-CSRFToken": csrfToken(), "X-Requested-With": "XMLHttpRequest"},
            body: JSON.stringify(payload)
        })
            .then(function (response) {
                return response.json().then(function (body) {
                    if (!response.ok || !body.ok) throw autoResponseError(response, body);
                    return body;
                });
            })
            .then(function (body) {
                if (body && body.preview && autoStatePayload) {
                    autoStatePayload.preview = body.preview;
                    renderAutoPreview(body.preview);
                }
                autoSetFeedback("Операция выполнена.", "success", "");
                return body;
            })
            .catch(function (error) {
                if (String(error.code).indexOf("settlement.control.") === 0) {
                    setControlState(error.status === "busy" ? "busy" : "lost", error.message);
                }
                autoSetFeedback(autoReasonLabels[autoShortCode(error.code)] || error.message, error.httpStatus === 409 ? "warning" : "error", error.code || "network_error");
                throw error;
            })
            .finally(function () {
                autoMutationInFlight = false;
                autoSyncButtons();
            });
    }

    function calculateAutoPreview() {
        if (!autoCohort || !autoCohort.value) return;
        autoSetFeedback("Выполняем расчёт…", "progress", "");
        runAutoMutation(root.dataset.autoCreateUrl, {cohort_id: Number(autoCohort.value)}).catch(function () {});
    }

    function confirmAutoPreview() {
        var preview = autoStatePayload && autoStatePayload.preview;
        if (!preview || !window.confirm("Подтвердить результат? Он будет зафиксирован, но фактическая карта ещё не изменится.")) return;
        autoSetFeedback("Фиксируем результат…", "progress", "");
        runAutoMutation(root.dataset.autoConfirmUrl, {run_id: preview.id}).catch(function () {});
    }

    function applyAutoPreviewShift(workShift, confirmReplaceManual) {
        var preview = autoStatePayload && autoStatePayload.preview;
        var shiftState = preview && preview.shift_apply && preview.shift_apply[workShift];
        if (!preview || !shiftState || shiftState.status !== "ready") return;
        var label = workShift === "night" ? "ночную" : "дневную";
        if (!confirmReplaceManual && !window.confirm("Применить " + label + " смену к карте фактического расселения?")) return;
        autoSetFeedback("Применяем " + label + " смену…", "progress", "");
        var url = workShift === "night" ? root.dataset.autoApplyNightUrl : root.dataset.autoApplyDayUrl;
        runAutoMutation(url, {run_id: preview.id, confirm_replace_manual: Boolean(confirmReplaceManual)})
            .then(function (body) {
                if (!body) return;
                markAutoPreviewOpenOnce();
                window.setTimeout(function () { window.location.reload(); }, 700);
            })
            .catch(function (error) {
                if (autoShortCode(error.code) !== "manual_replacement_confirmation_required") return;
                var count = Number(error.details.manual_occupancy_count || 0);
                var message = "Будет заменено ручных размещений: " + count + ".\n\nРучные изменения сохранятся в истории. Изменения затронут только выбранный период и смену.";
                if (window.confirm(message + "\n\nЗаменить ручные изменения и применить?")) {
                    applyAutoPreviewShift(workShift, true);
                }
            });
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
        var fields = [
            ["Табельный номер", employee.personnel_number],
            ["Должность", employee.position],
            ["Категория для расселения", employee.settlement_category_label || "Не указана"],
            ["Подразделение", employee.department],
            ["График", employee.work_schedule],
            ["Бригада", employee.brigade],
            ["Пол", employee.sex],
            ["Телефон", employee.phone],
            ["Фактическое место", employee.residence]
        ];
        if (employee.stay_period) {
            fields.push(
                ["Заселение на койку", employee.stay_period.start],
                ["Выезд с койки", employee.stay_period.end || "Дата не указана"]
            );
        }
        fields.forEach(function (row) {
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
            !relocationSourceBed
            && assignmentType
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
            id: Number(sourceBed.dataset.residentId),
            resident_id: Number(sourceBed.dataset.residentId),
            employee_id: Number(sourceBed.dataset.occupantId),
            full_name: sourceBed.dataset.occupantName,
            personnel_number: "Не указано",
            shift_label: sourceBed.dataset.shiftLabel,
            work_label: sourceBed.dataset.workLabel
        };
        if (!Number(sourceBed.dataset.occupancyId) || !selectedEmployee.id) {
            showFeedback("Не удалось определить точное текущее размещение.", true);
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

    function syncCheckinStart() {
        if (!checkinStart) return;
        checkinStart.hidden = !selectedBed || !isTransferredBed(selectedBed) || selectedBed.dataset.occupied === "true" || controlHeld;
        checkinStart.disabled = !controlAcquireButton || controlAcquireButton.disabled;
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
        autoSyncButtons();
        updateSettleButton();
        syncCheckinStart();
    }

    function setControlState(nextState, message) {
        var labels = {
            free: "Управление не начато",
            held: "Вы управляете расселением",
            busy: "Управление занято другим сотрудником",
            lost: "Связь с управлением потеряна"
        };
        var previouslyHeld = controlHeld;
        controlState = labels[nextState] ? nextState : "lost";
        controlHeld = controlState === "held";
        root.dataset.controlState = controlState;
        root.classList.toggle("is-control-held", controlHeld);
        if (controlPanel) controlPanel.dataset.controlState = controlState;
        if (controlStatus) controlStatus.textContent = labels[controlState];
        if (controlMessage) controlMessage.textContent = message || "";
        if (previouslyHeld !== controlHeld) {
            if (!controlHeld) clearDragState();
            root.querySelectorAll("[data-bed]").forEach(syncBedDragHandle);
            if (placementHint && settlementForm) updatePlacementPanel();
        }
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

    function updateBedOccupancy(destination, occupancy) {
        destination.dataset.occupied = "true";
        destination.dataset.occupancyId = String(occupancy.id || "");
        destination.dataset.stayStart = occupancy.stay_period?.start || "";
        destination.dataset.stayEnd = occupancy.stay_period?.end || "";
        destination.dataset.residentId = String(occupancy.resident_id || "");
        destination.dataset.occupantId = String(occupancy.employee_id || "");
        destination.dataset.occupantName = occupancy.occupant_name;
        destination.dataset.occupantPhotoUrl = occupancy.photo_url || "";
        destination.dataset.shiftLabel = occupancy.shift_label;
        destination.dataset.tooltipShiftLabel = occupancy.tooltip_shift_label || occupancy.shift_label;
        destination.dataset.onExtension = occupancy.on_extension ? "true" : "false";
        destination.dataset.assignmentWarning = occupancy.assignment_warning || "";
        destination.dataset.workLabel = occupancy.work_label;
        destination.dataset.workCaption = occupancy.work_caption || "";
        destination.dataset.tooltipWorkLabel = occupancy.tooltip_work_label || "";
        destination.dataset.assignmentTypeLabel = occupancy.assignment_type_label;
        destination.dataset.assignmentType = occupancy.assignment_type || "";
        destination.classList.remove("is-free");
        destination.classList.add("is-occupied");
        renderMapBed(destination);
    }

    function updateMapAfterSettlement(payload, destination, source) {
        clearDragState();
        if (source) {
            source.dataset.occupied = "false";
            source.dataset.occupancyId = "";
            delete source.dataset.stayStart;
            delete source.dataset.stayEnd;
            source.dataset.residentId = "";
            source.dataset.occupantId = "";
            source.dataset.occupantName = "";
            source.dataset.occupantPhotoUrl = "";
            source.dataset.shiftLabel = "";
            source.dataset.tooltipShiftLabel = "";
            delete source.dataset.onExtension;
            source.dataset.workLabel = "";
            source.dataset.tooltipWorkLabel = "";
            source.dataset.assignmentWarning = "";
            source.dataset.assignmentTypeLabel = "";
            source.dataset.assignmentType = "";
            source.classList.remove("is-occupied");
            source.classList.add("is-free");
            renderMapBed(source);
        }
        updateBedOccupancy(destination, payload.occupancy);
        updateShiftConflicts();

        refreshResidentSearch();
        updateSelectionSummary();
        if (activeRoom && !panel.hidden && (activeRoom.contains(destination) || activeRoom.contains(source || null))) renderRoom(activeRoom);
        if (!source) root.dispatchEvent(new Event("settlement:checked-in"));
        refreshWatchExtensions();
    }

    function submitPlacement() {
        if (!controlHeld || !selectedBed || !selectedEmployee || !assignmentType.value || saving) return;
        if (relocationSourceBed && !Number(relocationSourceBed.dataset.occupancyId)) {
            showFeedback("Не удалось определить точное текущее размещение.", true);
            return;
        }
        var destination = selectedBed;
        var source = relocationSourceBed;
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
            body: JSON.stringify(relocationSourceBed ? {
                action: "relocate",
                occupancy_id: Number(relocationSourceBed.dataset.occupancyId),
                bed_stable_id: selectedBed.dataset.bedId
            } : {
                action: "settle",
                resident_id: selectedEmployee.id,
                bed_stable_id: selectedBed.dataset.bedId,
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
                updateMapAfterSettlement(payload, destination, source);
                if (source && relocationSourceBed === source) {
                    closeRelocationModal(true);
                } else if (selectedBed === destination) {
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
        var occupancyId = Number(selectedBed.dataset.occupancyId);
        if (!occupancyId) {
            showFeedback("Не удалось определить точное текущее размещение.", true);
            return;
        }
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
                occupancy_id: occupancyId
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

    mapModeButtons.forEach(function (button) {
        button.addEventListener("click", function () {
            setMapMode(button.dataset.mapMode);
        });
    });

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

    document.addEventListener("keydown", function (event) {
        if (event.defaultPrevented || event.ctrlKey || event.altKey || event.metaKey || event.isComposing
            || activeMapMode !== "current" || dragSourceBed || relocationSourceBed
            || !/^[0-9\p{L}]$/u.test(event.key)) return;

        var input = /^\d$/.test(event.key) ? searchInput : residentFinder;
        var target = event.target;
        if (!input || target === input) return;
        var switchingSearch = quickSearchOrigin && (target === searchInput || target === residentFinder);
        if (!(target instanceof HTMLElement) || target.isContentEditable
            || (!switchingSearch && target.closest('input, textarea, select, [role="textbox"], [role="combobox"], [role="listbox"], [role="menu"]'))
            || (!root.contains(target) && target !== document.body && target !== document.documentElement)) return;

        var overlays = document.querySelectorAll('dialog[open], [role="dialog"]:not([hidden]), [data-unsettled-panel]:not([hidden]), [data-employee-panel]:not([hidden])');
        if (Array.from(overlays).some(function (overlay) { return overlay.getClientRects().length > 0; })) return;

        var origin = quickSearchOrigin || target;
        event.preventDefault();
        input.focus();
        quickSearchOrigin = origin;
        input.value = event.key;
        input.setSelectionRange(input.value.length, input.value.length);
        input.dispatchEvent(new Event("input", {bubbles: true}));
    });

    if (searchInput) {
        searchInput.addEventListener("input", function () {
            var query = searchInput.value.trim().toLowerCase();
            var number = /^(?:№\s*)?0*(\d+)$/.exec(query);
            state.search = number ? number[1] : query;
            chosenRoomId = "";
            if (state.search && residentFinder && residentFinder.value) {
                residentFinder.value = "";
                chosenResidentId = "";
                refreshResidentSearch();
            }
            highlightRooms();
            refreshRoomOptions();
        });
    }

    if (residentFinder) {
        residentFinder.addEventListener("input", function () {
            chosenResidentId = "";
            if (residentFinder.value && state.search) {
                state.search = "";
                chosenRoomId = "";
                searchInput.value = "";
                highlightRooms();
            }
            refreshResidentSearch();
        });
    }

    root.addEventListener("click", function (event) {
        clearTimeout(avatarClickTimer);
        var bed = event.target.closest("[data-bed]");
        if (bed) {
            if (swapSource) {
                openSwapConfirmation(bed);
                return;
            }
            // Сохраняем двойной щелчок по фото для открытия личной карточки.
            if (event.detail && Number(bed.dataset.occupantId) && event.target.closest("[data-bed-photo-slot], [data-bed-hover-card]")) {
                if (event.detail === 1) avatarClickTimer = setTimeout(function () {
                    openRoom(bed.closest("[data-room-card]"), bed);
                }, 320);
                return;
            }
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

    function updateShiftConflicts(previousShifts) {
        root.querySelectorAll("[data-room-card]").forEach(function (room) {
            var messages = [];
            room.querySelectorAll("[data-bed-block-size]").forEach(function (block) {
                var beds = Array.from(block.querySelectorAll("[data-bed]"));
                var occupied = beds.filter(bed => bed.dataset.occupied === "true");
                var reasons = new Map();
                ["День", "Ночь"].forEach(function (shift) {
                    var group = occupied.filter(bed => bed.dataset.shiftLabel === shift);
                    if (group.length < 3) return;
                    var reason = "Блок " + group[0].dataset.blockLabel + ": " + group.length +
                        (shift === "День" ? " в дневную смену" : " в ночную смену");
                    messages.push(reason);
                    group.forEach(bed => reasons.set(bed, reason));
                });
                beds.forEach(function (bed) {
                    var reason = reasons.get(bed) || "";
                    var before = previousShifts && previousShifts.get(bed.dataset.occupancyId);
                    var changed = before && (before === "День" || before === "Ночь") && before !== bed.dataset.shiftLabel;
                    if (!reason || bed.dataset.causeOccupancy !== bed.dataset.occupancyId) {
                        delete bed.dataset.causeOccupancy;
                        delete bed.dataset.shiftChange;
                    }
                    if (changed) {
                        bed.classList.remove("shift-just-changed");
                        void bed.offsetWidth;
                        bed.classList.add("shift-just-changed");
                    }
                    if (changed && reason) {
                        bed.dataset.causeOccupancy = bed.dataset.occupancyId;
                        bed.dataset.shiftChange = before + " → " + bed.dataset.shiftLabel;
                    }
                    var explanation = reason;
                    if (reason && bed.dataset.shiftChange) explanation += ". Смена изменилась: " + bed.dataset.shiftChange;
                    if ((bed.dataset.shiftConflict || "") !== explanation) {
                        bed.dataset.shiftConflict = explanation;
                        renderMapBed(bed);
                    }
                });
            });
            room.classList.toggle("has-shift-conflict", messages.length > 0);
            var header = room.querySelector("[data-room-open]");
            if (header) header.title = messages.join(". ");
        });
    }

    var assignmentsPending = false;
    async function refreshPublishedAssignments() {
        if (!root.dataset.assignmentsUrl || document.hidden || assignmentsPending || saving || dragSourceBed) return;
        assignmentsPending = true;
        try {
            var response = await fetch(root.dataset.assignmentsUrl, {cache: "no-store"});
            if (!response.ok) return;
            var snapshot = await response.json();
            if (!snapshot.beds || saving || dragSourceBed) return;
            var previous = new Map();
            var changedRooms = new Set();
            root.querySelectorAll("[data-bed][data-occupied='true']").forEach(function (bed) {
                var id = bed.dataset.occupancyId;
                var fields = snapshot.beds[id];
                if (!fields) return;
                previous.set(id, bed.dataset.shiftLabel);
                if (Object.keys(fields).some(key => bed.dataset[key] !== fields[key])) {
                    changedRooms.add(bed.closest("[data-room-card]"));
                    Object.assign(bed.dataset, fields);
                    renderMapBed(bed);
                }
            });
            updateShiftConflicts(previous);
            if (changedRooms.has(activeRoom)) renderBedCards();
        } catch {
            // Retry on the next poll or on return to the tab.
        } finally {
            assignmentsPending = false;
        }
    }
    updateShiftConflicts();
    window.setInterval(refreshPublishedAssignments, 15000);
    document.addEventListener("visibilitychange", refreshPublishedAssignments);
    window.addEventListener("focus", refreshPublishedAssignments);

    var dragSourceBed = null;
    var dropTargetBed = null;

    function setDropTarget(bed) {
        if (dropTargetBed === bed) return;
        if (dropTargetBed) dropTargetBed.classList.remove("is-drop-target");
        dropTargetBed = bed;
        if (dropTargetBed) dropTargetBed.classList.add("is-drop-target");
    }

    function clearDragState() {
        if (dragSourceBed) dragSourceBed.classList.remove("is-drag-source");
        dragSourceBed = null;
        setDropTarget(null);
    }

    root.addEventListener("dragstart", function (event) {
        clearTimeout(avatarClickTimer);
        if (event.target.closest("[data-plan-resident]")) return;
        var handle = event.target.closest("[data-bed-drag-handle]");
        var bed = handle && handle.closest("[data-bed]");
        if (!controlHeld || swapSource || !bed || !isTransferredBed(bed) || bed.dataset.occupied !== "true" || saving) {
            event.preventDefault();
            return;
        }
        dragSourceBed = bed;
        bed.classList.add("is-drag-source");
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", bed.dataset.bedId);
        event.dataTransfer.setData("application/x-settlement-bed", bed.dataset.bedId);
    });

    root.addEventListener("dragover", function (event) {
        if (!dragSourceBed) return;
        var bed = event.target.closest("[data-bed]");
        if (
            !controlHeld
            || !bed
            || bed === dragSourceBed
            || !isTransferredBed(bed)
            || bed.disabled
            || bed.closest("[data-floor-section]").hidden
        ) {
            setDropTarget(null);
            return;
        }
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
        setDropTarget(bed);
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
            || targetBed.closest("[data-floor-section]").hidden
        ) return;
        event.preventDefault();
        var sourceBed = dragSourceBed;
        clearDragState();
        if (targetBed.dataset.occupied === "true") startBedSwap(sourceBed, targetBed);
        else configureRelocation(sourceBed, targetBed);
    });

    root.addEventListener("dragend", clearDragState);

    root.addEventListener("dragstart", function (event) {
        var card = event.target.closest("[data-plan-resident]");
        if (
            !card
            || !controlHeld
            || autoMutationInFlight
            || card.dataset.planDraggable !== "true"
            || activeMapMode !== "next"
        ) return;
        planDragSource = card;
        card.classList.add("is-plan-drag-source");
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", card.dataset.planResident);
        event.dataTransfer.setData("application/x-settlement-plan-resident", card.dataset.planResident);
    });

    root.addEventListener("dragover", function (event) {
        var target = event.target.closest("[data-plan-bed]");
        if (
            !target
            || !planDragSource
            || !controlHeld
            || autoMutationInFlight
            || activeMapMode !== "next"
            || !target.dataset.targetCalendarSlotId
            || target.contains(planDragSource)
        ) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
        root.querySelectorAll(".is-plan-drop-target").forEach(function (node) {
            if (node !== target) node.classList.remove("is-plan-drop-target");
        });
        target.classList.add("is-plan-drop-target");
    });

    root.addEventListener("drop", function (event) {
        var target = event.target.closest("[data-plan-bed]");
        if (
            !target
            || !planDragSource
            || !controlHeld
            || autoMutationInFlight
            || activeMapMode !== "next"
            || !target.dataset.targetCalendarSlotId
            || target.contains(planDragSource)
        ) return;
        event.preventDefault();
        var residentId = Number(planDragSource.dataset.planResident);
        clearPlanDragState();
        var reason = askPlanReason();
        if (!reason) return;
        runPlanCorrection(root.dataset.autoMoveUrl, {
            run_id: autoStatePayload.preview.id,
            resident_id: residentId,
            target_calendar_slot_id: Number(target.dataset.targetCalendarSlotId),
            target_physical_bed_id: Number(target.dataset.targetPhysicalBedId),
            reason: reason
        });
    });

    root.addEventListener("dragend", clearPlanDragState);

    if (planLists) {
        planLists.addEventListener("dragover", function (event) {
            if (event.target.closest("[data-plan-bed]")) event.preventDefault();
        });
    }

    root.addEventListener("click", function (event) {
        var exclude = event.target.closest("[data-plan-exclude]");
        var restore = event.target.closest("[data-plan-restore]");
        if (!exclude && !restore) return;
        if (!controlHeld || autoMutationInFlight || activeMapMode !== "next") return;
        var reason = askPlanReason();
        if (!reason) return;
        runPlanCorrection(
            exclude ? root.dataset.autoExcludeUrl : root.dataset.autoRestoreUrl,
            {
                run_id: autoStatePayload.preview.id,
                resident_id: Number((exclude || restore).dataset[exclude ? "planExclude" : "planRestore"]),
                reason: reason
            }
        );
    });

    root.addEventListener("dblclick", function (event) {
        var handle = event.target.closest("[data-bed-photo-slot], [data-bed-hover-card]");
        var bed = handle && handle.closest("[data-bed]");
        if (!bed || !Number(bed.dataset.occupantId) || bed.dataset.occupied !== "true") return;
        clearTimeout(avatarClickTimer);
        event.preventDefault();
        event.stopPropagation();
        openEmployeePanel(Number(bed.dataset.occupantId));
    });
    panelClose.addEventListener("click", closePanel);
    panelRoomLink.addEventListener("click", function () { openRoom(activeRoom, null); });
    panelEmployeeLink.addEventListener("click", function () {
        if (selectedBed) openEmployeePanel(Number(selectedBed.dataset.occupantId));
    });
    if (roomTransferToggle) roomTransferToggle.addEventListener("click", changeRoomTransfer);
    panelBackdrop.addEventListener("click", closePanel);
    if (employeePanelClose) employeePanelClose.addEventListener("click", closeEmployeePanel);
    if (employeePanelBackdrop) employeePanelBackdrop.addEventListener("click", closeEmployeePanel);
    if (relocationModalClose) relocationModalClose.addEventListener("click", closeRelocationModal);
    if (relocationModalCancel) relocationModalCancel.addEventListener("click", closeRelocationModal);
    if (relocationModalBackdrop) relocationModalBackdrop.addEventListener("click", closeRelocationModal);
    if (autoPreviewModalOpen) autoPreviewModalOpen.addEventListener("click", function () {
        setAutoPreviewModal(true);
        loadAutoState(autoCohort && autoCohort.value, 0);
    });
    if (autoPreviewModalClose) autoPreviewModalClose.addEventListener("click", function () {
        setAutoPreviewModal(false);
    });
    if (autoPreviewModalCancel) autoPreviewModalCancel.addEventListener("click", function () {
        setAutoPreviewModal(false);
    });
    if (autoCohort) autoCohort.addEventListener("change", function () {
        loadAutoState(autoCohort.value, 0);
    });
    if (autoCalculateButton) autoCalculateButton.addEventListener("click", calculateAutoPreview);
    if (autoConfirmButton) autoConfirmButton.addEventListener("click", confirmAutoPreview);
    autoShiftButtons.forEach(function (button) {
        button.addEventListener("click", function () {
            applyAutoPreviewShift(button.dataset.autoApplyShift, false);
        });
    });
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
        clearTimeout(avatarClickTimer);
        if (unsettledPanel && !unsettledPanel.hidden) {
            closeUnsettledPanel(true);
        } else if (autoPreviewModal && !autoPreviewModal.hidden) {
            setAutoPreviewModal(false);
        } else if (relocationModal && !relocationModal.hidden) {
            closeRelocationModal();
        } else if (employeePanel && !employeePanel.hidden) {
            closeEmployeePanel();
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
    setMapMode("current");
    if (watchExtensionStateNode) {
        acceptWatchExtensions(JSON.parse(watchExtensionStateNode.textContent), Date.now());
        document.addEventListener("visibilitychange", resumeWatchExtensions);
        document.addEventListener("resume", resumeWatchExtensions);
        window.addEventListener("focus", resumeWatchExtensions);
        window.addEventListener("pageshow", resumeWatchExtensions);
        window.addEventListener("online", resumeWatchExtensions);
    }
    initializeControlLifecycle();
    if (consumeAutoPreviewOpenOnce()) {
        setAutoPreviewModal(true);
        loadAutoState("", 0);
    }
}());
