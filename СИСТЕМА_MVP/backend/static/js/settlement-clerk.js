(function () {
    "use strict";

    var root = document.querySelector("[data-settlement-root]");
    if (!root) return;

    var state = {
        dormitory: "all",
        floor: "all",
        status: "all",
        search: ""
    };
    var rooms = Array.from(root.querySelectorAll("[data-room-card]"));
    var visibleRoomCount = root.querySelector("[data-visible-room-count]");
    var globalFreeCount = root.querySelector("[data-free-bed-count]");
    var globalOccupiedCount = root.querySelector("[data-occupied-bed-count]");
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
    var activeRoom = null;
    var selectedBed = null;
    var selectedEmployee = null;
    var searchTimer = null;
    var searchSequence = 0;
    var saving = false;

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
        var dormitoryMatch = (
            state.dormitory === "all"
            || room.dataset.dormitory === state.dormitory
        );
        var floorMatch = (
            state.floor === "all"
            || room.dataset.floor === state.floor
        );
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

    function applyFilters() {
        var matches = 0;
        rooms.forEach(function (room) {
            var match = matchesRoom(room);
            room.classList.toggle("is-filter-muted", !match);
            room.setAttribute("data-filter-match", String(match));
            if (match) matches += 1;
        });
        if (visibleRoomCount) visibleRoomCount.textContent = String(matches);
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
        selectedBed.dataset.shiftLabel = occupancy.shift_label;
        selectedBed.dataset.workLabel = occupancy.work_label;
        selectedBed.dataset.assignmentTypeLabel = occupancy.assignment_type_label;
        selectedBed.classList.remove("is-free");
        selectedBed.classList.add("is-occupied");
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

        if (globalFreeCount) globalFreeCount.textContent = String(payload.summary.free_beds);
        if (globalOccupiedCount) globalOccupiedCount.textContent = String(payload.summary.occupied_beds);
        var dormitory = activeRoom.closest("[data-dormitory-section]");
        var dormitoryFree = dormitory ? dormitory.querySelector("[data-dormitory-free-count]") : null;
        if (dormitoryFree) {
            var freeBeds = Array.from(dormitory.querySelectorAll("[data-room-card][data-transfer-status='transferred'] [data-bed]"))
                .filter(function (bed) { return bed.dataset.occupied !== "true"; })
                .length;
            dormitoryFree.textContent = String(freeBeds);
        }
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

    root.querySelectorAll("[data-bed]:not(:disabled)").forEach(function (bed) {
        bed.setAttribute("aria-pressed", "false");
    });
    panelClose.addEventListener("click", closePanel);
    panelBackdrop.addEventListener("click", closePanel);
    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && !panel.hidden) closePanel();
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
