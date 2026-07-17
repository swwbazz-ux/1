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
        payload.temporary_transfer = payload.temporary_transfer || {};
        payload.categories = Array.isArray(payload.categories) ? payload.categories : [];
        payload.employees = Array.isArray(payload.employees) ? payload.employees : [];
        payload.rows = Array.isArray(payload.rows) ? payload.rows : [];
        payload.temporary_transfer.candidates = Array.isArray(payload.temporary_transfer.candidates)
            ? payload.temporary_transfer.candidates
            : [];
        payload.temporary_transfer.target_specializations = Array.isArray(payload.temporary_transfer.target_specializations)
            ? payload.temporary_transfer.target_specializations
            : [];
        payload.temporary_transfer.watch_periods = Array.isArray(payload.temporary_transfer.watch_periods)
            ? payload.temporary_transfer.watch_periods
            : [];
        return payload;
    }

    state = normalizeState(state);

    var categoryNav = root.querySelector("[data-category-nav]");
    var workDate = root.querySelector("[data-work-date]");
    var searchInput = root.querySelector("[data-planning-search]");
    var employeePool = root.querySelector("[data-employee-pool-drop]");
    var employeeList = root.querySelector("[data-employee-list]");
    var employeeEmpty = root.querySelector("[data-employee-empty]");
    var availableCount = root.querySelector("[data-available-count]");
    var board = root.querySelector("[data-assignment-board]");
    var boardEmpty = root.querySelector("[data-board-empty]");
    var filterButtons = Array.prototype.slice.call(root.querySelectorAll("[data-row-filter]"));
    var brigadeButtons = Array.prototype.slice.call(root.querySelectorAll("[data-brigade-filter]"));
    var summaryCounts = Array.prototype.slice.call(root.querySelectorAll("[data-summary-count]"));
    var autosaveState = root.querySelector("[data-autosave-state]");
    var autosaveText = root.querySelector("[data-autosave-text]");
    var exportButton = root.querySelector("[data-export-excel]");
    var publishButton = root.querySelector("[data-publish-button]");
    var temporaryTransferOpenButton = root.querySelector("[data-temporary-transfer-open]");
    var candidateDialog = document.querySelector("[data-candidate-dialog]");
    var candidateContext = document.querySelector("[data-candidate-context]");
    var candidateList = document.querySelector("[data-candidate-list]");
    var clearSlotButton = document.querySelector("[data-clear-slot]");
    var photoDialog = document.querySelector("[data-photo-dialog]");
    var photoImage = document.querySelector("[data-photo-image]");
    var photoCaption = document.querySelector("[data-photo-caption]");
    var recordDialog = document.querySelector("[data-record-dialog]");
    var recordKicker = document.querySelector("[data-record-kicker]");
    var recordTitle = document.querySelector("[data-record-title]");
    var recordSubtitle = document.querySelector("[data-record-subtitle]");
    var recordVisual = document.querySelector("[data-record-visual]");
    var recordFields = document.querySelector("[data-record-fields]");
    var publishDialog = document.querySelector("[data-publish-dialog]");
    var publishSummary = document.querySelector("[data-publish-summary]");
    var publishConfirm = document.querySelector("[data-publish-confirm]");
    var temporaryTransferDialog = document.querySelector("[data-temporary-transfer-dialog]");
    var temporaryTransferForm = document.querySelector("[data-temporary-transfer-form]");
    var temporaryTransferEmployee = document.querySelector("[data-temporary-transfer-employee]");
    var temporaryTransferSpecialization = document.querySelector("[data-temporary-transfer-specialization]");
    var temporaryTransferWatchPeriod = document.querySelector("[data-temporary-transfer-watch-period]");
    var temporaryTransferReason = document.querySelector("[data-temporary-transfer-reason]");
    var temporaryTransferSubmit = document.querySelector("[data-temporary-transfer-submit]");
    var toast = document.querySelector("[data-deputy-toast]");
    var notice = document.querySelector("[data-deputy-notice]");
    var noticeTitle = notice && notice.querySelector("[data-notice-title]");
    var noticeMessage = notice && notice.querySelector("[data-notice-message]");
    var noticeAction = notice && notice.querySelector("[data-notice-action]");
    var noticeClose = notice && notice.querySelector("[data-notice-close]");
    var currentFilter = "all";
    var currentBrigade = "all";
    var currentSlot = null;
    var saving = false;
    var toastTimer = null;
    var dragPayload = null;
    var dragPreview = null;
    var transparentDragImage = null;
    var noticeActionHandler = null;

    function textValue(value) {
        return value === null || value === undefined ? "" : String(value);
    }

    function employeeName(employee) {
        return textValue(employee && (employee.full_name || employee.name || employee.label)) || "Сотрудник";
    }

    function employeeMeta(employee) {
        return textValue(employee && (
            employee.position_label || employee.role_label || employee.position || employee.meta
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

    function normalizeSearch(value) {
        return textValue(value).normalize("NFKC").trim().replace(/\s+/g, " ")
            .toLocaleLowerCase("ru").replace(/ё/g, "е");
    }

    function employeeSearchRank(employee, rawQuery) {
        var query = normalizeSearch(rawQuery);
        if (!query) return 0;

        var name = normalizeSearch(employeeName(employee));
        var nameParts = name.split(" ").filter(Boolean);
        var surname = nameParts[0] || "";
        if (surname === query || name === query) return 0;
        if (surname.indexOf(query) === 0 || name.indexOf(query) === 0) return 1;

        for (var exactIndex = 1; exactIndex < nameParts.length; exactIndex += 1) {
            if (nameParts[exactIndex] === query) return 2 + exactIndex;
        }
        for (var prefixIndex = 1; prefixIndex < nameParts.length; prefixIndex += 1) {
            if (nameParts[prefixIndex].indexOf(query) === 0) return 5 + prefixIndex;
        }

        var queryParts = query.split(" ").filter(Boolean);
        if (queryParts.length > 1 && queryParts.every(function (queryPart) {
            return nameParts.some(function (namePart) { return namePart.indexOf(queryPart) === 0; });
        })) {
            return 10;
        }
        if (name.indexOf(query) !== -1) return 12;

        var details = normalizeSearch([employeeMeta(employee), employee && employee.phone].map(textValue).join(" "));
        return details.indexOf(query) !== -1 ? 20 : null;
    }

    function rankedEmployeeMatches(employees, query) {
        return employees.map(function (employee, index) {
            return { employee: employee, rank: employeeSearchRank(employee, query), index: index };
        }).filter(function (match) {
            return match.rank !== null;
        }).sort(function (left, right) {
            return left.rank - right.rank
                || employeeName(left.employee).localeCompare(employeeName(right.employee), "ru", { sensitivity: "base" })
                || left.index - right.index;
        });
    }

    function employeeMatchesBrigade(employee) {
        if (currentBrigade === "all") return true;
        return textValue(employee && employee.brigade_code) === currentBrigade;
    }

    function equipmentSearchRank(row, rawQuery) {
        var query = normalizeSearch(rawQuery);
        if (!query) return 0;
        var equipment = row.equipment || {};
        var label = normalizeSearch(equipment.label);
        if (label === query) return 0;
        if (label.indexOf(query) === 0) return 1;
        if (label.indexOf(query) !== -1) return 2;

        var details = normalizeSearch([equipment.model_label, equipment.status_label, row.attention]
            .map(textValue).join(" "));
        if (details.indexOf(query) !== -1) return 3;

        var equipmentParts = normalizeSearch([equipment.label, equipment.model_label, equipment.status_label, row.attention]
            .map(textValue).join(" ")).split(" ").filter(Boolean);
        var queryParts = query.split(" ").filter(Boolean);
        return queryParts.length > 1 && queryParts.every(function (queryPart) {
            return equipmentParts.some(function (equipmentPart) {
                return equipmentPart.indexOf(queryPart) === 0;
            });
        }) ? 4 : null;
    }

    function rowSearchRank(row, query) {
        var ranks = [equipmentSearchRank(row, query)];
        (row.slots || []).forEach(function (slot) {
            if (slot.employee) ranks.push(employeeSearchRank(slot.employee, query));
        });
        ranks = ranks.filter(function (rank) { return rank !== null; });
        return ranks.length ? Math.min.apply(Math, ranks) : null;
    }

    function searchContext() {
        var query = currentQuery();
        var brigadeEmployees = state.employees.filter(employeeMatchesBrigade);
        var employeeMatches = query ? rankedEmployeeMatches(brigadeEmployees, query) : [];
        var rowMatches = query ? state.rows.filter(rowMatchesFilter).map(function (row) {
            return { row: row, rank: rowSearchRank(row, query) };
        }).filter(function (match) {
            return match.rank !== null;
        }) : [];
        var bestRowRank = rowMatches.length
            ? Math.min.apply(Math, rowMatches.map(function (match) { return match.rank; }))
            : null;
        var employeeMode = Boolean(
            query
            && employeeMatches.length
            && (bestRowRank === null || employeeMatches[0].rank <= bestRowRank)
        );
        return {
            query: query,
            brigadeEmployees: brigadeEmployees,
            employeeMatches: employeeMatches,
            rowMatches: rowMatches,
            employeeMode: employeeMode
        };
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

    function temporaryTransferData() {
        return state.temporary_transfer || {};
    }

    function canRequestTemporaryTransfer() {
        var transfer = temporaryTransferData();
        return Boolean(
            state.plan.editable
            && state.endpoints.temporary_transfer_request
            && transfer.available
            && transfer.candidates.length
            && transfer.target_specializations.length
            && transfer.watch_periods.length
        );
    }

    function appendSelectOption(select, value, label) {
        if (!select) return;
        var option = document.createElement("option");
        option.value = value;
        option.textContent = label;
        select.appendChild(option);
    }

    function populateTemporaryTransferForm() {
        var transfer = temporaryTransferData();
        if (!temporaryTransferEmployee || !temporaryTransferSpecialization || !temporaryTransferWatchPeriod) return;

        temporaryTransferEmployee.innerHTML = "";
        appendSelectOption(temporaryTransferEmployee, "", "Выберите сотрудника");
        transfer.candidates.forEach(function (employee) {
            var label = employeeName(employee);
            var meta = employeeMeta(employee);
            appendSelectOption(
                temporaryTransferEmployee,
                textValue(employee.id),
                meta ? label + " · " + meta : label
            );
        });

        temporaryTransferSpecialization.innerHTML = "";
        appendSelectOption(temporaryTransferSpecialization, "", "Выберите специализацию");
        transfer.target_specializations.forEach(function (specialization) {
            appendSelectOption(
                temporaryTransferSpecialization,
                textValue(specialization.id),
                textValue(specialization.name)
            );
        });

        temporaryTransferWatchPeriod.innerHTML = "";
        appendSelectOption(temporaryTransferWatchPeriod, "", "Выберите вахту");
        transfer.watch_periods.forEach(function (period) {
            appendSelectOption(
                temporaryTransferWatchPeriod,
                textValue(period.id),
                textValue(period.label) + " · до " + textValue(period.ends_on_label)
            );
        });
        if (temporaryTransferReason) temporaryTransferReason.value = "";
    }

    function updateTemporaryTransferButton() {
        if (!temporaryTransferOpenButton) return;
        var enabled = canRequestTemporaryTransfer() && !saving;
        temporaryTransferOpenButton.disabled = !enabled;
        temporaryTransferOpenButton.classList.toggle("is-disabled", !enabled);
        temporaryTransferOpenButton.title = enabled
            ? "Запросить ОУП временный перевод сотрудника на эту вахту"
            : "Запрос доступен для текущего черновика, если есть подходящие сотрудники и действующая вахта";
    }

    function openTemporaryTransferDialog() {
        if (!canRequestTemporaryTransfer()) {
            showToast("Запрос временного перевода сейчас недоступен.", true);
            return;
        }
        populateTemporaryTransferForm();
        openDialog(temporaryTransferDialog);
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

    function hideNotice() {
        if (!notice) return;
        notice.hidden = true;
        noticeActionHandler = null;
        if (noticeAction) {
            noticeAction.hidden = true;
            noticeAction.textContent = "Обновить данные";
        }
    }

    function showNotice(message, options) {
        if (!notice) {
            showToast(message, true);
            return;
        }
        options = options || {};
        if (noticeTitle) noticeTitle.textContent = options.title || "Назначение не изменено";
        if (noticeMessage) noticeMessage.textContent = message || "Повторите действие.";
        noticeActionHandler = typeof options.action === "function" ? options.action : null;
        if (noticeAction) {
            noticeAction.hidden = !noticeActionHandler;
            noticeAction.textContent = options.actionLabel || "Обновить данные";
        }
        notice.hidden = false;
    }

    function updateAutosave(mode, message) {
        if (!autosaveState || !autosaveText) return;
        autosaveState.classList.toggle("is-saving", mode === "saving");
        autosaveState.classList.toggle("is-error", mode === "error");
        autosaveText.textContent = message;
    }

    function currentQuery() {
        return normalizeSearch(searchInput && searchInput.value);
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
            image.draggable = false;
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

    function appendRecordField(label, value) {
        value = textValue(value).trim();
        if (!recordFields || !value) return;
        var item = createElement("div", "deputy-record-field");
        item.appendChild(createElement("dt", "", label));
        item.appendChild(createElement("dd", "", value));
        recordFields.appendChild(item);
    }

    function prepareRecordDialog(kicker, title, subtitle) {
        if (!recordDialog || !recordFields || !recordVisual) return false;
        closeDialog(candidateDialog);
        closeDialog(photoDialog);
        recordVisual.replaceChildren();
        recordFields.replaceChildren();
        if (recordKicker) recordKicker.textContent = kicker;
        if (recordTitle) recordTitle.textContent = title;
        if (recordSubtitle) recordSubtitle.textContent = subtitle || "";
        return true;
    }

    function openEmployeeRecord(employee, assignmentLabel) {
        if (!employee || !prepareRecordDialog("Карточка сотрудника", employeeName(employee), employeeMeta(employee))) return;
        recordVisual.appendChild(createAvatar(employee, false));
        appendRecordField("Телефон", employee.phone || "Не указан");
        appendRecordField("Бригада", employee.brigade_label || "Не указана");
        var rotation = textValue(employee.rotation_label).trim();
        if (rotation && rotation.toLocaleLowerCase("ru") !== textValue(employee.brigade_label).toLocaleLowerCase("ru")) {
            appendRecordField("Вахта", rotation);
        }
        appendRecordField("Назначение", assignmentLabel || "Свободен");
        appendRecordField("Статус", employee.status_label || "Активен");
        openDialog(recordDialog);
    }

    function openEquipmentRecord(row) {
        var equipment = row && row.equipment || {};
        if (!prepareRecordDialog("Карточка техники", equipment.label || "Техника", equipment.model_label || equipment.type_label)) return;
        if (equipment.icon_url) {
            var image = document.createElement("img");
            image.src = equipment.icon_url;
            image.alt = "";
            recordVisual.appendChild(image);
        } else {
            recordVisual.textContent = "Т";
        }
        appendRecordField("Вид техники", equipment.type_label || "Не указан");
        appendRecordField("Модель", equipment.model_label || "Не указана");
        appendRecordField("Статус", equipment.status_label || (equipment.is_active ? "Активна в справочнике" : "Неактивна"));
        appendRecordField("Серийный номер", equipment.serial_number || "Не указан");
        appendRecordField("Принадлежность", equipment.ownership_label || "Не указана");
        (row.slots || []).forEach(function (slot) {
            appendRecordField(slot.label || "Смена", slot.employee ? employeeName(slot.employee) : "Не назначен");
        });
        openDialog(recordDialog);
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

    function moveDragPreview(event) {
        if (!dragPreview || !event || typeof event.clientX !== "number" || typeof event.clientY !== "number") return;
        var padding = 8;
        var width = dragPreview.offsetWidth || 280;
        var height = dragPreview.offsetHeight || 56;
        var left = Math.min(event.clientX - width / 2, window.innerWidth - width - padding);
        var top = Math.min(event.clientY - height / 2, window.innerHeight - height - padding);
        dragPreview.style.left = Math.max(padding, left) + "px";
        dragPreview.style.top = Math.max(padding, top) + "px";
    }

    function clearDropHighlights() {
        Array.prototype.slice.call(root.querySelectorAll(".is-drag-over")).forEach(function (node) {
            node.classList.remove("is-drag-over");
        });
    }

    function removeDragPreview() {
        if (dragPreview) {
            dragPreview.remove();
            dragPreview = null;
        }
        if (transparentDragImage) {
            transparentDragImage.remove();
            transparentDragImage = null;
        }
    }

    function applyDragPreviewTheme(preview) {
        if (!preview || !shell || typeof window.getComputedStyle !== "function") return;
        var styles = window.getComputedStyle(shell);
        [
            "--admin-panel",
            "--admin-ink",
            "--admin-line",
            "--admin-green",
            "--admin-green-soft",
            "--admin-muted"
        ].forEach(function (token) {
            var value = styles.getPropertyValue(token).trim();
            if (value) preview.style.setProperty(token, value);
        });
    }

    function finishDrag() {
        Array.prototype.slice.call(root.querySelectorAll(".is-dragging")).forEach(function (node) {
            node.classList.remove("is-dragging");
        });
        clearDropHighlights();
        removeDragPreview();
        dragPayload = null;
    }

    function createDragPreview(employee, event) {
        removeDragPreview();
        var preview = createElement("div", "deputy-drag-preview is-visible");
        preview.appendChild(createAvatar(employee, false));
        var main = createElement("span", "deputy-employee-main");
        main.appendChild(createElement("strong", "", employeeName(employee)));
        main.appendChild(createElement("small", "", employeeMeta(employee) || "Сотрудник"));
        preview.appendChild(main);
        applyDragPreviewTheme(preview);
        document.body.appendChild(preview);
        dragPreview = preview;
        moveDragPreview(event);

        transparentDragImage = createElement("span", "deputy-transparent-drag-image");
        document.body.appendChild(transparentDragImage);
        return transparentDragImage;
    }

    function bindDragSource(node, employee, visualNode) {
        var canDrag = planEditable() && !employee.disabled && !employee.busy;
        var visual = visualNode || node;
        node.draggable = canDrag;
        visual.classList.toggle("is-disabled", !canDrag && Boolean(employee.disabled || employee.busy));
        if (!canDrag) return;
        node.addEventListener("dragstart", function (event) {
            var source = sourceForEmployee(employee);
            dragPayload = {
                employeeId: employee.id,
                sourceEquipmentId: source.source_equipment_id,
                sourceShiftType: source.source_shift_type
            };
            visual.classList.add("is-dragging");
            if (event.dataTransfer) {
                event.dataTransfer.effectAllowed = "move";
                event.dataTransfer.setData("application/json", JSON.stringify(dragPayload));
                event.dataTransfer.setData("text/plain", textValue(employee.id));
                event.dataTransfer.setDragImage(createDragPreview(employee, event), 0, 0);
            }
        });
        node.addEventListener("drag", moveDragPreview);
        node.addEventListener("dragend", finishDrag);
    }

    function createEmployeeCard(employee) {
        var card = createElement("article", "deputy-employee-card");
        card.setAttribute("role", "listitem");
        card.tabIndex = 0;
        card.title = "Двойной клик — открыть карточку сотрудника";
        card.appendChild(createAvatar(employee));
        var main = createElement("span", "deputy-employee-main");
        main.appendChild(createElement("strong", "", employeeName(employee)));
        main.appendChild(createElement("small", "", employeeMeta(employee) || "Сотрудник"));
        card.appendChild(main);
        card.appendChild(createElement("span", "deputy-drag-mark", "⠿"));
        bindDragSource(card, employee);
        card.addEventListener("dblclick", function (event) {
            event.preventDefault();
            openEmployeeRecord(employee, "Свободен");
        });
        card.addEventListener("keydown", function (event) {
            if (event.key !== "Enter") return;
            event.preventDefault();
            openEmployeeRecord(employee, "Свободен");
        });
        return card;
    }

    function renderEmployees() {
        if (!employeeList) return;
        var context = searchContext();
        var visible = context.brigadeEmployees;
        if (context.query && context.employeeMode) {
            visible = context.employeeMatches.map(function (match) { return match.employee; });
        } else if (context.query && !context.rowMatches.length) {
            visible = [];
        }
        employeeList.replaceChildren();
        visible.forEach(function (employee) {
            employeeList.appendChild(createEmployeeCard(employee));
        });
        employeeList.hidden = visible.length === 0;
        if (availableCount) {
            availableCount.textContent = context.query && context.employeeMode
                ? visible.length + " из " + context.brigadeEmployees.length
                : (context.query && !context.rowMatches.length
                    ? "0 из " + context.brigadeEmployees.length
                    : textValue(context.brigadeEmployees.length));
        }
        if (employeeEmpty) {
            employeeEmpty.hidden = visible.length !== 0;
            employeeEmpty.textContent = context.query
                ? "Поиск не дал результатов."
                : (currentBrigade === "all" ? "Свободных сотрудников нет." : "В бригаде " + currentBrigade + " свободных сотрудников нет.");
        }
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
        wrapper.tabIndex = 0;
        wrapper.setAttribute("role", "button");
        wrapper.setAttribute("aria-label", "Открыть карточку техники " + textValue(equipment.label));
        wrapper.title = "Двойной клик — открыть карточку техники";
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
        wrapper.addEventListener("dblclick", function (event) {
            event.preventDefault();
            openEquipmentRecord(row);
        });
        wrapper.addEventListener("keydown", function (event) {
            if (event.key !== "Enter") return;
            event.preventDefault();
            openEquipmentRecord(row);
        });
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
        var candidates = Array.from(candidatesById.values()).filter(employeeMatchesBrigade).sort(function (left, right) {
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
            candidateList.appendChild(createElement(
                "p",
                "deputy-empty-state",
                currentBrigade === "all" ? "Свободных сотрудников нет." : "В бригаде " + currentBrigade + " подходящих сотрудников нет."
            ));
        }
        if (clearSlotButton) clearSlotButton.hidden = !slot.employee;
        openDialog(candidateDialog);
    }

    function dropPayloadFromEvent(event) {
        var payload = dragPayload;
        if (!payload && !event.dataTransfer) return null;
        try {
            payload = payload || JSON.parse(event.dataTransfer.getData("application/json") || "null");
        } catch (error) {
            var employeeId = event.dataTransfer.getData("text/plain");
            payload = employeeId ? { employeeId: employeeId } : null;
        }
        if (!payload) return null;
        return {
            employeeId: payload.employeeId || payload.employee_id || null,
            sourceEquipmentId: payload.sourceEquipmentId || payload.source_equipment_id || null,
            sourceShiftType: payload.sourceShiftType || payload.source_shift_type || null
        };
    }

    function findSlot(equipmentId, shiftType) {
        var matched = null;
        state.rows.some(function (row) {
            if (textValue(row.equipment && row.equipment.id) !== textValue(equipmentId)) return false;
            var slot = (row.slots || []).find(function (candidate) {
                return textValue(candidate.shift_type) === textValue(shiftType);
            });
            if (!slot) return false;
            matched = { row: row, slot: slot };
            return true;
        });
        return matched;
    }

    function createSlot(row, slot) {
        var button = createElement("div", "deputy-slot");
        button.classList.toggle("is-disabled", !planEditable());
        button.classList.toggle("has-employee", Boolean(slot.employee));
        button.classList.toggle("has-conflict", Boolean(slot.conflict));

        if (slot.employee) {
            button.title = "Двойной клик — открыть карточку сотрудника";
            button.appendChild(createAvatar(slot.employee));
            var person = createElement("button", "deputy-slot-person");
            person.type = "button";
            person.setAttribute("aria-label", "Открыть карточку сотрудника: " + employeeName(slot.employee));
            var main = createElement("span", "deputy-employee-main");
            main.appendChild(createElement("strong", "", employeeName(slot.employee)));
            main.appendChild(createElement("small", "", employeeMeta(slot.employee) || slot.label || "Назначен"));
            person.appendChild(main);
            var flags = createElement("span", "deputy-slot-flags");
            if (slot.changed) flags.appendChild(createElement("span", "deputy-slot-flag", "ИЗМ"));
            if (slot.conflict) flags.appendChild(createElement("span", "deputy-slot-flag is-conflict", "!"));
            person.appendChild(flags);
            button.appendChild(person);
            bindDragSource(person, Object.assign({}, slot.employee, {
                source_equipment_id: row.equipment && row.equipment.id,
                source_shift_type: slot.shift_type
            }), button);
            person.title = "Двойной клик — открыть карточку сотрудника";
            person.addEventListener("click", function (event) {
                if (event.detail === 0) {
                    openEmployeeRecord(slot.employee, [row.equipment && row.equipment.label, slot.label].filter(Boolean).join(" · "));
                }
            });
            person.addEventListener("dblclick", function (event) {
                event.preventDefault();
                event.stopPropagation();
                openEmployeeRecord(slot.employee, [row.equipment && row.equipment.label, slot.label].filter(Boolean).join(" · "));
            });
            if (planEditable()) {
                var editButton = createElement("button", "deputy-slot-edit", "Сменить");
                editButton.type = "button";
                editButton.setAttribute("aria-label", "Изменить назначение: " + textValue(slot.label));
                editButton.addEventListener("click", function (event) {
                    event.preventDefault();
                    event.stopPropagation();
                    openCandidatePicker(row, slot);
                });
                button.appendChild(editButton);
            }
            button.addEventListener("dblclick", function (event) {
                event.preventDefault();
                openEmployeeRecord(slot.employee, [row.equipment && row.equipment.label, slot.label].filter(Boolean).join(" · "));
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
            if (
                textValue(payload.sourceEquipmentId) === textValue(row.equipment && row.equipment.id)
                && textValue(payload.sourceShiftType) === textValue(slot.shift_type)
            ) {
                return;
            }
            saveSlot(row, slot, payload.employeeId, {
                source_equipment_id: payload.sourceEquipmentId,
                source_shift_type: payload.sourceShiftType
            });
        });
        return button;
    }

    function renderBoard() {
        if (!board) return;
        var context = searchContext();
        var rows = state.rows.filter(rowMatchesFilter);
        if (context.query && !context.employeeMode) {
            rows = rows.filter(function (row) { return rowSearchRank(row, context.query) !== null; });
        }
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
        if (exportButton) {
            var canExport = Boolean(state.plan.id && state.endpoints.export && !saving);
            exportButton.href = canExport ? state.endpoints.export : "#";
            exportButton.classList.toggle("is-disabled", !canExport);
            exportButton.setAttribute("aria-disabled", canExport ? "false" : "true");
            exportButton.tabIndex = canExport ? 0 : -1;
            exportButton.title = canExport
                ? "Скачать Excel для печати"
                : (saving ? "Дождитесь завершения сохранения" : "Экспорт недоступен");
        }
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
        updateTemporaryTransferButton();
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
            requestError.code = result.code || "";
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
            hideNotice();
            var savedLabel = state.plan.updated_at_label ? "Сохранено · " + state.plan.updated_at_label : "Все изменения сохранены";
            updateAutosave("saved", savedLabel);
        } catch (error) {
            if (error.payload) applyPayload(error.payload);
            updateAutosave("error", "Не сохранено");
            var shouldReload = ["stale_version", "stale_baseline", "plan_work_date_closed"].indexOf(error.code) !== -1;
            var title = error.code === "plan_work_date_closed"
                ? "Производственные сутки завершены"
                : (shouldReload ? "Данные изменились" : "Назначение не изменено");
            showNotice(error.message, {
                title: title,
                actionLabel: shouldReload ? "Обновить данные" : "",
                action: shouldReload ? function () { window.location.reload(); } : null
            });
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
                ? "Останутся незаполненные слоты: " + unfilled + ". Карточки сотрудников будут обновлены по текущей расстановке."
                : "После публикации назначения и карточки сотрудников будут обновлены.";
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
            hideNotice();
            updateAutosave("saved", "Расстановка опубликована");
            showToast("Расстановка опубликована. Карточки сотрудников обновлены.", false);
        } catch (error) {
            if (error.payload) applyPayload(error.payload);
            updateAutosave("error", "Не опубликовано");
            var shouldReload = ["stale_version", "stale_baseline", "plan_work_date_closed"].indexOf(error.code) !== -1;
            showNotice(error.message, {
                title: shouldReload ? "Данные изменились" : "Расстановка не опубликована",
                actionLabel: shouldReload ? "Обновить данные" : "",
                action: shouldReload ? function () { window.location.reload(); } : null
            });
        } finally {
            saving = false;
            updatePublishButton();
        }
    }

    async function submitTemporaryTransferRequest(event) {
        event.preventDefault();
        if (saving || !canRequestTemporaryTransfer()) return;
        var employeeId = temporaryTransferEmployee && temporaryTransferEmployee.value;
        var specializationId = temporaryTransferSpecialization && temporaryTransferSpecialization.value;
        var watchPeriodId = temporaryTransferWatchPeriod && temporaryTransferWatchPeriod.value;
        if (!employeeId || !specializationId || !watchPeriodId) {
            showToast("Выберите сотрудника, специализацию и вахту.", true);
            return;
        }
        saving = true;
        if (temporaryTransferSubmit) temporaryTransferSubmit.disabled = true;
        updateTemporaryTransferButton();
        try {
            var payload = await postJson(state.endpoints.temporary_transfer_request, {
                plan_id: state.plan.id,
                employee_id: Number(employeeId),
                target_specialization_id: Number(specializationId),
                watch_period_id: Number(watchPeriodId),
                reason: temporaryTransferReason ? temporaryTransferReason.value.trim() : ""
            });
            applyPayload(payload);
            closeDialog(temporaryTransferDialog);
            showToast("Запрос передан в ОУП. После одобрения сотрудник появится в списке для расстановки.", false);
        } catch (error) {
            showToast(error.message || "Не удалось отправить запрос в ОУП.", true);
        } finally {
            saving = false;
            if (temporaryTransferSubmit) temporaryTransferSubmit.disabled = false;
            updateTemporaryTransferButton();
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

    brigadeButtons.forEach(function (button) {
        button.addEventListener("click", function () {
            currentBrigade = button.getAttribute("data-brigade-filter") || "all";
            brigadeButtons.forEach(function (item) {
                var active = item === button;
                item.classList.toggle("is-active", active);
                item.setAttribute("aria-pressed", active ? "true" : "false");
            });
            closeDialog(candidateDialog);
            renderEmployees();
        });
    });

    if (searchInput) {
        searchInput.addEventListener("input", function () {
            renderEmployees();
            renderBoard();
        });
    }

    if (employeePool) {
        employeePool.addEventListener("dragover", function (event) {
            var payload = dragPayload;
            if (
                !planEditable()
                || saving
                || !payload
                || !payload.sourceEquipmentId
                || !payload.sourceShiftType
            ) return;
            event.preventDefault();
            employeePool.classList.add("is-drag-over");
            if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
            moveDragPreview(event);
        });
        employeePool.addEventListener("dragleave", function (event) {
            if (!event.relatedTarget || !employeePool.contains(event.relatedTarget)) {
                employeePool.classList.remove("is-drag-over");
            }
        });
        employeePool.addEventListener("drop", function (event) {
            if (!planEditable() || saving) return;
            var payload = dropPayloadFromEvent(event);
            if (!payload || !payload.sourceEquipmentId || !payload.sourceShiftType) return;
            event.preventDefault();
            employeePool.classList.remove("is-drag-over");
            var source = findSlot(payload.sourceEquipmentId, payload.sourceShiftType);
            if (!source) {
                showNotice("Исходное назначение не найдено. Обновите данные и повторите действие.", {
                    title: "Назначение не изменено",
                    actionLabel: "Обновить данные",
                    action: function () { window.location.reload(); }
                });
                return;
            }
            saveSlot(source.row, source.slot, null, null);
        });
    }

    Array.prototype.slice.call(document.querySelectorAll("[data-dialog-close]")).forEach(function (button) {
        button.addEventListener("click", function () { closeDialog(candidateDialog); });
    });
    Array.prototype.slice.call(document.querySelectorAll("[data-photo-close]")).forEach(function (button) {
        button.addEventListener("click", function () { closeDialog(photoDialog); });
    });
    Array.prototype.slice.call(document.querySelectorAll("[data-record-close]")).forEach(function (button) {
        button.addEventListener("click", function () { closeDialog(recordDialog); });
    });
    Array.prototype.slice.call(document.querySelectorAll("[data-publish-cancel]")).forEach(function (button) {
        button.addEventListener("click", function () { closeDialog(publishDialog); });
    });
    Array.prototype.slice.call(document.querySelectorAll("[data-temporary-transfer-cancel]")).forEach(function (button) {
        button.addEventListener("click", function () { closeDialog(temporaryTransferDialog); });
    });

    if (clearSlotButton) {
        clearSlotButton.addEventListener("click", function () {
            if (!currentSlot) return;
            closeDialog(candidateDialog);
            saveSlot(currentSlot.row, currentSlot.slot, null, null);
        });
    }
    if (publishButton) publishButton.addEventListener("click", openPublishConfirmation);
    if (temporaryTransferOpenButton) {
        temporaryTransferOpenButton.addEventListener("click", openTemporaryTransferDialog);
    }
    if (temporaryTransferForm) {
        temporaryTransferForm.addEventListener("submit", submitTemporaryTransferRequest);
    }
    if (exportButton) {
        exportButton.addEventListener("click", function (event) {
            if (!saving && state.plan.id && state.endpoints.export) return;
            event.preventDefault();
            if (saving) showToast("Дождитесь завершения сохранения.", false);
        });
    }
    if (publishConfirm) publishConfirm.addEventListener("click", publishPlan);
    if (noticeClose) noticeClose.addEventListener("click", hideNotice);
    if (noticeAction) {
        noticeAction.addEventListener("click", function () {
            var handler = noticeActionHandler;
            hideNotice();
            if (handler) handler();
        });
    }

    if (photoDialog) {
        photoDialog.addEventListener("click", function (event) {
            if (event.target === photoDialog) closeDialog(photoDialog);
        });
    }
    if (recordDialog) {
        recordDialog.addEventListener("click", function (event) {
            if (event.target === recordDialog) closeDialog(recordDialog);
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
    if (temporaryTransferDialog) {
        temporaryTransferDialog.addEventListener("click", function (event) {
            if (event.target === temporaryTransferDialog) closeDialog(temporaryTransferDialog);
        });
    }
    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && photoDialog && photoDialog.open) closeDialog(photoDialog);
        if (event.key === "Escape" && notice && !notice.hidden) hideNotice();
    });
    document.addEventListener("dragover", moveDragPreview);
    document.addEventListener("drop", finishDrag);

    var initialSavedLabel = state.plan.updated_at_label
        ? "Сохранено · " + state.plan.updated_at_label
        : "Все изменения сохранены";
    updateAutosave("saved", initialSavedLabel);
    renderAll();
})();
