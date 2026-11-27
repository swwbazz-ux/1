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

    var nameOrder = new Intl.Collator("ru", { sensitivity: "base" });

    function normalizeState(payload) {
        payload = payload && typeof payload === "object" ? payload : {};
        payload.plan = payload.plan || {};
        payload.role = payload.role || {};
        payload.endpoints = payload.endpoints || {};
        payload.summary = payload.summary || {};
        payload.temporary_transfer = payload.temporary_transfer || {};
        payload.categories = Array.isArray(payload.categories) ? payload.categories : [];
        payload.employees = Array.isArray(payload.employees) ? payload.employees : [];
        payload.factual_candidates = Array.isArray(payload.factual_candidates) ? payload.factual_candidates : [];
        payload.employees.forEach(function (employee) {
            employee.eligible_positions = Array.isArray(employee.eligible_positions)
                ? employee.eligible_positions
                : ["primary"];
        });
        var otherWatchEmployees = Array.isArray(payload.other_watch_employees) ? payload.other_watch_employees : [];
        payload.allWatchEmployees = payload.employees.concat(otherWatchEmployees).sort(function (left, right) {
            return nameOrder.compare(employeeName(left), employeeName(right));
        });
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
    var candidateSearchInput = document.querySelector("[data-candidate-search]");
    var candidateSearchClear = document.querySelector("[data-candidate-search-clear]");
    var candidateCount = document.querySelector("[data-candidate-count]");
    var candidateList = document.querySelector("[data-candidate-list]");
    var clearSlotButton = document.querySelector("[data-clear-slot]");
    var factualCandidatesControl = document.querySelector("[data-factual-candidates-control]");
    var factualCandidates = document.querySelector("[data-factual-candidates]");
    var photoDialog = document.querySelector("[data-photo-dialog]");
    var photoImage = document.querySelector("[data-photo-image]");
    var photoCaption = document.querySelector("[data-photo-caption]");
    var recordDialog = document.querySelector("[data-record-dialog]");
    var recordKicker = document.querySelector("[data-record-kicker]");
    var recordTitle = document.querySelector("[data-record-title]");
    var recordSubtitle = document.querySelector("[data-record-subtitle]");
    var recordVisual = document.querySelector("[data-record-visual]");
    var recordAssignment = document.querySelector("[data-record-assignment]");
    var recordFields = document.querySelector("[data-record-fields]");
    var recordActions = document.querySelector("[data-record-actions]");
    var recordDuty = document.querySelector("[data-record-duty]");
    var recordDutyForm = document.querySelector("[data-record-duty-form]");
    var recordDutyRemoval = document.querySelector("[data-record-duty-removal]");
    var recordDutyError = document.querySelector("[data-record-duty-error]");
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
    var categoryRequest = null;
    var peopleSearch = null;
    var pausedPanels = [];
    var currentFilter = "all";
    var currentBrigade = state.brigades && state.brigades.length === 2 ? "watch" : "all";
    var currentSlot = null;
    var candidateEmployees = [];
    var equipmentRowNodes = new Map();
    var employeeCardNodes = new Map();
    var rosterExpiryTimer;
    var nextRosterExpiry = Infinity;
    var boardBody = null;
    var activeEquipmentId = "";
    var dragSourceEquipmentId = "";
    var dragTargetEquipmentId = "";
    var saving = false;
    var toastTimer = null;
    var dragPayload = null;
    var dragPreview = null;
    var dragPreviewFrame = null;
    var dragPreviewPoint = null;
    var dragPreviewWidth = 0;
    var dragPreviewHeight = 0;
    var transparentDragImage = null;
    var noticeActionHandler = null;

    function textValue(value) {
        return value === null || value === undefined ? "" : String(value);
    }

    function employeeName(employee) {
        return textValue(employee && (employee.full_name || employee.name || employee.label)) || "Сотрудник";
    }

    function employeeMeta(employee) {
        var position = textValue(employee && (
            employee.position_label || employee.role_label || employee.position || employee.meta
        ));
        var contractor = textValue(employee && employee.contractor_label);
        var until = textValue(employee && employee.contractor_access_until_label);
        if (!contractor) return position;
        return [position, "Подрядчик: " + contractor + (until ? " · допуск по " + until : "")]
            .filter(Boolean)
            .join(" · ");
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

    function employeePresence(employee) {
        var presence = employee && employee.presence && typeof employee.presence === "object"
            ? employee.presence
            : {};
        var status = textValue(presence.status_code || presence.status) || "not_registered";
        return {
            status: status,
            label: textValue(presence.status_label || presence.label) || "Не подключался",
            lastSeenTime: textValue(presence.last_seen_time),
            clientBadges: Array.isArray(presence.client_badges) ? presence.client_badges : []
        };
    }

    function createPresenceBadge(employee, showClients) {
        var presence = employeePresence(employee);
        var wrapper = createElement("span", "application-presence is-compact deputy-presence");
        wrapper.setAttribute("data-application-presence", presence.status);
        wrapper.setAttribute("aria-label", "Приложение: " + presence.label);
        wrapper.setAttribute("title", "Приложение: " + presence.label);

        var line = createElement("span", "application-presence__line");
        line.appendChild(createElement("i", "application-presence__dot is-" + presence.status));
        line.appendChild(createElement("b", "is-" + presence.status, presence.label));
        if (presence.lastSeenTime) {
            line.appendChild(createElement("time", "", presence.lastSeenTime));
        }
        wrapper.appendChild(line);

        if (showClients !== false && presence.clientBadges.length) {
            var clients = createElement(
                "small",
                "application-presence__clients admin-live-client-badges"
            );
            clients.setAttribute("aria-label", "Вариант приложения");
            presence.clientBadges.forEach(function (client) {
                var label = textValue(client.label) || "Приложение";
                if (presence.clientBadges.length > 1 && client.app_label) {
                    label = textValue(client.app_label) + " · " + label;
                }
                if (client.version) label += " · " + textValue(client.version);
                clients.appendChild(createElement(
                    "em",
                    "application-presence__client admin-live-client-badge is-" + textValue(client.kind),
                    label
                ));
            });
            wrapper.appendChild(clients);
        }
        return wrapper;
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
                || nameOrder.compare(employeeName(left.employee), employeeName(right.employee))
                || left.index - right.index;
        });
    }

    function activeRosterExtensionUntil(employee) {
        return Date.parse(employee.extension_expires_at) <= Date.now() ? "" : employee.extension_until;
    }

    function inDeputyRoster(employee) {
        var brigades = state.brigades || [];
        return brigades.length !== 2 || Boolean(activeRosterExtensionUntil(employee)) || brigades.includes(employee.brigade_code);
    }

    function appendExtensionBadge(container, extensionUntil) {
        if (extensionUntil) {
            container.appendChild(createElement("span", "deputy-extension-badge", "На продлении до " + extensionUntil));
        }
    }

    function employeeMatchesBrigade(employee) {
        if (currentBrigade === "all") return Boolean(employee.brigade_code) || inDeputyRoster(employee);
        if (!inDeputyRoster(employee)) return false;
        if (currentBrigade === "watch") return true;
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
            if (slot.reported_name) ranks.push(employeeSearchRank({full_name: slot.reported_name}, query));
            if (slot.secondary_employee) ranks.push(employeeSearchRank(slot.secondary_employee, query));
        });
        ranks = ranks.filter(function (rank) { return rank !== null; });
        return ranks.length ? Math.min.apply(Math, ranks) : null;
    }

    function personMatchesSearch(employee, query) {
        return employee && peopleSearch.matches(employeeName(employee), query);
    }

    function crewMatchesSearch(slot, query) {
        return personMatchesSearch(slot.employee, query)
            || personMatchesSearch(slot.secondary_employee, query)
            || (slot.reported_name && peopleSearch.matches(slot.reported_name, query));
    }

    function searchContext() {
        var query = currentQuery();
        var source = currentBrigade === "all" ? state.allWatchEmployees : state.employees;
        var brigadeEmployees = source.filter(employeeMatchesBrigade);
        var hit = peopleSearch && peopleSearch.target();
        if (hit && hit.place.role === state.role.code) {
            var free = state.allWatchEmployees.find(function (employee) {
                return employee.id === hit.person.id && (inDeputyRoster(employee) || employee.brigade_code);
            });
            return {
                query: query, brigadeEmployees: brigadeEmployees, employeeMode: !hit.place.equipment,
                employeeMatches: free ? [{employee: free}] : [],
                rowMatches: state.rows.filter(function (row) {
                    return row.equipment.id === hit.place.equipment;
                }).map(function (row) { return {row: row}; })
            };
        }
        if (query && peopleSearch) {
            var matchingFree = brigadeEmployees.filter(function (employee) {
                return personMatchesSearch(employee, query);
            });
            var matchingRows = state.rows.filter(rowMatchesFilter).filter(function (row) {
                return equipmentSearchRank(row, query) !== null || row.slots.some(function (slot) {
                    return crewMatchesSearch(slot, query);
                });
            });
            return {
                query: query, brigadeEmployees: brigadeEmployees,
                employeeMode: matchingFree.length > 0,
                employeeMatches: matchingFree.map(function (employee) { return {employee: employee}; }),
                rowMatches: matchingRows.map(function (row) { return {row: row}; })
            };
        }
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

    function closeDialog(dialog, options) {
        if (!dialog) return;
        var preserveEquipmentActive = Boolean(options && options.preserveEquipmentActive);
        var candidateWasOpen = dialog === candidateDialog && Boolean(dialog.open || dialog.hasAttribute("open"));
        if (dialog === candidateDialog) {
            if (preserveEquipmentActive) {
                dialog.setAttribute("data-preserve-equipment-active", "true");
            } else {
                dialog.removeAttribute("data-preserve-equipment-active");
            }
        }
        if (typeof dialog.close === "function" && dialog.open) {
            dialog.close();
        } else {
            dialog.removeAttribute("open");
        }
        if (candidateWasOpen && !preserveEquipmentActive) clearActiveEquipmentRow();
    }

    function equipmentRowId(row) {
        return textValue(row && row.equipment && row.equipment.id);
    }

    function syncEquipmentRowState(rowId) {
        var entry = equipmentRowNodes.get(rowId);
        if (!entry) return;
        var rowNode = entry.node;
        var isActive = rowId === activeEquipmentId;
        var isDragSource = rowId === dragSourceEquipmentId;
        var isDropTarget = rowId === dragTargetEquipmentId;
        var interactionLabel = isDropTarget ? "Сюда" : (isDragSource ? "Отсюда" : (isActive ? "Выбрано" : ""));
        var stateBadge = rowNode.querySelector(".deputy-equipment-state");
        rowNode.classList.toggle("is-active-equipment", isActive);
        rowNode.classList.toggle("is-drag-source", isDragSource);
        rowNode.classList.toggle("is-drop-target", isDropTarget);
        if (isActive) {
            rowNode.setAttribute("aria-current", "true");
        } else {
            rowNode.removeAttribute("aria-current");
        }
        if (stateBadge) {
            stateBadge.textContent = interactionLabel || "Выбрано";
            stateBadge.classList.toggle("is-visible", Boolean(interactionLabel));
        }
    }

    function setActiveEquipmentRow(row) {
        var nextId = equipmentRowId(row);
        var previousId = activeEquipmentId;
        if (previousId === nextId) return;
        activeEquipmentId = nextId;
        syncEquipmentRowState(previousId);
        syncEquipmentRowState(nextId);
    }

    function clearActiveEquipmentRow() {
        setActiveEquipmentRow(null);
    }

    function setDragTargetEquipmentRow(row) {
        var nextId = equipmentRowId(row);
        var previousId = dragTargetEquipmentId;
        if (previousId === nextId) return;
        dragTargetEquipmentId = nextId;
        syncEquipmentRowState(previousId);
        syncEquipmentRowState(nextId);
    }

    function clearDragTargetEquipmentRow(row) {
        if (row && dragTargetEquipmentId !== equipmentRowId(row)) return;
        setDragTargetEquipmentRow(null);
    }

    function temporaryTransferData() {
        return state.temporary_transfer || {};
    }

    function canRequestTemporaryTransfer() {
        var transfer = temporaryTransferData();
        return Boolean(
            mutationAllowed()
            &&
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

    function contractAllowsMutation() {
        var guard = window.AppPwaContractGuard;
        if (!guard || typeof guard.getState !== "function") return false;
        var contractState = guard.getState();
        return Boolean(contractState && contractState.locked === false);
    }

    function roleAllowsMutation() {
        return !(
            typeof window.isAppRoleReadonly === "function"
            && window.isAppRoleReadonly()
        );
    }

    function mutationAllowed() {
        return contractAllowsMutation() && roleAllowsMutation();
    }

    function contractLockedError() {
        var error = new Error(
            "Рабочее приложение обновляется. Изменения временно заблокированы."
        );
        error.code = "app_contract_locked";
        return error;
    }

    function planEditable() {
        return Boolean(
            mutationAllowed()
            && state.plan.editable
            && state.endpoints.slot
        );
    }

    function sourceForEmployee(employee) {
        var assignment = employee && employee.assignment && typeof employee.assignment === "object" ? employee.assignment : {};
        return {
            source_equipment_id: employee && (employee.source_equipment_id || assignment.equipment_id) || null,
            source_shift_type: employee && (employee.source_shift_type || assignment.shift_type) || null,
            source_position: employee && employee.source_position || null
        };
    }

    function employeeCanFillPosition(employee, position) {
        var positions = employee && Array.isArray(employee.eligible_positions)
            ? employee.eligible_positions
            : ["primary"];
        return positions.indexOf(position) !== -1;
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

    function appendRecordField(label, value, modifier) {
        value = textValue(value).trim();
        if (!recordFields || !value) return;
        var item = createElement("div", "deputy-record-field" + (modifier ? " " + modifier : ""));
        item.appendChild(createElement("dt", "", label));
        item.appendChild(createElement("dd", "", value));
        recordFields.appendChild(item);
        return item.lastElementChild;
    }

    function prepareRecordDialog(kicker, title, subtitle) {
        if (!recordDialog || !recordFields || !recordVisual) return false;
        closeDialog(candidateDialog);
        closeDialog(photoDialog);
        recordDialog.classList.remove("is-specialization", "is-employee");
        recordAssignment.hidden = true;
        if (recordDuty) {
            recordDuty.hidden = true;
            recordDuty.open = false;
        }
        recordVisual.replaceChildren();
        recordFields.replaceChildren();
        if (recordActions) {
            recordActions.querySelectorAll("[data-review-action]").forEach(function (action) { action.remove(); });
        }
        if (recordKicker) recordKicker.textContent = kicker;
        if (recordTitle) recordTitle.textContent = title;
        if (recordSubtitle) recordSubtitle.textContent = subtitle || "";
        return true;
    }

    function updateRecordDutyFields() {
        var fields = recordDutyForm.elements;
        var status = fields.status.value;
        recordDutyForm.querySelector("[data-record-duty-inputs]").hidden = !status;
        fields.shift_type.required = status === "reserve";
        fields.location.required = status === "other_work";
        recordDutyForm.querySelector("[data-duty-shift-hint]").textContent = fields.shift_type.required ? "обязательно" : "можно уточнить позже";
        recordDutyForm.querySelector("[data-duty-location-hint]").textContent = fields.location.required ? "обязательно" : "по желанию";
        recordDutyError.hidden = true;
    }

    function prepareRecordDuty(employeeId, shiftType) {
        if (!recordDutyForm || !employeeId || !planEditable() || !state.endpoints.off_equipment_transfer) return;
        recordDutyForm.reset();
        recordDutyForm.dataset.employeeId = employeeId;
        recordDutyForm.dataset.planId = state.plan.id;
        recordDutyForm.dataset.planVersion = state.plan.version;
        recordDutyForm.dataset.workDate = state.plan.work_date;
        recordDutyForm.elements.shift_type.value = shiftType || "";
        recordDutyRemoval.hidden = !shiftType;
        updateRecordDutyFields();
        recordDuty.hidden = false;
    }

    function openEmployeeRecord(employee, assignmentLabel, shiftType) {
        if (!employee || !prepareRecordDialog("Карточка сотрудника", employeeName(employee), employeeMeta(employee))) return;
        recordDialog.classList.add("is-employee");
        recordVisual.appendChild(createAvatar(employee, false));
        recordAssignment.hidden = false;
        var date = recordAssignment.querySelector("time");
        date.dateTime = state.plan.work_date;
        date.textContent = state.plan.work_date_label || state.plan.work_date;
        recordAssignment.querySelector("[data-record-posting]").textContent = assignmentLabel || "Свободен";
        recordAssignment.querySelector("[data-record-brigade]").textContent = employee.brigade_label || "Не указана";
        var rotation = textValue(employee.rotation_label).trim();
        if (rotation.toLocaleLowerCase("ru") === textValue(employee.brigade_label).trim().toLocaleLowerCase("ru")) rotation = "";
        appendRecordField("График работы", rotation || "Не указан");
        var phone = appendRecordField("Телефон", employee.phone || "Не указан");
        if (employee.phone && /\d/.test(employee.phone)) {
            var link = createElement("a", "", employee.phone);
            link.href = "tel:" + employee.phone.replace(/[^\d+]/g, "");
            phone.replaceChildren(link);
        }
        appendRecordField("Кадровый статус", employee.status_label || "Активен");
        var presence = employeePresence(employee);
        var connection = appendRecordField("Связь с приложением", presence.label);
        if (presence.clientBadges.length) {
            connection.appendChild(createElement("small", "deputy-record-clients",
                presence.clientBadges.map(function (client) {
                    return (textValue(client.label) || "Приложение")
                        + (client.version ? " · " + textValue(client.version) : "");
                }).join(", ")
            ));
        }
        if (employee.can_change_off_equipment && inDeputyRoster(employee)) prepareRecordDuty(employee.id, shiftType);
        openDialog(recordDialog);
    }

    function addReviewAction(label, callback, primary) {
        if (!recordActions) return;
        var action = createElement("button", "admin-button " + (primary ? "admin-button-primary" : "admin-button-secondary"), label);
        action.type = "button";
        action.setAttribute("data-review-action", "");
        action.addEventListener("click", function () {
            if (!planEditable() || saving) return;
            closeDialog(recordDialog);
            callback();
        });
        recordActions.insertBefore(action, recordActions.lastElementChild);
    }

    function choicesForCrewPosition(employee, position) {
        return (employee.specialization_choices || []).filter(function (choice) {
            return choice.positions.indexOf(position) !== -1;
        });
    }

    function openSpecializationChoice(employee, target, choices) {
        var context = [target.row.equipment.label, target.slot.label].join(" · ");
        if (!prepareRecordDialog("Специализация", employeeName(employee), context)) return;
        recordDialog.classList.add("is-specialization");
        recordVisual.appendChild(createAvatar(employee, false));
        appendRecordField("Кадровая должность", employee.position_label);
        appendRecordField("Производственная специализация", "Выбранная специализация сохранится в карточке сотрудника для следующих смен.", "is-summary");
        choices.forEach(function (choice) {
            addReviewAction("Сохранить «" + choice.name + "» и назначить", function () {
                setActiveEquipmentRow(target.row);
                saveSlot(target.row, target.slot, employee.id, null, target.position, false, choice.id);
            }, choices.length === 1);
        });
        if (target.position === "primary") {
            addReviewAction("Назначить на смену без сохранения специализации", function () {
                setActiveEquipmentRow(target.row);
                saveSlot(target.row, target.slot, employee.id, null, target.position, true);
            });
        }
        openDialog(recordDialog);
    }

    function openSpecializationReview(row, slot) {
        var review = slot.specialization_review || {};
        var choices = choicesForCrewPosition(review, "primary");
        var equipment = row.equipment || {};
        var context = [equipment.type_label, equipment.label, slot.label].filter(Boolean).join(" · ");
        if (!prepareRecordDialog("Специализация", slot.reported_name, context)) return;
        recordDialog.classList.add("is-specialization");
        recordVisual.appendChild(createAvatar({full_name: slot.reported_name}, false));

        var required = review.required || state.role.label;
        if (review.warning) {
            var reason = review.current
                ? "Действующая специализация не подходит для этого места."
                : "В карточке сотрудника не сохранена производственная специализация.";
            appendRecordField("Почему появилась пометка", reason + " Для этой техники требуется «" + required + "».", "is-summary is-warning");
        } else {
            appendRecordField("Результат проверки", review.issue || (review.employee_id
                ? "Сотрудник подходит для этого места." : "Требуется сверка карточки сотрудника."), "is-summary");
        }
        appendRecordField("Штатная должность", review.position || "Не указана");
        var savedSpecialization = review.base === undefined ? review.current : review.base;
        appendRecordField("Специализация в карточке", savedSpecialization || "Не указана");
        if (review.current && review.current !== savedSpecialization) {
            appendRecordField("Действующая специализация", review.current);
        }
        if (review.suggested) appendRecordField("По умолчанию для должности", review.suggested);
        if (!review.warning) appendRecordField("Требуется для техники", required);
        if (review.warning) {
            var help = choices.length
                ? "Если это постоянная работа сотрудника, сохраните специализацию в его карточке. Для подмены можно оставить текущее назначение."
                : "Если сотрудник действительно работает на этой машине, оставьте его в смене. Расстановку с этой пометкой можно сохранить и опубликовать. Чтобы убрать пометку, попросите ОУП проверить карточку и указать подходящую специализацию.";
            appendRecordField("Что делать", help, "is-summary");
        }
        if (planEditable()) {
            if (review.can_assign) {
                addReviewAction("Назначить сотрудника", function () {
                    saveSlot(row, slot, review.employee_id, null, "primary");
                });
            } else if (choices.length) {
                choices.forEach(function (choice) {
                    addReviewAction("Сохранить «" + choice.name + "» и назначить", function () {
                        saveSlot(row, slot, review.employee_id, null, "primary", false, choice.id);
                    }, choices.length === 1);
                });
            } else if (canRequestTemporaryTransfer() && temporaryTransferData().candidates.some(function (employee) {
                return employee.id === review.employee_id;
            })) {
                addReviewAction("Запросить перевод", function () {
                    openTemporaryTransferDialog();
                    temporaryTransferEmployee.value = textValue(review.employee_id);
                    if (temporaryTransferData().target_specializations.length === 1) {
                        temporaryTransferSpecialization.value = textValue(temporaryTransferData().target_specializations[0].id);
                    }
                });
            }
            addReviewAction("Заменить сотрудника", function () {
                openCandidatePicker(row, slot, "primary");
            });
        }
        if (review.can_change_off_equipment) prepareRecordDuty(review.employee_id, slot.shift_type);
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
            appendRecordField(slot.label || "Смена", slot.employee ? employeeName(slot.employee) : (slot.reported_name ? slot.reported_name + (slot.specialization_review && slot.specialization_review.warning ? " · Специализация отличается" : "") : "Не назначен"));
            if (state.role.supports_secondary === false) return;
            appendRecordField(
                (slot.label || "Смена") + " · " + (slot.secondary_label || state.secondary_employee_label || "Дополнительный участник"),
                slot.secondary_employee ? employeeName(slot.secondary_employee) : "Не назначен"
            );
        });
        openDialog(recordDialog);
    }

    function pauseCategoryControls() {
        if (pausedPanels.length) return;
        var toolbar = categoryNav.parentElement;
        var panels = Array.from(root.children).concat(Array.from(toolbar.children));
        pausedPanels = panels.filter(function (panel) {
            return panel !== toolbar && panel !== categoryNav && panel !== searchInput?.parentElement && !panel.inert;
        });
        pausedPanels.forEach(function (panel) { panel.inert = true; });
        root.setAttribute("aria-busy", "true");
    }

    function resumeCategoryControls() {
        pausedPanels.forEach(function (panel) { panel.inert = false; });
        pausedPanels = [];
        root.removeAttribute("aria-busy");
    }

    async function switchCategory(url, fromHistory) {
        if (categoryRequest) categoryRequest.abort();
        var controller = new AbortController();
        categoryRequest = controller;
        var api = new URL(categoryNav.dataset.placementUrl, location.origin);
        api.search = url.search;
        pauseCategoryControls();
        updateAutosave("saving", "Загружаю расстановку…");
        try {
            var response = await fetch(api, {credentials: "same-origin", cache: "no-store",
                headers: {"Accept": "application/json"}, signal: controller.signal});
            if (controller !== categoryRequest) return;
            if (response.status === 403 || response.redirected) {
                location.assign(url.href);
                return;
            }
            if (!response.ok) throw Error();
            var result = await response.json();
            if (controller !== categoryRequest) return;
            var payload = result.payload;
            if (textValue(result.access_id) !== categoryNav.dataset.accessId ||
                result.current_date !== categoryNav.dataset.currentDate ||
                JSON.stringify(payload.brigades) !== JSON.stringify(state.brigades)) {
                location.assign(url.href);
                return;
            }
            clearActiveEquipmentRow();
            currentSlot = null;
            hideNotice();
            applyPayload(payload, false);
            if (!fromHistory && url.href !== location.href) history.pushState(null, "", url.href);
            updateAutosave("saved", state.plan.updated_at_label ? "Сохранено · " + state.plan.updated_at_label : "Все изменения сохранены");
            return true;
        } catch (error) {
            if (controller !== categoryRequest || controller.signal.aborted) return;
            if (fromHistory) {
                location.reload();
                return;
            }
            updateAutosave("error", "Не удалось загрузить вкладку");
            showNotice("Проверьте связь и попробуйте ещё раз.", {title: "Вкладка не открылась"});
        } finally {
            if (controller === categoryRequest) {
                categoryRequest = null;
                resumeCategoryControls();
            }
        }
    }

    if (categoryNav && categoryNav.dataset.placementUrl) {
        categoryNav.addEventListener("click", function (event) {
            var link = event.target.closest("a[href]");
            if (!link || event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey ||
                event.shiftKey || event.altKey || link.target || !window.AbortController) return;
            if (saving) {
                event.preventDefault();
                showToast("Дождитесь сохранения расстановки.");
                return;
            }
            if (link.hasAttribute("data-off-equipment-link")) return;
            event.preventDefault();
            if (peopleSearch) peopleSearch.clear(true);
            switchCategory(new URL(link.href), false);
        });
        window.addEventListener("popstate", function () {
            if (saving || document.querySelector("dialog[open]") || !window.AbortController) {
                location.reload();
                return;
            }
            if (peopleSearch) peopleSearch.clear(true);
            switchCategory(new URL(location.href), true);
        });
    }

    function renderCategories() {
        if (!categoryNav) return;
        categoryNav.replaceChildren();
        categoryNav.hidden = false;
        state.categories.forEach(function (category) {
            var link = createElement("a", "", "");
            link.href = category.url || "#";
            link.appendChild(createElement("strong", "", category.label || category.code));
            var categoryStatus = createElement(
                "small",
                "deputy-category-status",
                (category.status_label || "Черновик") + " · r" + textValue(category.revision || 1)
            );
            if (category.published_at_label) {
                categoryStatus.textContent += " · " + category.published_at_label;
            }
            link.appendChild(categoryStatus);
            var isActive = Boolean(category.active || category.is_active || category.code === state.role.code);
            link.classList.toggle("is-active", isActive);
            if (isActive) link.setAttribute("aria-current", "page");
            if (!category.url) {
                link.addEventListener("click", function (event) { event.preventDefault(); });
            }
            categoryNav.appendChild(link);
        });
        if (categoryNav.dataset.offEquipmentUrl) {
            var offEquipmentLink = createElement("a", "", "");
            offEquipmentLink.href = categoryNav.dataset.offEquipmentUrl;
            offEquipmentLink.setAttribute("data-off-equipment-link", "");
            offEquipmentLink.appendChild(createElement("strong", "", "Без техники"));
            categoryNav.appendChild(offEquipmentLink);
        }
        var current = categoryNav.querySelector('[aria-current="page"]');
        if (current && categoryNav.scrollWidth > categoryNav.clientWidth) {
            var navBox = categoryNav.getBoundingClientRect();
            var currentBox = current.getBoundingClientRect();
            if (currentBox.right > navBox.right) categoryNav.scrollLeft += currentBox.right - navBox.right;
            else if (currentBox.left < navBox.left) categoryNav.scrollLeft += currentBox.left - navBox.left;
        }
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
        dragPreviewPoint = { x: event.clientX, y: event.clientY };
        if (dragPreviewFrame === null) {
            dragPreviewFrame = window.requestAnimationFrame(paintDragPreview);
        }
    }

    function paintDragPreview() {
        dragPreviewFrame = null;
        if (!dragPreview || !dragPreviewPoint) return;
        var padding = 8;
        var left = Math.min(dragPreviewPoint.x - dragPreviewWidth / 2, window.innerWidth - dragPreviewWidth - padding);
        var top = Math.min(dragPreviewPoint.y - dragPreviewHeight / 2, window.innerHeight - dragPreviewHeight - padding);
        dragPreview.style.transform = "translate3d(" + Math.max(padding, left) + "px, " + Math.max(padding, top) + "px, 0)";
    }

    function clearDropHighlights() {
        Array.prototype.slice.call(root.querySelectorAll(".is-drag-over")).forEach(function (node) {
            node.classList.remove("is-drag-over");
        });
    }

    function removeDragPreview() {
        if (dragPreviewFrame !== null) window.cancelAnimationFrame(dragPreviewFrame);
        dragPreviewFrame = null;
        dragPreviewPoint = null;
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
        if (!dragPayload && !dragPreview && !dragSourceEquipmentId && !dragTargetEquipmentId) return;
        Array.prototype.slice.call(root.querySelectorAll(".is-dragging")).forEach(function (node) {
            node.classList.remove("is-dragging");
        });
        clearDropHighlights();
        var previousSourceId = dragSourceEquipmentId;
        dragSourceEquipmentId = "";
        clearDragTargetEquipmentRow();
        syncEquipmentRowState(previousSourceId);
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
        main.appendChild(createPresenceBadge(employee, false));
        preview.appendChild(main);
        applyDragPreviewTheme(preview);
        document.body.appendChild(preview);
        dragPreview = preview;
        dragPreviewWidth = preview.offsetWidth || 280;
        dragPreviewHeight = preview.offsetHeight || 56;
        preview.style.left = "0px";
        preview.style.top = "0px";
        moveDragPreview(event);

        transparentDragImage = createElement("span", "deputy-transparent-drag-image");
        document.body.appendChild(transparentDragImage);
        return transparentDragImage;
    }

    function bindDragSource(node, employee) {
        var assigned = Boolean(sourceForEmployee(employee).source_equipment_id);
        function canDrag() {
            return planEditable() && (assigned || inDeputyRoster(employee)) && !employee.disabled && !employee.busy;
        }
        node.draggable = canDrag();
        node.classList.toggle("is-disabled", !node.draggable && Boolean(employee.disabled || employee.busy));
        if (!node.draggable) return;
        node.addEventListener("dragstart", function (event) {
            if (!canDrag() || saving) {
                if (event.preventDefault) event.preventDefault();
                finishDrag();
                return;
            }
            var source = sourceForEmployee(employee);
            dragPayload = {
                employeeId: employee.id,
                sourceEquipmentId: source.source_equipment_id,
                sourceShiftType: source.source_shift_type,
                sourcePosition: source.source_position
            };
            dragSourceEquipmentId = textValue(source.source_equipment_id);
            syncEquipmentRowState(dragSourceEquipmentId);
            node.classList.add("is-dragging");
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
        card.setAttribute("data-search-employee", employee.id);
        card.setAttribute("role", "listitem");
        var canAssign = inDeputyRoster(employee);
        card.appendChild(createAvatar(employee));
        var main = createElement("span", "deputy-employee-main");
        main.appendChild(createElement("strong", "", employeeName(employee)));
        var positionLabel = employeeMeta(employee) || "Сотрудник";
        var position = createElement("small", "", positionLabel);
        position.title = positionLabel;
        main.appendChild(position);
        appendExtensionBadge(main, activeRosterExtensionUntil(employee));
        card.appendChild(main);
        if (!canAssign) {
            main.appendChild(createElement("small", "", "Бригада " + employee.brigade_code + " · только просмотр"));
            return card;
        }
        card.tabIndex = 0;
        card.title = "Двойной клик — открыть карточку сотрудника";
        main.appendChild(createPresenceBadge(employee, false));
        card.appendChild(createElement("span", "deputy-drag-mark", "⠿"));
        bindDragSource(card, employee);
        card.addEventListener("dblclick", function (event) {
            event.preventDefault();
            openEmployeeRecord(employee);
        });
        card.addEventListener("keydown", function (event) {
            if (event.key !== "Enter") return;
            event.preventDefault();
            openEmployeeRecord(employee);
        });
        return card;
    }

    function reconcileChildren(container, nodes) {
        var wanted = new Set(nodes);
        Array.from(container.children).forEach(function (node) {
            if (!wanted.has(node)) node.remove();
        });
        nodes.forEach(function (node, index) {
            var current = container.children[index] || null;
            if (current !== node) container.insertBefore(node, current);
        });
    }

    function renderEmployees(reuseNodes, context) {
        if (!employeeList) return;
        context = context || searchContext();
        var visible = context.brigadeEmployees;
        if (context.query && context.employeeMode) {
            visible = context.employeeMatches.map(function (match) { return match.employee; });
        } else if (context.query && !context.rowMatches.length) {
            visible = [];
        }
        var nextEmployeeNodes = new Map();
        var cards = visible.map(function (employee) {
            var id = textValue(employee.id);
            var fingerprint = JSON.stringify([employee, activeRosterExtensionUntil(employee)]);
            var previous = reuseNodes && employeeCardNodes.get(id);
            var node = previous && previous.fingerprint === fingerprint ? previous.node : createEmployeeCard(employee);
            nextEmployeeNodes.set(id, { node: node, fingerprint: fingerprint });
            return node;
        });
        employeeCardNodes = nextEmployeeNodes;
        if (reuseNodes) reconcileChildren(employeeList, cards);
        else employeeList.replaceChildren.apply(employeeList, cards);
        employeeList.hidden = visible.length === 0;
        if (availableCount) {
            availableCount.textContent = context.query && context.employeeMode
                ? (peopleSearch && peopleSearch.target() ? textValue(visible.length) : visible.length + " из " + context.brigadeEmployees.length)
                : (context.query && !context.rowMatches.length
                    ? "0 из " + context.brigadeEmployees.length
                    : textValue(context.brigadeEmployees.length));
        }
        if (employeeEmpty) {
            employeeEmpty.hidden = visible.length !== 0;
            employeeEmpty.textContent = context.query
                ? "Среди свободных сотрудников совпадений нет."
                : ((currentBrigade === "all" || currentBrigade === "watch") ? "Свободных сотрудников нет." : "В бригаде " + currentBrigade + " свободных сотрудников нет.");
        }
    }

    function scheduleRosterExpiry() {
        window.clearTimeout(rosterExpiryTimer);
        nextRosterExpiry = Infinity;
        var now = Date.now();
        state.employees.concat(state.factual_candidates).forEach(function (employee) {
            var expiry = Date.parse(employee.extension_expires_at);
            if (expiry > now && expiry < nextRosterExpiry) nextRosterExpiry = expiry;
        });
        if (Number.isFinite(nextRosterExpiry)) {
            rosterExpiryTimer = window.setTimeout(refreshExpiredRoster, Math.min(nextRosterExpiry - now, 2147483647));
        }
    }

    function refreshExpiredRoster() {
        if (Date.now() >= nextRosterExpiry && Number.isFinite(nextRosterExpiry)) {
            renderEmployees(true);
            if (candidateDialog && candidateDialog.open) renderCandidatePicker();
        }
        scheduleRosterExpiry();
    }

    function rowHasConflict(row) {
        return Boolean(row.conflict || (row.slots || []).some(function (slot) {
            return slot.conflict || slot.secondary_conflict;
        }));
    }

    function rowHasChanged(row) {
        return Boolean(row.changed || (row.slots || []).some(function (slot) {
            return slot.changed || slot.secondary_changed;
        }));
    }

    function rowIsUnfilled(row) {
        return (row.slots || []).some(function (slot) { return !slot.employee; });
    }

    function rowIsAssigned(row) {
        return (row.slots || []).some(function (slot) {
            return Boolean(slot.employee || slot.secondary_employee);
        });
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
        if (equipment.is_robot) wrapper.classList.add("is-robot");
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
        if (equipment.icon_url) wrapper.appendChild(iconWrap);
        var main = createElement("span", "deputy-equipment-main");
        main.appendChild(createElement("strong", "", equipment.label || "Техника"));
        if (equipment.model_label) main.appendChild(createElement("small", "", equipment.model_label));
        var stateBadge = createElement("span", "deputy-equipment-state", "Выбрано");
        stateBadge.setAttribute("aria-hidden", "true");
        main.appendChild(stateBadge);
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

    function renderCandidatePicker() {
        if (!candidateList || !currentSlot) return;
        var query = normalizeSearch(candidateSearchInput && candidateSearchInput.value);
        var showFactual = currentSlot.position === "primary" && factualCandidates && factualCandidates.checked;
        var candidatesWithChoices = state.factual_candidates.filter(function (employee) {
            return choicesForCrewPosition(employee, currentSlot.position).length;
        });
        var candidatesForPosition = showFactual ? state.factual_candidates : candidateEmployees.concat(candidatesWithChoices);
        function visibleInPicker(employee) {
            if (employee.source_equipment_id) {
                return currentBrigade === "all" || currentBrigade === "watch" || employee.brigade_code === currentBrigade;
            }
            return inDeputyRoster(employee) && employeeMatchesBrigade(employee);
        }
        var visibleEmployees = candidatesForPosition.filter(visibleInPicker);
        var candidates = rankedEmployeeMatches(visibleEmployees, query).map(function (match) {
            return match.employee;
        });
        if (query && !showFactual && currentSlot.position === "primary") {
            var otherSpecializations = state.factual_candidates.filter(function (employee) {
                return !choicesForCrewPosition(employee, currentSlot.position).length && visibleInPicker(employee);
            });
            candidates = candidates.concat(rankedEmployeeMatches(otherSpecializations, query).map(function (match) {
                return Object.assign({}, match.employee, {disabled: true});
            }));
        }
        candidateList.replaceChildren();
        if (candidateCount) {
            candidateCount.textContent = query
                ? "Найдено: " + candidates.length
                : "Кандидатов: " + visibleEmployees.length;
        }
        if (candidateSearchClear) candidateSearchClear.hidden = !query;
        candidates.forEach(function (employee) {
            var button = createElement("button", "deputy-candidate");
            button.type = "button";
            button.disabled = Boolean(employee.disabled || employee.busy);
            button.appendChild(createAvatar(employee, false));
            var main = createElement("span", "deputy-employee-main");
            main.appendChild(createElement("strong", "", employeeName(employee)));
            main.appendChild(createElement("small", "", employee.busy_reason || employee.assigned_label || employeeMeta(employee) || "Сотрудник"));
            appendExtensionBadge(main, activeRosterExtensionUntil(employee));
            var choices = choicesForCrewPosition(employee, currentSlot.position);
            if (choices.length) {
                main.appendChild(createElement("small", "", "Можно сохранить специализацию при назначении"));
            } else if (employee.specialization_issue) {
                main.appendChild(createElement("small", "", employee.specialization_issue));
            }
            button.appendChild(main);
            if (showFactual && !choices.length) {
                main.appendChild(createElement("small", "", "Фактический состав с предупреждением"));
            } else if (!choices.length) {
                button.appendChild(createPresenceBadge(employee));
            }
            button.addEventListener("click", function () {
                if (button.disabled || !currentSlot) return;
                var source = sourceForEmployee(employee);
                var target = currentSlot;
                if (choices.length) {
                    openSpecializationChoice(employee, target, choices);
                    return;
                }
                closeDialog(candidateDialog, { preserveEquipmentActive: true });
                setActiveEquipmentRow(target.row);
                saveSlot(target.row, target.slot, employee.id, source, target.position, showFactual);
            });
            candidateList.appendChild(button);
        });
        if (!candidates.length) {
            var emptyMessage;
            if (query) {
                emptyMessage = "По вашему запросу сотрудники не найдены.";
            } else {
                emptyMessage = currentBrigade === "all" || currentBrigade === "watch"
                    ? "Свободных сотрудников нет."
                    : "В бригаде " + currentBrigade + " подходящих сотрудников нет.";
            }
            candidateList.appendChild(createElement("p", "deputy-empty-state", emptyMessage));
        }
        candidateList.scrollTop = 0;
    }

    function clearCandidateSearch(restoreFocus) {
        if (!candidateSearchInput) return;
        candidateSearchInput.value = "";
        renderCandidatePicker();
        if (restoreFocus && typeof candidateSearchInput.focus === "function") {
            candidateSearchInput.focus({ preventScroll: true });
        }
    }

    function focusFirstCandidate() {
        if (!candidateList) return false;
        var candidate = Array.prototype.slice.call(candidateList.querySelectorAll(".deputy-candidate"))
            .find(function (item) { return !item.disabled; });
        if (!candidate || typeof candidate.focus !== "function") return false;
        candidate.focus({ preventScroll: true });
        return true;
    }

    function openCandidatePicker(row, slot, position) {
        if (!planEditable() || saving || !candidateDialog || !candidateList) return;
        position = position === "secondary" ? "secondary" : "primary";
        var employeeKey = position === "secondary" ? "secondary_employee" : "employee";
        var positionLabel = position === "secondary"
            ? (slot.secondary_label || state.secondary_employee_label || "Дополнительный участник")
            : (state.role.primary_label || (state.role.code === "driver" ? "Водитель" : "Машинист"));
        currentSlot = { row: row, slot: slot, position: position };
        setActiveEquipmentRow(row);
        if (candidateContext) {
            candidateContext.textContent = [row.equipment && row.equipment.label, slot.label, positionLabel]
                .filter(Boolean).join(" · ");
        }
        var candidatesById = new Map();
        state.employees.filter(function (employee) {
            return employeeCanFillPosition(employee, position);
        }).forEach(function (employee) {
            candidatesById.set(textValue(employee.id), employee);
        });
        state.rows.forEach(function (candidateRow) {
            (candidateRow.slots || []).forEach(function (candidateSlot) {
                [
                    { key: "employee", position: "primary", label: state.role.primary_label || (state.role.code === "driver" ? "Водитель" : "Машинист") },
                    { key: "secondary_employee", position: "secondary", label: candidateSlot.secondary_label || state.secondary_employee_label }
                ].forEach(function (assignment) {
                    var assigned = candidateSlot[assignment.key];
                    if (!assigned || !employeeCanFillPosition(assigned, position)) return;
                    var assignedEmployee = Object.assign({}, assigned, {
                        source_equipment_id: candidateRow.equipment && candidateRow.equipment.id,
                        source_shift_type: candidateSlot.shift_type,
                        source_position: assignment.position,
                        assigned_label: [candidateRow.equipment && candidateRow.equipment.label, candidateSlot.label, assignment.label]
                            .filter(Boolean).join(" · ")
                    });
                    candidatesById.set(textValue(assignedEmployee.id), assignedEmployee);
                });
            });
        });
        candidateEmployees = Array.from(candidatesById.values());
        if (factualCandidates) factualCandidates.checked = false;
        if (factualCandidatesControl) {
            factualCandidatesControl.hidden = position !== "primary" || !state.factual_candidates.some(employeeMatchesBrigade);
        }
        if (candidateSearchInput) candidateSearchInput.value = "";
        renderCandidatePicker();
        if (clearSlotButton) clearSlotButton.hidden = !(slot[employeeKey] || (position === "primary" && slot.reported_name));
        openDialog(candidateDialog);
        var coarseMobilePointer = window.matchMedia
            && window.matchMedia("(max-width: 720px) and (pointer: coarse)").matches;
        if (candidateSearchInput && !coarseMobilePointer && typeof candidateSearchInput.focus === "function") {
            candidateSearchInput.focus({ preventScroll: true });
        }
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
            sourceShiftType: payload.sourceShiftType || payload.source_shift_type || null,
            sourcePosition: payload.sourcePosition || payload.source_position || null
        };
    }

    function findSlot(equipmentId, shiftType, position) {
        var matched = null;
        state.rows.some(function (row) {
            if (textValue(row.equipment && row.equipment.id) !== textValue(equipmentId)) return false;
            var slot = (row.slots || []).find(function (candidate) {
                return textValue(candidate.shift_type) === textValue(shiftType);
            });
            if (!slot) return false;
            matched = { row: row, slot: slot, position: position === "secondary" ? "secondary" : "primary" };
            return true;
        });
        return matched;
    }

    function reportedOccupant(slot) {
        var review = slot.specialization_review || {};
        if (!slot.reported_name || !review.employee_id) return null;
        return {id: review.employee_id, full_name: slot.reported_name, position: review.position,
            reported: true, eligible_positions: ["primary"]};
    }

    function createCrewPosition(row, slot, position) {
        var isSecondary = position === "secondary";
        var employeeKey = isSecondary ? "secondary_employee" : "employee";
        var employee = slot[employeeKey];
        var reportedName = !employee && !isSecondary && slot.reported_name;
        var review = slot.specialization_review || {};
        var occupant = employee || (reportedName && (reportedOccupant(slot) || {full_name: reportedName, position: review.position}));
        var changed = isSecondary ? slot.secondary_changed : slot.changed;
        var conflict = isSecondary ? slot.secondary_conflict : slot.conflict;
        var issue = isSecondary ? slot.secondary_issue : slot.issue;
        var positionLabel = isSecondary
            ? (slot.secondary_label || state.secondary_employee_label || "Дополнительный участник")
            : (state.role.primary_label || (state.role.code === "driver" ? "Водитель" : "Машинист"));
        var button = createElement("div", "deputy-crew-position is-" + position);
        if (occupant) {
            button.setAttribute("data-search-employee", employee ? employee.id : review.employee_id);
            button.setAttribute("data-search-equipment", row.equipment.id);
            button.setAttribute("data-search-shift", slot.shift_type);
            button.setAttribute("data-search-position", position);
        }
        button.classList.toggle("is-disabled", !planEditable());
        button.classList.toggle("has-employee", Boolean(occupant));
        button.classList.toggle("has-conflict", Boolean(conflict));
        button.appendChild(createElement("span", "deputy-crew-position-label", positionLabel));

        if (occupant) {
            button.appendChild(createAvatar(occupant));
            var person = createElement("button", "deputy-slot-person");
            person.type = "button";
            person.setAttribute("aria-label", "Открыть карточку сотрудника: " + employeeName(occupant));
            var main = createElement("span", "deputy-employee-main");
            main.appendChild(createElement("strong", "", employeeName(occupant)));
            main.appendChild(createElement("small", "", employeeMeta(occupant) || positionLabel));
            appendExtensionBadge(main, occupant.extension_until);
            person.appendChild(main);
            var flags = createElement("span", "deputy-slot-flags");
            if (reportedName) {
                var warning = !slot.specialization_review || review.warning;
                var label = "Фактический состав";
                if (warning) label = review.current === "" ? "Специализация не указана" : "Специализация отличается";
                var badge = createElement("span", "deputy-specialization-review" + (warning ? "" : " is-resolved"));
                badge.appendChild(createElement("span", "deputy-review-label", label));
                badge.appendChild(createElement("span", "deputy-review-symbol", warning ? "!" : "Ф"));
                flags.appendChild(badge);
                person.classList.add("has-specialization-review");
                person.title = label + " — открыть пояснение";
                person.setAttribute("aria-label", label + ": " + reportedName);
                person.setAttribute("data-role-readonly-safe", "");
                person.addEventListener("click", function () { openSpecializationReview(row, slot); });
            } else {
                flags.appendChild(createPresenceBadge(employee));
            }
            if (changed) flags.appendChild(createElement("span", "deputy-slot-flag", "ИЗМ"));
            if (conflict) flags.appendChild(createElement("span", "deputy-slot-flag is-conflict", "!"));
            person.appendChild(flags);
            button.appendChild(person);
            if (occupant.id) {
                bindDragSource(button, Object.assign({}, occupant, {
                    source_equipment_id: row.equipment && row.equipment.id,
                    source_shift_type: slot.shift_type,
                    source_position: position
                }));
            }
            if (employee) {
                var equipmentLabel = [row.equipment.type_label, row.equipment.label].filter(Boolean).join(" ");
                var assignmentLabel = [equipmentLabel, slot.label, positionLabel].filter(Boolean).join(" · ");
                button.title = person.title = "Двойной клик — открыть карточку сотрудника";
                person.addEventListener("click", function (event) {
                    if (event.detail === 0) {
                        openEmployeeRecord(employee, assignmentLabel, slot.shift_type);
                    }
                });
                person.addEventListener("dblclick", function (event) {
                    event.preventDefault();
                    event.stopPropagation();
                    openEmployeeRecord(employee, assignmentLabel, slot.shift_type);
                });
                button.addEventListener("dblclick", function (event) {
                    event.preventDefault();
                    openEmployeeRecord(employee, assignmentLabel, slot.shift_type);
                });
            }
            if (planEditable()) {
                var editButton = createElement("button", "deputy-slot-edit", "Сменить");
                editButton.type = "button";
                editButton.setAttribute("aria-label", "Изменить назначение: " + textValue(slot.label));
                editButton.addEventListener("click", function (event) {
                    event.preventDefault();
                    event.stopPropagation();
                    openCandidatePicker(row, slot, position);
                });
                button.appendChild(editButton);
            }
        } else {
            var emptyAction = createElement("button", "deputy-slot-empty", isSecondary ? "Добавить" : "Назначить");
            emptyAction.type = "button";
            emptyAction.disabled = !planEditable();
            emptyAction.setAttribute("aria-label", "Назначить сотрудника: " + textValue(slot.label) + " · " + positionLabel);
            emptyAction.addEventListener("click", function () {
                openCandidatePicker(row, slot, position);
            });
            button.appendChild(emptyAction);
        }
        if (issue) button.appendChild(createElement("span", "deputy-slot-issue", issue));
        button.addEventListener("dragover", function (event) {
            if (!planEditable() || saving) return;
            event.preventDefault();
            if (!button.classList.contains("is-drag-over")) button.classList.add("is-drag-over");
            setActiveEquipmentRow(row);
            setDragTargetEquipmentRow(row);
            if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
        });
        button.addEventListener("dragleave", function (event) {
            if (event.relatedTarget && button.contains(event.relatedTarget)) return;
            button.classList.remove("is-drag-over");
            clearDragTargetEquipmentRow(row);
        });
        button.addEventListener("drop", function (event) {
            if (!planEditable() || saving) return;
            event.preventDefault();
            button.classList.remove("is-drag-over");
            var payload = dropPayloadFromEvent(event);
            if (!payload || !payload.employeeId) {
                finishDrag();
                return;
            }
            var source = findSlot(payload.sourceEquipmentId, payload.sourceShiftType, payload.sourcePosition);
            var assignedEmployee = source && (source.position === "primary"
                ? source.slot.employee || reportedOccupant(source.slot) : source.slot.secondary_employee);
            var fromCrew = assignedEmployee && textValue(assignedEmployee.id) === textValue(payload.employeeId);
            var employee = fromCrew ? assignedEmployee : state.employees.find(function (candidate) {
                return textValue(candidate.id) === textValue(payload.employeeId);
            });
            if (!employee || (!fromCrew && !inDeputyRoster(employee))) {
                finishDrag();
                return;
            }
            if (!employeeCanFillPosition(employee, position)) {
                showNotice("Специализация сотрудника не подходит для этого места в экипаже.");
                finishDrag();
                return;
            }
            if (
                textValue(payload.sourceEquipmentId) === textValue(row.equipment && row.equipment.id)
                && textValue(payload.sourceShiftType) === textValue(slot.shift_type)
                && textValue(payload.sourcePosition || "primary") === textValue(position)
            ) {
                finishDrag();
                return;
            }
            finishDrag();
            saveSlot(row, slot, payload.employeeId, {
                source_equipment_id: payload.sourceEquipmentId,
                source_shift_type: payload.sourceShiftType,
                move_reported: Boolean(employee.reported)
            }, position);
        });
        return button;
    }

    function createSlot(row, slot) {
        var wrapper = createElement("div", "deputy-slot");
        wrapper.appendChild(createCrewPosition(row, slot, "primary"));
        if (state.role.supports_secondary !== false) wrapper.appendChild(createCrewPosition(row, slot, "secondary"));
        return wrapper;
    }

    function renderBoard(reuseNodes, context) {
        if (!board) return;
        context = context || searchContext();
        var rows = state.rows.filter(rowMatchesFilter);
        if (context.query && (!context.employeeMode || (peopleSearch && !peopleSearch.target()))) {
            rows = context.rowMatches.map(function (match) { return match.row; });
        }
        if (!rows.length) {
            board.replaceChildren();
            boardBody = null;
            equipmentRowNodes = new Map();
            if (boardEmpty) boardEmpty.hidden = false;
            return;
        }
        if (boardEmpty) boardEmpty.hidden = true;

        var retainTable = reuseNodes && boardBody;
        if (!retainTable) {
            var table = createElement("table", "deputy-assignment-table");
            var thead = document.createElement("thead");
            var headRow = document.createElement("tr");
            headRow.appendChild(createElement("th", "", "Техника"));
            var firstSlots = rows[0].slots || [];
            headRow.appendChild(createElement("th", "", firstSlots[0] && firstSlots[0].label || "Смена 1"));
            headRow.appendChild(createElement("th", "", firstSlots[1] && firstSlots[1].label || "Смена 2"));
            thead.appendChild(headRow);
            table.appendChild(thead);
            boardBody = document.createElement("tbody");
            table.appendChild(boardBody);
            board.replaceChildren(table);
        }

        var nextRowNodes = new Map();
        var rebuiltIds = [];
        var rowNodes = rows.map(function (row) {
            var id = equipmentRowId(row);
            var fingerprint = JSON.stringify(row);
            var previous = retainTable && equipmentRowNodes.get(id);
            var entry = previous;
            if (!previous || previous.fingerprint !== fingerprint) {
                entry = { node: createEquipmentRow(row), fingerprint: fingerprint };
                rebuiltIds.push(id);
            }
            nextRowNodes.set(id, entry);
            return entry.node;
        });
        equipmentRowNodes = nextRowNodes;
        if (retainTable) reconcileChildren(boardBody, rowNodes);
        else boardBody.replaceChildren.apply(boardBody, rowNodes);
        rebuiltIds.forEach(syncEquipmentRowState);
    }

    function createEquipmentRow(row) {
        var tr = document.createElement("tr");
        tr.classList.add("deputy-equipment-row");
        tr.setAttribute("data-equipment-id", equipmentRowId(row));
        tr.classList.toggle("has-attention", Boolean(row.attention || row.issue));
        tr.classList.toggle("has-conflict", rowHasConflict(row));
        tr.addEventListener("pointerdown", function () { setActiveEquipmentRow(row); });
        tr.addEventListener("focusin", function () { setActiveEquipmentRow(row); });
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
        return tr;
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
        var canPublish = Boolean(
            mutationAllowed()
            && state.plan.editable
            && state.endpoints.publish
            && !saving
            && conflicts === 0
        );
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

    function renderAll(reuseNodes) {
        renderCategories();
        renderSummary();
        var context = searchContext();
        renderEmployees(reuseNodes, context);
        scheduleRosterExpiry();
        renderBoard(reuseNodes, context);
        markSearchBadge();
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
        if (!mutationAllowed()) {
            throw contractLockedError();
        }
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

    function applyPayload(payload, reuseNodes) {
        var nextState = normalizeState(payload);
        var sameBoard = state.plan.id === nextState.plan.id
            && state.plan.editable === nextState.plan.editable
            && state.role.code === nextState.role.code;
        if (peopleSearch && !categoryRequest && currentQuery()) peopleSearch.clear(true);
        state = nextState;
        renderAll(reuseNodes && sameBoard);
    }

    async function saveSlot(row, slot, employeeId, source, position, allowFactual, specializationId) {
        if (
            saving
            || !mutationAllowed()
            || !state.plan.editable
            || !state.endpoints.slot
        ) return;
        saving = true;
        updateAutosave("saving", "Сохраняю…");
        updatePublishButton();
        var body = {
            plan_id: state.plan.id,
            expected_version: state.plan.version,
            equipment_id: row.equipment && row.equipment.id,
            shift_type: slot.shift_type,
            employee_id: employeeId === null ? null : employeeId,
            position: position === "secondary" ? "secondary" : "primary"
        };
        if (allowFactual) body.factual = true;
        if (specializationId) body.specialization_id = specializationId;
        if (source && source.move_reported) body.move_reported = true;
        if (source && source.source_equipment_id) body.source_equipment_id = source.source_equipment_id;
        if (source && source.source_shift_type) body.source_shift_type = source.source_shift_type;
        try {
            var payload = await postJson(state.endpoints.slot, body);
            applyPayload(payload, true);
            hideNotice();
            var savedLabel = state.plan.updated_at_label ? "Сохранено · " + state.plan.updated_at_label : "Все изменения сохранены";
            updateAutosave("saved", savedLabel);
            if (specializationId) showToast("Специализация сохранена. Сотрудник назначен.", false);
        } catch (error) {
            if (error.payload) applyPayload(error.payload);
            updateAutosave("error", "Не сохранено");
            var shouldReload = ["stale_version", "stale_baseline", "plan_work_date_closed", "specialization_choice_changed"].indexOf(error.code) !== -1;
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

    async function submitRecordDuty(event) {
        event.preventDefault();
        if (saving || !planEditable() || recordDuty.hidden) return;
        var fields = new FormData(recordDutyForm);
        var selection = recordDutyForm.dataset;
        var submit = recordDutyForm.querySelector("button[type=submit]");
        saving = true;
        submit.disabled = true;
        recordDutyError.hidden = true;
        updateAutosave("saving", "Публикую статус…");
        updatePublishButton();
        try {
            var payload = await postJson(state.endpoints.off_equipment_transfer, {
                plan_id: Number(selection.planId),
                expected_plan_version: Number(selection.planVersion),
                work_date: selection.workDate,
                employee_id: Number(selection.employeeId),
                status: fields.get("status"),
                shift_type: fields.get("shift_type"),
                location: fields.get("location")
            });
            closeDialog(recordDialog);
            applyPayload(payload, true);
            updateAutosave("saved", "Статус опубликован");
            showToast("Сотрудник переведён во вкладку «Без техники».", false);
        } catch (error) {
            recordDutyError.textContent = error.message;
            recordDutyError.hidden = false;
            updateAutosave("error", "Статус не опубликован");
        } finally {
            saving = false;
            submit.disabled = false;
            updatePublishButton();
        }
    }

    function openPublishConfirmation() {
        if (
            !publishDialog
            || !mutationAllowed()
            || !state.plan.editable
            || saving
        ) return;
        var unfilled = Number(state.summary.unfilled_count || 0);
        if (publishSummary) {
            var publishObject = (state.role.category_label || state.role.label)
                + " · производственные сутки " + state.plan.work_date_label
                + " · ревизия r" + textValue(state.plan.revision || 1) + ". ";
            publishSummary.textContent = publishObject + (unfilled > 0
                ? "Останутся незаполненные слоты: " + unfilled + ". Карточки сотрудников будут обновлены по текущей расстановке."
                : "После публикации назначения и карточки сотрудников будут обновлены.");
        }
        openDialog(publishDialog);
    }

    async function publishPlan() {
        if (
            saving
            || !mutationAllowed()
            || !state.plan.editable
            || !state.endpoints.publish
        ) return;
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
        if (
            saving
            || !mutationAllowed()
            || !canRequestTemporaryTransfer()
        ) return;
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
            if (peopleSearch && currentQuery()) {
                peopleSearch.clear(true);
                renderSearch();
                return;
            }
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
            if (peopleSearch && currentQuery()) {
                peopleSearch.clear(true);
                renderBoard(true);
                markSearchBadge();
            }
            brigadeButtons.forEach(function (item) {
                var active = item === button;
                item.classList.toggle("is-active", active);
                item.setAttribute("aria-pressed", active ? "true" : "false");
            });
            closeDialog(candidateDialog);
            renderEmployees();
        });
    });

    function markSearchBadge() {
        if (!peopleSearch) return;
        board.removeAttribute("data-found-shift");
        root.querySelectorAll(".is-person-search-hit").forEach(function (node) {
            node.classList.remove("is-person-search-hit");
        });
        var markedNames = new Set();
        root.querySelectorAll(".deputy-planning-workspace mark.employee-search-highlight").forEach(function (mark) {
            markedNames.add(mark.parentNode);
        });
        markedNames.forEach(function (name) { name.textContent = name.textContent; });
        var hit = peopleSearch.target();
        var rowFilter = currentFilter;
        var brigadeFilter = currentBrigade;
        if (hit && hit.place.role === state.role.code) {
            var row = state.rows.find(function (item) { return item.equipment.id === hit.place.equipment; });
            var free = !hit.place.equipment && state.allWatchEmployees.find(function (item) { return item.id === hit.person.id; });
            if (row && !rowMatchesFilter(row)) rowFilter = "all";
            if (free && !employeeMatchesBrigade(free)) brigadeFilter = "all";
        }
        function reflectFilter(buttons, attribute, selected) {
            buttons.forEach(function (button) {
                var active = button.getAttribute(attribute) === selected;
                button.classList.toggle("is-active", active);
                button.setAttribute("aria-pressed", String(active));
            });
        }
        reflectFilter(filterButtons, "data-row-filter", rowFilter);
        reflectFilter(brigadeButtons, "data-brigade-filter", brigadeFilter);
        if (!hit || hit.place.role !== state.role.code) {
            var query = currentQuery();
            if (query) root.querySelectorAll(".deputy-planning-workspace [data-search-employee] .deputy-employee-main strong")
                .forEach(function (name) { peopleSearch.highlight(name, query); });
            return;
        }
        var selector = '[data-search-employee="' + hit.person.id + '"]';
        if (hit.place.equipment) {
            selector += '[data-search-equipment="' + hit.place.equipment + '"][data-search-shift="' +
                hit.place.shift + '"][data-search-position="' + hit.place.position + '"]';
        } else selector += ".deputy-employee-card";
        var badge = root.querySelector(selector);
        if (badge) {
            badge.classList.add("is-person-search-hit");
            var name = badge.querySelector(".deputy-employee-main strong");
            if (name) peopleSearch.highlight(name, currentQuery());
            if (hit.place.equipment) board.setAttribute("data-found-shift", hit.place.shift);
        }
        return badge;
    }

    function renderSearch() {
        var context = searchContext();
        renderEmployees(true, context);
        renderBoard(true, context);
        markSearchBadge();
    }

    async function loadSearchPeople() {
        // A report opened by revision must show only employees of that snapshot.
        if (new URL(location.href).searchParams.has("plan")) {
            var people = new Map();
            state.rows.forEach(function (row) {
                row.slots.forEach(function (slot) {
                    var primary = slot.employee || (slot.reported_name && {
                        id: slot.specialization_review.employee_id, full_name: slot.reported_name
                    });
                    [primary, slot.secondary_employee].forEach(function (employee, index) {
                        if (!employee) return;
                        if (!people.has(employee.id)) people.set(employee.id, Object.assign({}, employee, {places: []}));
                        people.get(employee.id).places.push({role: state.role.code, equipment: row.equipment.id,
                            number: row.equipment.label, shift: slot.shift_type, position: index ? "secondary" : "primary"});
                    });
                });
            });
            return Array.from(people.values());
        }
        var response = await fetch(searchInput.dataset.peopleUrl, {credentials: "same-origin", cache: "no-store"});
        if (!response.ok) throw Error();
        var result = await response.json();
        if (textValue(result.access_id) !== categoryNav.dataset.accessId) throw Error();
        return result.people;
    }

    if (searchInput && window.deputyPeopleSearch && searchInput.dataset.peopleUrl) {
        function categoryFor(code) { return state.categories.find(function (item) { return item.code === code; }); }
        peopleSearch = window.deputyPeopleSearch({
            root: shell || root,
            input: searchInput, query: currentQuery, normalize: normalizeSearch, role: function () { return state.role.code; },
            quickSearchBlocked: function () { return saving || Boolean(categoryRequest || dragPayload); },
            load: loadSearchPeople, render: renderSearch, findBadge: markSearchBadge, busy: function () { return saving; },
            isSnapshot: function () { return new URL(location.href).searchParams.has("plan"); },
            categoryLabel: function (code) { return categoryFor(code)?.label || code; },
            categoryUrl: function (code) { return categoryFor(code)?.url || location.href; },
            navigate: function (code) { return switchCategory(new URL(categoryFor(code).url, location.origin), false); },
            cancelNavigation: function () { if (categoryRequest) categoryRequest.abort(); },
            hasLocalMatches: function () {
                var context = searchContext();
                return context.rowMatches.length || context.employeeMatches.length;
            }
        });
    } else if (searchInput) {
        searchInput.addEventListener("input", renderSearch);
    }
    if (factualCandidates) factualCandidates.addEventListener("change", renderCandidatePicker);
    if (candidateSearchInput) {
        candidateSearchInput.addEventListener("input", renderCandidatePicker);
        candidateSearchInput.addEventListener("keydown", function (event) {
            if (event.key === "Escape" && normalizeSearch(candidateSearchInput.value)) {
                event.preventDefault();
                event.stopPropagation();
                clearCandidateSearch(true);
                return;
            }
            if (event.key !== "Enter" && event.key !== "ArrowDown") return;
            if (focusFirstCandidate()) event.preventDefault();
        });
    }
    if (candidateSearchClear) {
        candidateSearchClear.addEventListener("click", function () {
            clearCandidateSearch(true);
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
            if (!employeePool.classList.contains("is-drag-over")) employeePool.classList.add("is-drag-over");
            if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
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
            var source = findSlot(payload.sourceEquipmentId, payload.sourceShiftType, payload.sourcePosition);
            if (!source) {
                showNotice("Исходное назначение не найдено. Обновите данные и повторите действие.", {
                    title: "Назначение не изменено",
                    actionLabel: "Обновить данные",
                    action: function () { window.location.reload(); }
                });
                return;
            }
            saveSlot(source.row, source.slot, null, null, source.position);
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
    if (recordDutyForm) {
        recordDutyForm.addEventListener("change", function (event) {
            if (event.target.name === "status") updateRecordDutyFields();
        });
        recordDutyForm.addEventListener("submit", submitRecordDuty);
    }
    Array.prototype.slice.call(document.querySelectorAll("[data-publish-cancel]")).forEach(function (button) {
        button.addEventListener("click", function () { closeDialog(publishDialog); });
    });
    Array.prototype.slice.call(document.querySelectorAll("[data-temporary-transfer-cancel]")).forEach(function (button) {
        button.addEventListener("click", function () { closeDialog(temporaryTransferDialog); });
    });

    if (clearSlotButton) {
        clearSlotButton.addEventListener("click", function () {
            if (!currentSlot) return;
            var target = currentSlot;
            closeDialog(candidateDialog, { preserveEquipmentActive: true });
            setActiveEquipmentRow(target.row);
            saveSlot(target.row, target.slot, null, null, target.position);
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
        candidateDialog.addEventListener("close", function () {
            if (candidateDialog.hasAttribute("data-preserve-equipment-active")) {
                candidateDialog.removeAttribute("data-preserve-equipment-active");
                return;
            }
            clearActiveEquipmentRow();
        });
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
    document.addEventListener("pointerdown", function (event) {
        if (!event.target || typeof event.target.closest !== "function") return;
        if (event.target.closest(".deputy-equipment-row")) return;
        if (candidateDialog && candidateDialog.contains(event.target)) return;
        if (recordDialog && recordDialog.contains(event.target)) return;
        if (photoDialog && photoDialog.contains(event.target)) return;
        clearActiveEquipmentRow();
    });
    document.addEventListener("focusin", function (event) {
        if (!event.target || typeof event.target.closest !== "function") return;
        if (event.target.closest(".deputy-equipment-row")) return;
        if (candidateDialog && candidateDialog.contains(event.target)) return;
        if (recordDialog && recordDialog.contains(event.target)) return;
        if (photoDialog && photoDialog.contains(event.target)) return;
        clearActiveEquipmentRow();
    });
    document.addEventListener("dragover", moveDragPreview);
    document.addEventListener("drop", finishDrag);

    var lastMutationAllowed = mutationAllowed();
    function handleMutationAvailabilityChange() {
        var allowed = mutationAllowed();
        if (allowed === lastMutationAllowed) return;
        lastMutationAllowed = allowed;
        if (!allowed) {
            finishDrag();
            currentSlot = null;
            closeDialog(candidateDialog);
            clearActiveEquipmentRow();
            closeDialog(publishDialog);
            closeDialog(temporaryTransferDialog);
        }
        renderEmployees();
        renderBoard();
        updatePublishButton();
        updateTemporaryTransferButton();
    }

    window.addEventListener(
        "app-pwa-contract-state",
        handleMutationAvailabilityChange
    );
    window.addEventListener(
        "active-role-state-changed",
        handleMutationAvailabilityChange
    );

    document.addEventListener("visibilitychange", function () {
        if (!document.hidden && Date.now() >= nextRosterExpiry) refreshExpiredRoster();
    });

    var initialSavedLabel = state.plan.updated_at_label
        ? "Сохранено · " + state.plan.updated_at_label
        : "Все изменения сохранены";
    updateAutosave("saved", initialSavedLabel);
    renderAll();
})();
