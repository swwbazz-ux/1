(function () {
    "use strict";

    var shell = document.querySelector("[data-admin-theme]");
    var themeToggle = document.querySelector("[data-admin-theme-toggle]");

    function setTheme(theme) {
        if (!shell) return;
        var isNight = theme === "night";
        shell.classList.toggle("admin-night", isNight);
        shell.classList.toggle("admin-day", !isNight);
        if (themeToggle) {
            themeToggle.setAttribute("data-theme-icon", isNight ? "moon" : "sun");
            themeToggle.setAttribute("aria-label", isNight ? "Включить дневную тему" : "Включить ночную тему");
            themeToggle.setAttribute("title", isNight ? "Дневной режим" : "Ночной режим");
        }
        try {
            window.localStorage.setItem("admin-theme", isNight ? "night" : "day");
        } catch (error) {}
    }

    var savedTheme = "day";
    try {
        savedTheme = window.localStorage.getItem("admin-theme") || "day";
    } catch (error) {}
    setTheme(savedTheme);

    if (themeToggle) {
        themeToggle.addEventListener("click", function () {
            setTheme(shell && shell.classList.contains("admin-night") ? "day" : "night");
        });
    }

    var root = document.querySelector("[data-deputy-planning-root]");
    var dataNode = document.getElementById("deputy-planning-data");
    if (!root || !dataNode) return;

    var state;
    try {
        state = JSON.parse(dataNode.textContent || "{}");
    } catch (error) {
        state = {};
    }

    function normalizeState(payload) {
        payload = payload && typeof payload === "object" ? payload : {};
        payload.plan = payload.plan || {};
        payload.role = payload.role || {};
        payload.endpoints = payload.endpoints || {};
        payload.summary = payload.summary || {};
        payload.categories = Array.isArray(payload.categories) ? payload.categories : [];
        payload.employees = Array.isArray(payload.employees) ? payload.employees : [];
        payload.rows = Array.isArray(payload.rows) ? payload.rows : [];
        return payload;
    }

    state = normalizeState(state);

    var categoryNav = root.querySelector("[data-category-nav]");
    var workDate = root.querySelector("[data-work-date]");
    var searchInput = root.querySelector("[data-planning-search]");
    var employeeList = root.querySelector("[data-employee-list]");
    var employeeEmpty = root.querySelector("[data-employee-empty]");
    var availableCount = root.querySelector("[data-available-count]");
    var board = root.querySelector("[data-assignment-board]");
    var boardEmpty = root.querySelector("[data-board-empty]");
    var filterButtons = Array.prototype.slice.call(root.querySelectorAll("[data-row-filter]"));
    var summaryCounts = Array.prototype.slice.call(root.querySelectorAll("[data-summary-count]"));
    var autosaveState = root.querySelector("[data-autosave-state]");
    var autosaveText = root.querySelector("[data-autosave-text]");
    var publishButton = root.querySelector("[data-publish-button]");
    var candidateDialog = document.querySelector("[data-candidate-dialog]");
    var candidateContext = document.querySelector("[data-candidate-context]");
    var candidateList = document.querySelector("[data-candidate-list]");
    var clearSlotButton = document.querySelector("[data-clear-slot]");
    var photoDialog = document.querySelector("[data-photo-dialog]");
    var photoImage = document.querySelector("[data-photo-image]");
    var photoCaption = document.querySelector("[data-photo-caption]");
    var publishDialog = document.querySelector("[data-publish-dialog]");
    var publishSummary = document.querySelector("[data-publish-summary]");
    var publishConfirm = document.querySelector("[data-publish-confirm]");
    var toast = document.querySelector("[data-deputy-toast]");
    var currentFilter = "all";
    var currentSlot = null;
    var saving = false;
    var toastTimer = null;
    var dragPayload = null;

    function textValue(value) {
        return value === null || value === undefined ? "" : String(value);
    }

    function employeeName(employee) {
        return textValue(employee && (employee.full_name || employee.name || employee.label)) || "Сотрудник";
    }

    function employeeMeta(employee) {
        return textValue(employee && (
            employee.position_label || employee.role_label || employee.position || employee.meta || employee.personnel_number
        ));
    }

    function employeePhoto(employee) {
        return textValue(employee && (employee.photo_url || employee.photo));
    }

    function employeeInitials(employee) {
        var provided = textValue(employee && employee.initials);
        if (provided) return provided;
        return employeeName(employee).split(/\s+/).filter(Boolean).slice(0, 2).map(function (part) {
            return part.charAt(0);
        }).join("").toUpperCase() || "??";
    }

    function employeeSearchText(employee) {
        return [employeeName(employee), employeeMeta(employee), employee && employee.personnel_number]
            .map(textValue).join(" ").toLocaleLowerCase("ru");
    }

    function rowSearchText(row) {
        var equipment = row.equipment || {};
        var slotEmployees = (row.slots || []).map(function (slot) {
            return employeeName(slot.employee);
        });
        return [equipment.label, equipment.model_label, equipment.status_label, row.attention]
            .concat(slotEmployees).map(textValue).join(" ").toLocaleLowerCase("ru");
    }

    function createElement(tag, className, content) {
        var element = document.createElement(tag);
        if (className) element.className = className;
        if (content !== undefined && content !== null) element.textContent = textValue(content);
        return element;
    }

    function openDialog(dialog) {
        if (!dialog) return;
        if (typeof dialog.showModal === "function") {
            if (!dialog.open) dialog.showModal();
        } else {
            dialog.setAttribute("open", "");
        }
    }

    function closeDialog(dialog) {
        if (!dialog) return;
        if (typeof dialog.close === "function" && dialog.open) {
            dialog.close();
        } else {
            dialog.removeAttribute("open");
        }
    }

    function showToast(message, isError) {
        if (!toast) return;
        window.clearTimeout(toastTimer);
        toast.textContent = message;
        toast.classList.toggle("is-error", Boolean(isError));
        toast.hidden = false;
        toastTimer = window.setTimeout(function () {
            toast.hidden = true;
        }, isError ? 6000 : 3200);
    }

    function updateAutosave(mode, message) {
        if (!autosaveState || !autosaveText) return;
        autosaveState.classList.toggle("is-saving", mode === "saving");
        autosaveState.classList.toggle("is-error", mode === "error");
        autosaveText.textContent = message;
    }

    function currentQuery() {
        return textValue(searchInput && searchInput.value).trim().toLocaleLowerCase("ru");
    }

    function planEditable() {
        return Boolean(state.plan.editable && state.endpoints.slot);
    }

    function sourceForEmployee(employee) {
        var assignment = employee && employee.assignment && typeof employee.assignment === "object" ? employee.assignment : {};
        return {
            source_equipment_id: employee && (employee.source_equipment_id || assignment.equipment_id) || null,
            source_shift_type: employee && (employee.source_shift_type || assignment.shift_type) || null
        };
    }

    function openPhoto(employee) {
        var photo = employeePhoto(employee);
        if (!photo || !photoDialog || !photoImage) return;
        var name = employeeName(employee);
        photoImage.src = photo;
        photoImage.alt = "Фото " + name;
        if (photoCaption) photoCaption.textContent = name;
        openDialog(photoDialog);
    }

    function createAvatar(employee, interactive) {
        var photo = employeePhoto(employee);
        var canOpenPhoto = Boolean(photo && interactive !== false);
        var avatar = createElement(canOpenPhoto ? "button" : "span", "deputy-avatar");
        if (photo) {
            var image = document.createElement("img");
            image.src = photo;
            image.alt = "";
            avatar.appendChild(image);
            if (canOpenPhoto) {
                avatar.type = "button";
                avatar.setAttribute("aria-label", "Открыть фото " + employeeName(employee));
                avatar.title = "Открыть фото";
                avatar.addEventListener("click", function (event) {
                    event.preventDefault();
                    event.stopPropagation();
                    openPhoto(employee);
                });
            } else {
                avatar.setAttribute("aria-hidden", "true");
            }
        } else {
            avatar.textContent = employeeInitials(employee);
            avatar.setAttribute("aria-hidden", "true");
        }
        return avatar;
    }

    function renderCategories() {
        if (!categoryNav) return;
        categoryNav.replaceChildren();
        categoryNav.hidden = state.categories.length === 0;
        state.categories.forEach(function (category) {
            var link = createElement("a", "", category.label || category.code);
            link.href = category.url || "#";
            var isActive = Boolean(category.active || category.is_active || category.code === state.role.code);
            link.classList.toggle("is-active", isActive);
            if (isActive) link.setAttribute("aria-current", "page");
            if (!category.url) {
                link.addEventListener("click", function (event) { event.preventDefault(); });
            }
            categoryNav.appendChild(link);
        });
    }

    function renderSummary() {
        if (workDate && state.plan.work_date_label) workDate.textContent = textValue(state.plan.work_date_label);
        summaryCounts.forEach(function (node) {
            var key = node.getAttribute("data-summary-count");
            node.textContent = textValue(state.summary[key] || 0);
        });
    }

    function bindDragSource(node, employee) {
        var canDrag = planEditable() && !employee.disabled && !employee.busy;
        node.draggable = canDrag;
        node.classList.toggle("is-disabled", !canDrag && Boolean(employee.disabled || employee.busy));
        if (!canDrag) return;
        node.addEventListener("dragstart", function (event) {
            var source = sourceForEmployee(employee);
            dragPayload = {
                employeeId: employee.id,
                sourceEquipmentId: source.source_equipment_id,
                sourceShiftType: source.source_shift_type
            };
            node.classList.add("is-dragging");
            if (event.dataTransfer) {
                event.dataTransfer.effectAllowed = "move";
                event.dataTransfer.setData("application/json", JSON.stringify(dragPayload));
                event.dataTransfer.setData("text/plain", textValue(employee.id));
            }
        });
        node.addEventListener("dragend", function () {
            node.classList.remove("is-dragging");
            dragPayload = null;
        });
    }

    function createEmployeeCard(employee) {
        var card = createElement("article", "deputy-employee-card");
        card.setAttribute("role", "listitem");
        card.appendChild(createAvatar(employee));
        var main = createElement("span", "deputy-employee-main");
        main.appendChild(createElement("strong", "", employeeName(employee)));
        main.appendChild(createElement("small", "", employeeMeta(employee) || "Сотрудник"));
        card.appendChild(main);
        card.appendChild(createElement("span", "deputy-drag-mark", "⠿"));
        bindDragSource(card, employee);
        return card;
    }

    function renderEmployees() {
        if (!employeeList) return;
        var query = currentQuery();
        var visible = state.employees.filter(function (employee) {
            return !query || employeeSearchText(employee).indexOf(query) !== -1;
        });
        employeeList.replaceChildren();
        visible.forEach(function (employee) {
            employeeList.appendChild(createEmployeeCard(employee));
        });
        employeeList.hidden = visible.length === 0;
        if (availableCount) {
            availableCount.textContent = query ? visible.length + " из " + state.employees.length : textValue(state.employees.length);
        }
        if (employeeEmpty) employeeEmpty.hidden = visible.length !== 0;
    }

    function rowHasConflict(row) {
        return Boolean(row.conflict || (row.slots || []).some(function (slot) { return slot.conflict; }));
    }

    function rowHasChanged(row) {
        return Boolean(row.changed || (row.slots || []).some(function (slot) { return slot.changed; }));
    }

    function rowIsUnfilled(row) {
        return (row.slots || []).some(function (slot) { return !slot.employee; });
    }

    function rowIsAssigned(row) {
        return (row.slots || []).some(function (slot) { return Boolean(slot.employee); });
    }

    function rowMatchesFilter(row) {
        if (currentFilter === "assigned") return rowIsAssigned(row);
        if (currentFilter === "unfilled") return rowIsUnfilled(row);
        if (currentFilter === "conflict") return rowHasConflict(row);
        if (currentFilter === "changed") return rowHasChanged(row);
        return true;
    }

    function neutralStatus(label) {
        return ["", "исправен", "исправна", "активен", "активна", "в работе", "норма"].indexOf(textValue(label).trim().toLocaleLowerCase("ru")) !== -1;
    }

    function attentionLabel(row) {
        if (typeof row.attention === "string") return row.attention;
        if (row.issue) return textValue(row.issue);
        if (rowHasConflict(row)) return "Конфликт назначения";
        return "";
    }

    function createEquipmentCell(row) {
        var equipment = row.equipment || {};
        var wrapper = createElement("div", "deputy-equipment-cell");
        var iconWrap = createElement("span", "deputy-equipment-icon");
        if (equipment.icon_url) {
            var icon = document.createElement("img");
            icon.src = equipment.icon_url;
            icon.alt = "";
            iconWrap.appendChild(icon);
        }
        wrapper.appendChild(iconWrap);
        var main = createElement("span", "deputy-equipment-main");
        main.appendChild(createElement("strong", "", equipment.label || "Техника"));
        if (equipment.model_label) main.appendChild(createElement("small", "", equipment.model_label));
        var issue = attentionLabel(row);
        var status = textValue(equipment.status_label);
        var note = issue || (!neutralStatus(status) ? status : "");
        if (note) {
            var noteNode = createElement("span", "deputy-equipment-note" + (rowHasConflict(row) ? " is-danger" : ""), note);
            main.appendChild(noteNode);
        }
        wrapper.appendChild(main);
        return wrapper;
    }

    function openCandidatePicker(row, slot) {
        if (!planEditable() || saving || !candidateDialog || !candidateList) return;
        currentSlot = { row: row, slot: slot };
        if (candidateContext) {
            candidateContext.textContent = [row.equipment && row.equipment.label, slot.label]
                .filter(Boolean).join(" · ");
        }
        candidateList.replaceChildren();
        var candidatesById = new Map();
        state.employees.forEach(function (employee) {
            candidatesById.set(textValue(employee.id), employee);
        });
        state.rows.forEach(function (candidateRow) {
            (candidateRow.slots || []).forEach(function (candidateSlot) {
                if (!candidateSlot.employee) return;
                var assignedEmployee = Object.assign({}, candidateSlot.employee, {
                    source_equipment_id: candidateRow.equipment && candidateRow.equipment.id,
                    source_shift_type: candidateSlot.shift_type,
                    assigned_label: [candidateRow.equipment && candidateRow.equipment.label, candidateSlot.label]
                        .filter(Boolean).join(" · ")
                });
                candidatesById.set(textValue(assignedEmployee.id), assignedEmployee);
            });
        });
        var candidates = Array.from(candidatesById.values()).sort(function (left, right) {
            return employeeName(left).localeCompare(employeeName(right), "ru");
        });
        candidates.forEach(function (employee) {
            var button = createElement("button", "deputy-candidate");
            button.type = "button";
            button.disabled = Boolean(employee.disabled || employee.busy);
            button.appendChild(createAvatar(employee, false));
            var main = createElement("span", "deputy-employee-main");
            main.appendChild(createElement("strong", "", employeeName(employee)));
            main.appendChild(createElement("small", "", employee.busy_reason || employee.assigned_label || employeeMeta(employee) || "Сотрудник"));
            button.appendChild(main);
            button.addEventListener("click", function () {
                var source = sourceForEmployee(employee);
                closeDialog(candidateDialog);
                saveSlot(row, slot, employee.id, source);
            });
            candidateList.appendChild(button);
        });
        if (!candidates.length) {
            candidateList.appendChild(createElement("p", "deputy-empty-state", "Свободных сотрудников нет."));
        }
        if (clearSlotButton) clearSlotButton.hidden = !slot.employee;
        openDialog(candidateDialog);
    }

    function dropPayloadFromEvent(event) {
        if (dragPayload) return dragPayload;
        if (!event.dataTransfer) return null;
        try {
            return JSON.parse(event.dataTransfer.getData("application/json") || "null");
        } catch (error) {
            var employeeId = event.dataTransfer.getData("text/plain");
            return employeeId ? { employeeId: employeeId } : null;
        }
    }

    function createSlot(row, slot) {
        var button = createElement("div", "deputy-slot");
        button.classList.toggle("is-disabled", !planEditable());
        button.classList.toggle("has-employee", Boolean(slot.employee));
        button.classList.toggle("has-conflict", Boolean(slot.conflict));

        if (slot.employee) {
            button.appendChild(createAvatar(slot.employee));
            var person = createElement("button", "deputy-slot-person");
            person.type = "button";
            person.disabled = !planEditable();
            person.setAttribute("aria-label", "Изменить назначение: " + textValue(slot.label));
            var main = createElement("span", "deputy-employee-main");
            main.appendChild(createElement("strong", "", employeeName(slot.employee)));
            main.appendChild(createElement("small", "", employeeMeta(slot.employee) || slot.label || "Назначен"));
            person.appendChild(main);
            var flags = createElement("span", "deputy-slot-flags");
            if (slot.changed) flags.appendChild(createElement("span", "deputy-slot-flag", "ИЗМ"));
            if (slot.conflict) flags.appendChild(createElement("span", "deputy-slot-flag is-conflict", "!"));
            person.appendChild(flags);
            button.appendChild(person);
            bindDragSource(button, Object.assign({}, slot.employee, {
                source_equipment_id: row.equipment && row.equipment.id,
                source_shift_type: slot.shift_type
            }));
            person.addEventListener("click", function () {
                openCandidatePicker(row, slot);
            });
        } else {
            var emptyAction = createElement("button", "deputy-slot-empty", "Назначить");
            emptyAction.type = "button";
            emptyAction.disabled = !planEditable();
            emptyAction.setAttribute("aria-label", "Назначить сотрудника: " + textValue(slot.label));
            emptyAction.addEventListener("click", function () {
                openCandidatePicker(row, slot);
            });
            button.appendChild(emptyAction);
        }
        if (slot.issue) button.appendChild(createElement("span", "deputy-slot-issue", slot.issue));
        button.addEventListener("dragover", function (event) {
            if (!planEditable() || saving) return;
            event.preventDefault();
            button.classList.add("is-drag-over");
            if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
        });
        button.addEventListener("dragleave", function () {
            button.classList.remove("is-drag-over");
        });
        button.addEventListener("drop", function (event) {
            if (!planEditable() || saving) return;
            event.preventDefault();
            button.classList.remove("is-drag-over");
            var payload = dropPayloadFromEvent(event);
            if (!payload || !payload.employeeId) return;
            saveSlot(row, slot, payload.employeeId, {
                source_equipment_id: payload.sourceEquipmentId,
                source_shift_type: payload.sourceShiftType
            });
        });
        return button;
    }

    function renderBoard() {
        if (!board) return;
        var query = currentQuery();
        var rows = state.rows.filter(function (row) {
            return rowMatchesFilter(row) && (!query || rowSearchText(row).indexOf(query) !== -1);
        });
        board.replaceChildren();
        if (!rows.length) {
            if (boardEmpty) boardEmpty.hidden = false;
            return;
        }
        if (boardEmpty) boardEmpty.hidden = true;

        var table = createElement("table", "deputy-assignment-table");
        var thead = document.createElement("thead");
        var headRow = document.createElement("tr");
        headRow.appendChild(createElement("th", "", "Техника"));
        var firstSlots = rows[0].slots || [];
        headRow.appendChild(createElement("th", "", firstSlots[0] && firstSlots[0].label || "Смена 1"));
        headRow.appendChild(createElement("th", "", firstSlots[1] && firstSlots[1].label || "Смена 2"));
        thead.appendChild(headRow);
        table.appendChild(thead);

        var tbody = document.createElement("tbody");
        rows.forEach(function (row) {
            var tr = document.createElement("tr");
            tr.classList.toggle("has-attention", Boolean(row.attention || row.issue));
            tr.classList.toggle("has-conflict", rowHasConflict(row));
            var equipmentCell = document.createElement("td");
            equipmentCell.appendChild(createEquipmentCell(row));
            tr.appendChild(equipmentCell);
            [0, 1].forEach(function (index) {
                var slot = (row.slots || [])[index] || {
                    shift_type: index === 0 ? "day" : "night",
                    label: index === 0 ? "Смена 1" : "Смена 2",
                    employee: null
                };
                var cell = document.createElement("td");
                cell.appendChild(createSlot(row, slot));
                tr.appendChild(cell);
            });
            tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        board.appendChild(table);
    }

    function updatePublishButton() {
        if (!publishButton) return;
        var conflicts = Number(state.summary.conflict_count || 0);
        var canPublish = Boolean(state.plan.editable && state.endpoints.publish && !saving && conflicts === 0);
        publishButton.disabled = !canPublish;
        publishButton.textContent = state.plan.editable ? "Опубликовать" : "Опубликовано";
        if (conflicts > 0) {
            publishButton.title = "Сначала устраните конфликты";
        } else if (!state.endpoints.publish) {
            publishButton.title = "Публикация недоступна";
        } else {
            publishButton.removeAttribute("title");
        }
    }

    function renderAll() {
        renderCategories();
        renderSummary();
        renderEmployees();
        renderBoard();
        updatePublishButton();
    }

    function csrfToken() {
        var input = root.querySelector("input[name='csrfmiddlewaretoken']");
        if (input && input.value) return input.value;
        var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : "";
    }

    async function postJson(url, body) {
        var response = await window.fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRFToken": csrfToken()
            },
            body: JSON.stringify(body)
        });
        var result;
        try {
            result = await response.json();
        } catch (error) {
            result = { ok: false, error: "Сервер вернул некорректный ответ." };
        }
        if (!response.ok || !result.ok) {
            var requestError = new Error(result.error || result.message || "Не удалось сохранить изменения.");
            requestError.payload = result.payload;
            throw requestError;
        }
        return result.payload;
    }

    function applyPayload(payload) {
        state = normalizeState(payload);
        renderAll();
    }

    async function saveSlot(row, slot, employeeId, source) {
        if (saving || !planEditable()) return;
        saving = true;
        updateAutosave("saving", "Сохраняю…");
        updatePublishButton();
        var body = {
            plan_id: state.plan.id,
            expected_version: state.plan.version,
            equipment_id: row.equipment && row.equipment.id,
            shift_type: slot.shift_type,
            employee_id: employeeId === null ? null : employeeId
        };
        if (source && source.source_equipment_id) body.source_equipment_id = source.source_equipment_id;
        if (source && source.source_shift_type) body.source_shift_type = source.source_shift_type;
        try {
            var payload = await postJson(state.endpoints.slot, body);
            applyPayload(payload);
            var savedLabel = state.plan.updated_at_label ? "Сохранено · " + state.plan.updated_at_label : "Все изменения сохранены";
            updateAutosave("saved", savedLabel);
        } catch (error) {
            if (error.payload) applyPayload(error.payload);
            updateAutosave("error", "Не сохранено");
            showToast(error.message, true);
        } finally {
            saving = false;
            updatePublishButton();
        }
    }

    function openPublishConfirmation() {
        if (!publishDialog || !state.plan.editable || saving) return;
        var unfilled = Number(state.summary.unfilled_count || 0);
        if (publishSummary) {
            publishSummary.textContent = unfilled > 0
                ? "Останутся незаполненные слоты: " + unfilled + ". Опубликовать текущую расстановку?"
                : "После публикации назначения станут доступны сотрудникам.";
        }
        openDialog(publishDialog);
    }

    async function publishPlan() {
        if (saving || !state.plan.editable || !state.endpoints.publish) return;
        closeDialog(publishDialog);
        saving = true;
        updateAutosave("saving", "Публикую…");
        updatePublishButton();
        try {
            var payload = await postJson(state.endpoints.publish, {
                plan_id: state.plan.id,
                expected_version: state.plan.version
            });
            applyPayload(payload);
            updateAutosave("saved", "Расстановка опубликована");
            showToast("Расстановка опубликована.", false);
        } catch (error) {
            if (error.payload) applyPayload(error.payload);
            updateAutosave("error", "Не опубликовано");
            showToast(error.message, true);
        } finally {
            saving = false;
            updatePublishButton();
        }
    }

    filterButtons.forEach(function (button) {
        button.addEventListener("click", function () {
            currentFilter = button.getAttribute("data-row-filter") || "all";
            filterButtons.forEach(function (item) {
                var active = item === button;
                item.classList.toggle("is-active", active);
                item.setAttribute("aria-pressed", active ? "true" : "false");
            });
            renderBoard();
        });
    });

    if (searchInput) {
        searchInput.addEventListener("input", function () {
            renderEmployees();
            renderBoard();
        });
    }

    Array.prototype.slice.call(document.querySelectorAll("[data-dialog-close]")).forEach(function (button) {
        button.addEventListener("click", function () { closeDialog(candidateDialog); });
    });
    Array.prototype.slice.call(document.querySelectorAll("[data-photo-close]")).forEach(function (button) {
        button.addEventListener("click", function () { closeDialog(photoDialog); });
    });
    Array.prototype.slice.call(document.querySelectorAll("[data-publish-cancel]")).forEach(function (button) {
        button.addEventListener("click", function () { closeDialog(publishDialog); });
    });

    if (clearSlotButton) {
        clearSlotButton.addEventListener("click", function () {
            if (!currentSlot) return;
            closeDialog(candidateDialog);
            saveSlot(currentSlot.row, currentSlot.slot, null, null);
        });
    }
    if (publishButton) publishButton.addEventListener("click", openPublishConfirmation);
    if (publishConfirm) publishConfirm.addEventListener("click", publishPlan);

    if (photoDialog) {
        photoDialog.addEventListener("click", function (event) {
            if (event.target === photoDialog) closeDialog(photoDialog);
        });
    }
    if (candidateDialog) {
        candidateDialog.addEventListener("click", function (event) {
            if (event.target === candidateDialog) closeDialog(candidateDialog);
        });
    }
    if (publishDialog) {
        publishDialog.addEventListener("click", function (event) {
            if (event.target === publishDialog) closeDialog(publishDialog);
        });
    }
    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && photoDialog && photoDialog.open) closeDialog(photoDialog);
    });

    var initialSavedLabel = state.plan.updated_at_label
        ? "Сохранено · " + state.plan.updated_at_label
        : "Все изменения сохранены";
    updateAutosave("saved", initialSavedLabel);
    renderAll();
})();
