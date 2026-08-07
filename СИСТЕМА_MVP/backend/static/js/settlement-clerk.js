(function () {
    "use strict";

    var root = document.querySelector("[data-settlement-root]");
    if (!root) return;

    var state = {
        dormitory: "5",
        floor: "1",
        status: "all",
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
    var panelTitle = root.querySelector("[data-room-panel-title]");
    var panelLocation = root.querySelector("[data-room-panel-location]");
    var panelStatus = root.querySelector("[data-room-panel-status]");
    var panelOccupiedCount = root.querySelector("[data-room-occupied-count]");
    var panelFreeCount = root.querySelector("[data-room-free-count]");
    var panelBeds = root.querySelector("[data-room-panel-beds]");
    var placementHint = root.querySelector("[data-placement-hint]");
    var settlementForm = root.querySelector("[data-settlement-form]");
    var employeeSearch = root.querySelector("[data-employee-search]");
    var employeeResults = root.querySelector("[data-employee-results]");
    var selectedEmployeeCard = root.querySelector("[data-selected-employee]");
    var selectedEmployeeName = root.querySelector("[data-selected-employee-name]");
    var selectedEmployeeMeta = root.querySelector("[data-selected-employee-meta]");
    var assignmentType = root.querySelector("[data-assignment-type]");
    var settleButton = root.querySelector("[data-settle-button]");
    var placementFeedback = root.querySelector("[data-placement-feedback]");
    var unsettledPanelToggle = root.querySelector("[data-unsettled-panel-toggle]");
    var unsettledPanel = root.querySelector("[data-unsettled-panel]");
    var unsettledPanelClose = root.querySelector("[data-unsettled-panel-close]");
    var unsettledSearch = root.querySelector("[data-unsettled-search]");
    var unsettledVisibleCount = root.querySelector("[data-unsettled-visible-count]");
    var unsettledFilterEmpty = root.querySelector("[data-unsettled-filter-empty]");
    var unsettledEmployees = Array.from(root.querySelectorAll("[data-unsettled-employee]"));
    var unsettledShiftButtons = Array.from(root.querySelectorAll("[data-unsettled-shift-filter]"));
    var activeRoom = null;
    var selectedBed = null;
    var selectedEmployee = null;
    var searchTimer = null;
    var searchSequence = 0;
    var saving = false;
    var unsettledShift = "all";

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
        if (unsettledPanelToggle) {
            unsettledPanelToggle.setAttribute("aria-expanded", "false");
            unsettledPanelToggle.setAttribute(
                "aria-label",
                "Открыть панель нерасселённых сотрудников"
            );
        }
        document.body.classList.remove("settlement-unsettled-panel-open");
        if (restoreFocus !== false && unsettledPanelToggle) unsettledPanelToggle.focus();
    }

    function openUnsettledPanel() {
        if (!unsettledPanel) return;
        closePanel();
        unsettledPanel.hidden = false;
        unsettledPanel.setAttribute("aria-hidden", "false");
        if (unsettledPanelToggle) {
            unsettledPanelToggle.setAttribute("aria-expanded", "true");
            unsettledPanelToggle.setAttribute(
                "aria-label",
                "Закрыть панель нерасселённых сотрудников"
            );
        }
        document.body.classList.add("settlement-unsettled-panel-open");
        renderUnsettledEmployees();
        if (unsettledSearch) unsettledSearch.focus();
    }

    function toggleUnsettledPanel() {
        if (!unsettledPanel) return;
        if (unsettledPanel.hidden) {
            openUnsettledPanel();
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

    function renderMapBed(bed) {
        var occupied = bed.dataset.occupied === "true";
        var unavailable = Boolean(bed.disabled);
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

        bed.dataset.bedState = unavailable ? "unavailable" : (occupied ? "occupied" : "free");
        bed.classList.toggle("is-occupied", occupied);
        bed.classList.toggle("is-free", !occupied);
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
        applyFilters();
    }

    function matchesRoom(room) {
        var dormitoryMatch = room.dataset.dormitory === state.dormitory;
        var floorMatch = room.dataset.floor === state.floor;
        var statusMatch = (
            state.status === "all"
            || room.dataset.transferStatus === state.status
        );
        var searchMatch = (
            !state.search
            || String(room.dataset.search || "").includes(state.search)
        );
        return dormitoryMatch && floorMatch && statusMatch && searchMatch;
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
        if (employeeResults) {
            employeeResults.hidden = true;
            employeeResults.replaceChildren();
        }
        if (employeeSearch) employeeSearch.value = "";
        if (assignmentType) assignmentType.value = "";
        updateSettleButton();
    }

    function showFeedback(message, isError) {
        if (!placementFeedback) return;
        placementFeedback.textContent = message;
        placementFeedback.classList.toggle("is-error", Boolean(isError));
        placementFeedback.hidden = !message;
    }

    function updatePlacementPanel() {
        if (!placementHint || !settlementForm) return;
        showFeedback("", false);
        settlementForm.hidden = true;
        placementHint.hidden = false;

        if (!selectedBed) {
            placementHint.textContent = "Выберите свободное койко-место в переданной комнате.";
            return;
        }
        if (selectedBed.dataset.occupied === "true") {
            placementHint.textContent = "Это койко-место занято. Доступны только сведения о текущем жильце.";
            return;
        }
        if (!activeRoom || activeRoom.dataset.transferStatus !== "transferred") {
            placementHint.textContent = "Комната не передана. Заселение в неё недоступно.";
            return;
        }

        placementHint.hidden = true;
        settlementForm.hidden = false;
        if (employeeSearch) employeeSearch.focus();
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
        if (changed) resetEmployeeSelection();
        renderBedCards();
        updatePlacementPanel();
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
        settleButton.disabled = Boolean(
            saving
            || !selectedEmployee
            || !assignmentType
            || !assignmentType.value
        );
    }

    function csrfToken() {
        var input = settlementForm.querySelector("input[name='csrfmiddlewaretoken']");
        if (input && input.value) return input.value;
        var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : "";
    }

    function updateMapAfterSettlement(payload) {
        var occupancy = payload.occupancy;
        selectedBed.dataset.occupied = "true";
        selectedBed.dataset.occupantName = occupancy.occupant_name;
        selectedBed.dataset.occupantPhotoUrl = occupancy.photo_url || "";
        selectedBed.dataset.shiftLabel = occupancy.shift_label;
        selectedBed.dataset.workLabel = occupancy.work_label;
        selectedBed.dataset.assignmentTypeLabel = occupancy.assignment_type_label;
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
        if (!selectedBed || !selectedEmployee || !assignmentType.value || saving) return;
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
                bed_stable_id: selectedBed.dataset.bedId,
                employee_id: selectedEmployee.id,
                assignment_type: assignmentType.value
            })
        })
            .then(function (response) {
                return response.json().then(function (payload) {
                    if (!response.ok) throw new Error(payload.error || "Не удалось сохранить заселение.");
                    return payload;
                });
            })
            .then(function (payload) {
                var occupantName = payload.occupancy.occupant_name;
                updateMapAfterSettlement(payload);
                resetEmployeeSelection();
                showFeedback(occupantName + " заселён в выбранное койко-место.", false);
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
        if (!selectedBed || !selectedEmployee || !assignmentType.value || saving) return;
        var destination = [
            selectedBed.dataset.dormitoryLabel,
            selectedBed.dataset.floorLabel,
            selectedBed.dataset.roomLabel,
            "блок " + selectedBed.dataset.blockLabel,
            selectedBed.dataset.positionLabel
        ].join(", ");
        var message = "Заселить " + selectedEmployee.full_name + " в " + destination + "?";
        if (typeof window.openAppConfirmDialog === "function") {
            window.openAppConfirmDialog(
                message,
                submitPlacement,
                0,
                "Заселить",
                {title: "Подтвердите заселение"}
            );
            return;
        }
        if (window.confirm(message)) submitPlacement();
    }

    [
        ["[data-dorm-filter]", "dormitory", "dormFilter"],
        ["[data-floor-filter]", "floor", "floorFilter"],
        ["[data-status-filter]", "status", "statusFilter"]
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
        var bed = event.target.closest("[data-bed]");
        if (bed) {
            openRoom(bed.closest("[data-room-card]"), bed);
            return;
        }
        var roomButton = event.target.closest("[data-room-open]");
        if (roomButton) openRoom(roomButton.closest("[data-room-card]"), null);
    });

    root.querySelectorAll("[data-bed]").forEach(function (bed) {
        renderMapBed(bed);
        if (!bed.disabled) bed.setAttribute("aria-pressed", "false");
    });
    panelClose.addEventListener("click", closePanel);
    panelBackdrop.addEventListener("click", closePanel);
    if (unsettledPanelToggle) {
        unsettledPanelToggle.addEventListener("click", toggleUnsettledPanel);
    }
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
        } else if (!panel.hidden) {
            closePanel();
        }
    });
    employeeSearch.addEventListener("input", function () {
        window.clearTimeout(searchTimer);
        searchTimer = window.setTimeout(runEmployeeSearch, 220);
    });
    assignmentType.addEventListener("change", updateSettleButton);
    settlementForm.addEventListener("submit", function (event) {
        event.preventDefault();
        confirmPlacement();
    });

    applyFilters();
}());
