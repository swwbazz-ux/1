/* T-025a: внешние жильцы — поиск и создание карточки.
 *
 * Окно живёт отдельно от settlement-clerk.js: адреса приходят через data-*
 * на [data-settlement-root], CSRF-токен берётся из собственной формы окна,
 * Esc перехватывается в capture-фазе, чтобы не закрыть панель комнаты под окном.
 * Ручного заселения внешнего здесь нет: заселение идёт через реестр заезда.
 */
(function () {
    "use strict";

    var root = document.querySelector("[data-settlement-root]");
    if (!root) return;

    var modal = root.querySelector("[data-external-residents-modal]");
    if (!modal) return;

    var backdrop = root.querySelector("[data-external-residents-backdrop]");
    var trigger = root.querySelector("[data-external-residents-toggle]");
    var closeButtons = Array.from(modal.querySelectorAll("[data-external-residents-close]"));
    var searchInput = modal.querySelector("[data-external-residents-search-input]");
    var countNote = modal.querySelector("[data-external-residents-count]");
    var resultsBox = modal.querySelector("[data-external-residents-results]");
    var emptyNote = modal.querySelector("[data-external-residents-empty]");
    var leaseHint = modal.querySelector("[data-external-residents-lease-hint]");
    var feedback = modal.querySelector("[data-external-residents-feedback]");
    var formToggle = modal.querySelector("[data-external-residents-create-toggle]");
    var form = modal.querySelector("[data-external-residents-create-form]");
    var formCancel = modal.querySelector("[data-external-residents-create-cancel]");
    var submitButton = modal.querySelector("[data-external-residents-submit]");
    var duplicates = modal.querySelector("[data-external-residents-duplicates]");
    var duplicateList = modal.querySelector("[data-external-residents-duplicate-list]");
    var duplicateConfirm = modal.querySelector("[data-external-residents-confirm-duplicate]");
    var duplicateDismiss = modal.querySelector("[data-external-residents-dismiss-duplicate]");
    var startWorkButton = modal.querySelector("[data-external-residents-start-work]");

    if (!searchInput || !form || !submitButton || !formToggle) return;

    var fieldSelectors = {
        resident_type: "[data-external-resident-field='resident_type']",
        full_name: "[data-external-resident-field='full_name']",
        organization: "[data-external-resident-field='organization']",
        position_title: "[data-external-resident-field='position_title']",
        phone: "[data-external-resident-field='phone']",
        sex: "[data-external-resident-field='sex']"
    };
    var fieldMessages = {
        resident_type: "Укажите тип жильца.",
        full_name: "Укажите ФИО.",
        organization: "Укажите организацию.",
        phone: "Укажите телефон.",
        sex: "Укажите пол."
    };
    var fieldOrder = [
        "resident_type",
        "full_name",
        "organization",
        "phone",
        "sex"
    ];
    var sexInputs = Array.from(form.querySelectorAll("[data-external-resident-sex]"));

    var searchTimer = null;
    var searchSequence = 0;
    var createInFlight = false;
    var accessDenied = false;
    var controlLocked = false;
    var focusResultsPending = false;

    function fieldElement(name) {
        return name === "sex"
            ? form.querySelector(fieldSelectors.sex)
            : form.querySelector(fieldSelectors[name]);
    }

    function fieldInput(name) {
        if (name === "sex") return sexInputs[0] || null;
        return form.querySelector(fieldSelectors[name]);
    }

    function fieldError(name) {
        return form.querySelector("[data-external-resident-error='" + name + "']");
    }

    function isControlHeld() {
        return root.dataset.controlState === "held" && !controlLocked;
    }

    function isCreateFormOpen() {
        return !form.hidden;
    }

    function valueOf(name) {
        var input = fieldInput(name);
        if (!input) return "";
        if (input.type === "radio") {
            var checked = sexInputs.filter(function (candidate) {
                return candidate.checked;
            })[0];
            return checked ? checked.value : "";
        }
        return String(input.value || "").trim();
    }

    function hasFormInput() {
        return fieldOrder.some(function (name) {
            return valueOf(name) !== "";
        });
    }

    function setCount(text) {
        if (countNote) countNote.textContent = text;
    }

    function setFeedback(text, isError) {
        if (!feedback) return;
        feedback.textContent = text || "";
        feedback.classList.toggle("is-error", Boolean(isError));
        feedback.hidden = !text;
    }

    function clearFeedback() {
        setFeedback("", false);
    }

    function setFieldError(name, message) {
        var holder = fieldError(name);
        var input = fieldElement(name);
        if (holder) {
            holder.textContent = message || "";
            holder.hidden = !message;
        }
        if (input) {
            if (message) input.setAttribute("aria-invalid", "true");
            else input.removeAttribute("aria-invalid");
        }
    }

    function clearFieldErrors() {
        fieldOrder.forEach(function (name) {
            setFieldError(name, "");
        });
    }

    function setFormFieldsEnabled(enabled) {
        Array.from(form.querySelectorAll("input, select")).forEach(function (input) {
            if (input.name === "csrfmiddlewaretoken") return;
            input.disabled = !enabled;
        });
    }

    function syncCreateAvailability() {
        if (root.dataset.controlState === "held") controlLocked = false;
        var held = isControlHeld() && !accessDenied;
        formToggle.disabled = !held;
        submitButton.disabled = !held || createInFlight;
        if (duplicateConfirm) duplicateConfirm.disabled = !held || createInFlight;
        if (leaseHint) leaseHint.hidden = held || accessDenied;
        if (startWorkButton) startWorkButton.hidden = held || accessDenied;
        setFormFieldsEnabled(held);
    }

    function setControlLocked(message) {
        controlLocked = true;
        setFeedback(message || "Управление расселением не активно.", true);
        syncCreateAvailability();
    }

    function setAccessDenied(message) {
        accessDenied = true;
        if (resultsBox) {
            resultsBox.hidden = true;
            resultsBox.replaceChildren();
        }
        if (emptyNote) emptyNote.hidden = true;
        setCount("Нет доступа к расселению.");
        setFeedback(message || "Нет доступа к расселению.", true);
        syncCreateAvailability();
    }

    function csrfToken() {
        var input = form.querySelector("input[name='csrfmiddlewaretoken']");
        if (input && input.value) return input.value;
        var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : "";
    }

    function element(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function residentMeta(resident) {
        var parts = [];
        if (resident.position_title) parts.push(resident.position_title);
        if (resident.organization) parts.push(resident.organization);
        return parts.join(" · ");
    }

    function residentTypeLabel(resident) {
        if (resident.resident_type_label) return String(resident.resident_type_label);
        var labels = {
            CONTRACTOR: "Сотрудник подрядчика",
            BUSINESS_TRIP: "Командированный",
            EXTERNAL_OTHER: "Другой внешний жилец"
        };
        return labels[resident.resident_type] || "";
    }

    function buildResultRow(resident) {
        var row = element("article", "settlement-external-residents-result");
        var head = element("div", "settlement-external-residents-result-head");
        head.appendChild(element("strong", "", resident.full_name || "Без ФИО"));
        var label = residentTypeLabel(resident);
        if (label) head.appendChild(element("span", "settlement-external-residents-type", label));
        row.appendChild(head);
        var meta = residentMeta(resident);
        if (meta) row.appendChild(element("small", "", meta));
        if (resident.phone) row.appendChild(element("small", "", resident.phone));
        row.appendChild(element("small", "", "Таб. № " + (resident.personnel_number || "не указан")));
        var details = element("button", "settlement-bed-prep-secondary", "Открыть карточку");
        details.type = "button";
        details.addEventListener("click", function () {
            closeModal();
            root.dispatchEvent(new CustomEvent("settlement:open-resident-card", {
                detail: {residentId: resident.id}
            }));
        });
        row.appendChild(details);
        return row;
    }

    function renderResults(residents) {
        if (!resultsBox) return;
        resultsBox.replaceChildren();
        if (!residents.length) {
            resultsBox.hidden = true;
            if (emptyNote) emptyNote.hidden = searchInput.value.trim().length < 2;
            setCount("Найдено: 0");
            if (focusResultsPending && countNote) countNote.focus();
            focusResultsPending = false;
            return;
        }
        residents.forEach(function (resident) {
            resultsBox.appendChild(buildResultRow(resident));
        });
        resultsBox.hidden = false;
        if (emptyNote) emptyNote.hidden = true;
        setCount("Найдено: " + residents.length);
        if (focusResultsPending) resultsBox.focus();
        focusResultsPending = false;
    }

    function runSearch() {
        var query = searchInput.value.trim();
        if (query.length < 2) {
            searchSequence += 1;
            if (resultsBox) {
                resultsBox.hidden = true;
                resultsBox.replaceChildren();
            }
            if (emptyNote) emptyNote.hidden = true;
            setCount(query.length ? "Введите минимум 2 символа" : "Начните вводить ФИО, организацию или телефон");
            if (focusResultsPending) {
                if (countNote) countNote.focus();
                focusResultsPending = false;
            }
            return;
        }

        var url = root.dataset.externalResidentSearchUrl;
        if (!url) {
            setCount("Поиск внешних жильцов недоступен.");
            return;
        }

        searchSequence += 1;
        var sequence = searchSequence;
        setCount("Ищем…");

        fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrfToken(),
                "X-Requested-With": "XMLHttpRequest"
            },
            body: JSON.stringify({q: query})
        })
            .then(function (response) {
                return response.json().then(function (payload) {
                    if (response.status === 403) {
                        var denied = new Error((payload && payload.error) || "Нет доступа к расселению.");
                        denied.code = "denied";
                        throw denied;
                    }
                    if (!response.ok || !payload || payload.ok !== true) {
                        var failure = new Error((payload && payload.error) || "Не удалось выполнить поиск.");
                        failure.code = payload && payload.code;
                        throw failure;
                    }
                    return payload;
                }, function () {
                    var broken = new Error("Не удалось выполнить поиск.");
                    if (response.status === 403) broken.code = "denied";
                    throw broken;
                });
            })
            .then(function (payload) {
                if (sequence !== searchSequence) return;
                renderResults(payload.results || []);
            })
            .catch(function (error) {
                if (sequence !== searchSequence) return;
                if (error && error.code === "denied") {
                    setAccessDenied(error.message);
                    return;
                }
                if (resultsBox) {
                    resultsBox.hidden = true;
                    resultsBox.replaceChildren();
                }
                if (emptyNote) emptyNote.hidden = true;
                setCount("Не удалось выполнить поиск. Повторите.");
                focusResultsPending = false;
            });
    }

    function openCreateForm() {
        form.hidden = false;
        formToggle.setAttribute("aria-expanded", "true");
        syncCreateAvailability();
        var first = fieldInput("resident_type");
        if (first && !first.disabled) first.focus();
    }

    function collapseCreateForm() {
        form.hidden = true;
        formToggle.setAttribute("aria-expanded", "false");
        if (formToggle) formToggle.focus();
    }

    function clearCreateForm() {
        var typeInput = fieldInput("resident_type");
        if (typeInput) typeInput.value = "";
        ["full_name", "organization", "position_title", "phone"].forEach(function (name) {
            var input = fieldInput(name);
            if (input) input.value = "";
        });
        sexInputs.forEach(function (input) {
            input.checked = false;
        });
        clearFieldErrors();
        hideDuplicates();
        form.hidden = true;
        formToggle.setAttribute("aria-expanded", "false");
    }

    function hideDuplicates() {
        if (!duplicates) return;
        duplicates.hidden = true;
        if (duplicateList) duplicateList.replaceChildren();
    }

    function showDuplicates(candidates) {
        if (!duplicates || !duplicateList) return;
        duplicateList.replaceChildren();
        candidates.forEach(function (candidate) {
            var item = document.createElement("li");
            var name = candidate.full_name || "Без ФИО";
            var meta = [];
            if (candidate.organization) meta.push(candidate.organization);
            if (candidate.phone) meta.push(candidate.phone);
            item.textContent = meta.length ? name + " — " + meta.join(", ") : name;
            duplicateList.appendChild(item);
        });
        duplicates.hidden = false;
        if (duplicateConfirm && !duplicateConfirm.disabled) duplicateConfirm.focus();
    }

    function readValues() {
        return {
            resident_type: valueOf("resident_type"),
            full_name: valueOf("full_name"),
            organization: valueOf("organization"),
            position_title: valueOf("position_title"),
            phone: valueOf("phone"),
            sex: valueOf("sex")
        };
    }

    function validateValues(values) {
        var invalid = [];
        fieldOrder.forEach(function (name) {
            if (!values[name]) invalid.push(name);
        });
        return invalid;
    }

    function focusFirstInvalid(names) {
        if (!names.length) return;
        var target = fieldInput(names[0]);
        if (target && target.focus) target.focus();
    }

    function markInvalid(names) {
        clearFieldErrors();
        names.forEach(function (name) {
            setFieldError(name, fieldMessages[name] || "Заполните поле.");
        });
    }

    function guessFieldFromServerMessage(message) {
        var text = String(message || "").toLowerCase();
        if (text.indexOf("фио") !== -1) return "full_name";
        if (text.indexOf("должность") !== -1) return "position_title";
        if (text.indexOf("организац") !== -1) return "organization";
        if (text.indexOf("телефон") !== -1) return "phone";
        if (text.indexOf("пол") !== -1) return "sex";
        if (text.indexOf("тип") !== -1) return "resident_type";
        return "";
    }

    function applyServerValidation(payload) {
        var code = String((payload && payload.code) || "");
        var message = (payload && payload.error) || "Проверьте заполнение карточки.";
        var field = "";
        if (code === "settlement.resident.invalid_sex") field = "sex";
        else if (code === "settlement.resident.invalid_type") field = "resident_type";
        else if (code === "settlement.resident.required_field") {
            var empty = validateValues(readValues());
            field = empty.length ? empty[0] : guessFieldFromServerMessage(message);
        }
        if (field) {
            markInvalid([field]);
            setFieldError(field, message);
            focusFirstInvalid([field]);
            setFeedback(message, true);
            return;
        }
        setFeedback(message, true);
    }

    function handleCreated(resident) {
        var name = resident && resident.full_name ? resident.full_name : "";
        clearFeedback();
        clearCreateForm();
        hideDuplicates();
        setFeedback("Карточка создана. Передайте её табельщику для включения в реестр заезда.", false);
        if (name) searchInput.value = name;
        focusResultsPending = true;
        runSearch();
    }

    function submitCreate(confirmDuplicate) {
        if (createInFlight) return;
        var values = readValues();
        var invalid = validateValues(values);
        if (invalid.length) {
            markInvalid(invalid);
            setFeedback("Заполните обязательные поля карточки.", true);
            focusFirstInvalid(invalid);
            return;
        }
        clearFieldErrors();
        clearFeedback();
        hideDuplicates();

        var url = root.dataset.externalResidentCreateUrl;
        if (!url) {
            setFeedback("Создание карточки недоступно: маршрут не подключён.", true);
            return;
        }

        var payload = {
            resident_type: values.resident_type,
            full_name: values.full_name,
            position_title: values.position_title,
            organization: values.organization,
            phone: values.phone,
            sex: values.sex,
            confirm_duplicate: Boolean(confirmDuplicate)
        };

        createInFlight = true;
        syncCreateAvailability();
        setFeedback("Создаём карточку…", false);

        fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrfToken(),
                "X-Requested-With": "XMLHttpRequest"
            },
            body: JSON.stringify(payload)
        })
            .then(function (response) {
                return response.json().then(function (body) {
                    return {status: response.status, body: body, response: response};
                }, function () {
                    return {status: response.status, body: null, response: response};
                });
            })
            .then(function (result) {
                var body = result.body || {};
                if (result.status === 403) {
                    if (String(body.code || "").indexOf("settlement.control.") === 0) {
                        setControlLocked(body.error || "Управление расселением не активно.");
                        return;
                    }
                    setAccessDenied(body.error || "Нет доступа к расселению.");
                    return;
                }
                if (result.status === 409) {
                    if (String(body.code || "").indexOf("settlement.control.") === 0) {
                        setControlLocked(body.error || "Управление расселением не активно.");
                        return;
                    }
                    setFeedback(body.error || "Операция отклонена.", true);
                    return;
                }
                if (result.status === 400) {
                    applyServerValidation(body);
                    return;
                }
                if (result.status === 200 && body.requires_confirmation) {
                    setFeedback("Похожая карточка уже есть. Проверьте список ниже.", true);
                    showDuplicates(body.duplicate_candidates || []);
                    return;
                }
                if (!body.ok) {
                    setFeedback(body.error || "Не удалось выполнить запрос. Повторите.", true);
                    return;
                }
                handleCreated(body.resident);
            })
            .catch(function () {
                setFeedback("Не удалось выполнить запрос. Повторите.", true);
            })
            .then(function () {
                createInFlight = false;
                syncCreateAvailability();
                if (accessDenied) setFormFieldsEnabled(false);
            });
    }

    function focusableElements() {
        return Array.from(modal.querySelectorAll(
            "button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])"
        )).filter(function (candidate) {
            return !candidate.disabled && candidate.getClientRects().length > 0;
        });
    }

    function trapFocus(event) {
        var items = focusableElements();
        if (!items.length) return;
        var first = items[0];
        var last = items[items.length - 1];
        var current = document.activeElement;
        if (event.shiftKey) {
            if (current === first || !modal.contains(current)) {
                event.preventDefault();
                last.focus();
            }
            return;
        }
        if (current === last || !modal.contains(current)) {
            event.preventDefault();
            first.focus();
        }
    }

    function openModal() {
        if (!modal.hidden) return;
        modal.hidden = false;
        modal.setAttribute("aria-hidden", "false");
        if (backdrop) backdrop.hidden = false;
        document.body.classList.add("settlement-external-residents-modal-open");
        if (trigger) trigger.setAttribute("aria-expanded", "true");
        syncCreateAvailability();
        if (searchInput.value.trim().length >= 2) runSearch();
        searchInput.focus();
    }

    function closeModal() {
        if (modal.hidden) return;
        modal.hidden = true;
        modal.setAttribute("aria-hidden", "true");
        if (backdrop) backdrop.hidden = true;
        document.body.classList.remove("settlement-external-residents-modal-open");
        focusResultsPending = false;
        if (trigger) {
            trigger.setAttribute("aria-expanded", "false");
            trigger.focus();
        }
    }

    if (trigger) {
        trigger.addEventListener("click", function () {
            openModal();
        });
    }

    closeButtons.forEach(function (button) {
        button.addEventListener("click", function () {
            closeModal();
        });
    });

    if (backdrop) {
        backdrop.addEventListener("click", function () {
            closeModal();
        });
    }

    formToggle.addEventListener("click", function () {
        if (formToggle.disabled) return;
        if (isCreateFormOpen()) collapseCreateForm();
        else openCreateForm();
    });

    if (formCancel) {
        formCancel.addEventListener("click", function () {
            collapseCreateForm();
        });
    }

    form.addEventListener("submit", function (event) {
        event.preventDefault();
        submitCreate(false);
    });

    if (duplicateConfirm) {
        duplicateConfirm.addEventListener("click", function () {
            if (duplicateConfirm.disabled) return;
            submitCreate(true);
        });
    }

    if (duplicateDismiss) {
        duplicateDismiss.addEventListener("click", function () {
            hideDuplicates();
            setFeedback("Создание не подтверждено. Проверьте найденные карточки.", false);
            if (submitButton && !submitButton.disabled) submitButton.focus();
        });
    }

    if (startWorkButton) {
        startWorkButton.addEventListener("click", function () {
            closeModal();
            var acquire = root.querySelector("[data-control-acquire]");
            if (acquire && acquire.focus) {
                acquire.focus();
                if (acquire.scrollIntoView) acquire.scrollIntoView({block: "center"});
            }
        });
    }

    searchInput.addEventListener("input", function () {
        window.clearTimeout(searchTimer);
        searchTimer = window.setTimeout(runSearch, 220);
    });

    document.addEventListener("keydown", function (event) {
        if (modal.hidden) return;
        if (event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            if (isCreateFormOpen() && hasFormInput()) {
                collapseCreateForm();
                return;
            }
            closeModal();
            return;
        }
        if (event.key === "Tab") trapFocus(event);
    }, true);

    if (window.MutationObserver) {
        new MutationObserver(function () {
            syncCreateAvailability();
        }).observe(root, {attributes: true, attributeFilter: ["data-control-state"]});
    }

    syncCreateAvailability();
}());
