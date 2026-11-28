/* T-029 B2: срочное размещение, резерв и административный перехват управления.
   Карта, панель комнаты и поиск «Нерасселённые» остаются в settlement-clerk.js.
   Этот файл добавляет только свои узлы (бейджи на койках, блок подготовки,
   форму подготовки, вкладку «Временные», кнопку резерва, кнопку перехвата) и
   вызывает серверные маршруты T-029. Существующая разметка и классы карты не
   перерисовываются: отметки ставятся атрибутами и собственным бейджем. */
(function () {
    "use strict";

    var root = document.querySelector("[data-settlement-root]");
    if (!root) return;

    var panel = root.querySelector("[data-room-panel]");
    var placementPanel = root.querySelector("[data-placement-panel]");
    var settlementForm = root.querySelector("[data-settlement-form]");
    var occupiedActions = root.querySelector("[data-occupied-actions]");
    var feedback = root.querySelector("[data-placement-feedback]");
    var controlFeedback = root.querySelector("[data-control-feedback]");
    var takeoverButton = root.querySelector("[data-control-takeover]");

    var prepBlock = root.querySelector("[data-bed-preparation]");
    var checkinForm = root.querySelector("[data-bed-checkin]");
    var prepResidentLabel = root.querySelector("[data-bed-preparation-resident]");
    var prepHoldLabel = root.querySelector("[data-bed-preparation-hold]");
    var prepBasisLabel = root.querySelector("[data-bed-preparation-basis]");
    var prepExpiredHint = root.querySelector("[data-bed-preparation-expired]");
    var prepConfirmButton = root.querySelector("[data-bed-preparation-confirm]");
    var prepExtendOpen = root.querySelector("[data-bed-preparation-extend-open]");
    var prepCancelButton = root.querySelector("[data-bed-preparation-cancel]");
    var prepExtendBlock = root.querySelector("[data-bed-preparation-extend]");
    var prepExtendHoldInput = root.querySelector("[data-bed-preparation-hold-input]");
    var prepExtendReasonInput = root.querySelector("[data-bed-preparation-reason]");
    var prepExtendSubmit = root.querySelector("[data-bed-preparation-extend-submit]");
    var prepOpenButton = root.querySelector("[data-bed-preparation-open]");
    var prepActions = root.querySelector("[data-bed-preparation-actions]");
    var reserveActions = root.querySelector("[data-bed-reserve-actions]");
    var reserveButton = root.querySelector("[data-bed-reserve-toggle]");
    var prepForm = root.querySelector("[data-bed-preparation-form]");
    var prepKindButtons = Array.from(root.querySelectorAll("[data-preparation-kind]"));
    var prepResidentKindLabel = root.querySelector("[data-bed-preparation-resident-label]");
    var prepResidentSearch = root.querySelector("[data-bed-preparation-resident-search]");
    var prepResidentResults = root.querySelector("[data-bed-preparation-resident-results]");
    var prepSelectedCard = root.querySelector("[data-bed-preparation-selected]");
    var prepSelectedName = root.querySelector("[data-bed-preparation-selected-name]");
    var prepSelectedMeta = root.querySelector("[data-bed-preparation-selected-meta]");
    var prepArrivalInput = root.querySelector("[data-bed-preparation-arrival]");
    var prepBasisInput = root.querySelector("[data-bed-preparation-basis-input]");
    var prepSuggestButton = root.querySelector("[data-bed-preparation-suggest]");
    var prepSuggestions = root.querySelector("[data-bed-preparation-suggestions]");
    var prepSubmitButton = root.querySelector("[data-bed-preparation-submit]");
    var prepFormCancel = root.querySelector("[data-bed-preparation-form-cancel]");

    var unsettledPanel = root.querySelector("[data-unsettled-panel]");
    var unsettledBody = root.querySelector("[data-unsettled-body]");
    var unsettledFields = root.querySelector("[data-unsettled-fields]");
    var scopeButtons = Array.from(root.querySelectorAll("[data-temporary-scope]"));
    var scopeCount = root.querySelector("[data-temporary-scope-count]");
    var temporaryBody = root.querySelector("[data-temporary-body]");
    var temporaryList = root.querySelector("[data-temporary-list]");
    var temporarySummary = root.querySelector("[data-temporary-summary]");
    var temporaryFeedback = root.querySelector("[data-temporary-feedback]");
    var temporaryEmpty = root.querySelector("[data-temporary-empty]");

    var state = {
        preparations: {},
        preparationRows: [],
        temporary: {},
        temporaryRows: [],
        reserves: {},
        reserveRows: [],
        relocationRows: [],
        relocationByOccupancy: {},
        counts: {prepared: 0, temporary: 0, reserves: 0, expired_preparations: 0},
        loaded: false,
        drawerFeedbackKind: "",
        scope: "unsettled",
        formOpen: false,
        formBedId: "",
        panelBedId: "",
        draft: {resident: null, kind: "employee", arrivalDate: "", basis: "", query: ""},
        loading: false,
        saving: false,
        searchTimer: null,
        searchSequence: 0
    };

    /* T-036 frontend-7: тип жильца для подготовки места.
       Поиск (settlement_resident_search) отдаёт и сотрудников, и внешние
       карточки; prepare/confirm принимают resident_id любого типа.
       Режим «Гость» — командированные и другие внешние гости; серверный
       фильтр `resident_type=GUEST` (backend-20) применяется до лимита.
       Пока такого значения нет, поиск повторяется без параметра, а клиентский
       фильтр остаётся страховкой (см. runResidentSearch). */
    var RESIDENT_KIND_EMPLOYEE = "employee";
    var RESIDENT_KIND_GUEST = "guest";
    var RESIDENT_KIND_EMPTY = {
        employee: "Сотрудники не найдены. Проверьте ФИО или табельный номер.",
        guest: "Гости не найдены. Проверьте ФИО, организацию или телефон."
    };
    var RESIDENT_KIND_SEARCH_TYPE = {
        employee: "EMPLOYEE",
        guest: "GUEST"
    };
    /* Гость = командированный или другой внешний жилец; подрядчик — нет. */
    var GUEST_RESIDENT_TYPES = {
        BUSINESS_TRIP: true,
        EXTERNAL_OTHER: true
    };
    var SPECIAL_FUND_HINTS = {
        protected_dispatch_only: "38 — ВИП-резерв: только командированный из центрального офиса, вручную и с основанием.",
        protected_room_forbidden: "Комната 36/37 защищённого фонда: через «Подготовить место» недоступна.",
        itr_room_requires_itr: "Комната на 2 места: заселение только ручное (ИТР) — используйте «Заселить» на карте."
    };

    function element(tagName, className, text) {
        var node = document.createElement(tagName);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function setHidden(node, hidden) {
        if (!node) return;
        if (node.hidden === Boolean(hidden)) return;
        node.hidden = Boolean(hidden);
    }

    function csrfToken() {
        var input = root.querySelector("input[name='csrfmiddlewaretoken']");
        if (input && input.value) return input.value;
        var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : "";
    }

    function formatMoment(value) {
        if (!value) return "не указано";
        var moment = new Date(value);
        if (Number.isNaN(moment.getTime())) return String(value);
        return moment.toLocaleString("ru-RU", {
            day: "2-digit",
            month: "2-digit",
            year: "numeric",
            hour: "2-digit",
            minute: "2-digit"
        });
    }

    function showFeedback(message, isError) {
        if (feedback) {
            feedback.textContent = message;
            feedback.classList.toggle("is-error", Boolean(isError));
            feedback.hidden = !message;
        }
        /* Действия запускаются и из ящика «Временные», когда панель комнаты
           закрыта: дублируем сообщение в видимый блок вкладки. */
        if (message && unsettledPanel && !unsettledPanel.hidden) {
            showDrawerMessage(message, isError, "action");
        }
    }

    function showDrawerMessage(message, isError, kind) {
        if (!temporaryFeedback) return;
        temporaryFeedback.textContent = message;
        temporaryFeedback.classList.toggle("is-error", Boolean(isError));
        temporaryFeedback.hidden = !message;
        state.drawerFeedbackKind = message ? String(kind || "") : "";
    }

    function clearDrawerMessage(kind) {
        if (!temporaryFeedback || temporaryFeedback.hidden) return;
        if (kind && state.drawerFeedbackKind !== kind) return;
        showDrawerMessage("", false, "");
    }

    function showControlFeedback(message, isError) {
        if (!controlFeedback) return;
        controlFeedback.textContent = message;
        controlFeedback.classList.toggle("is-error", Boolean(isError));
        controlFeedback.hidden = !message;
    }

    function askReason(question) {
        var value = window.prompt(question);
        if (value === null) return null;
        value = String(value).trim();
        if (!value) {
            showFeedback("Причина обязательна.", true);
            return null;
        }
        return value;
    }

    function requestJson(url, payload) {
        if (!url) return Promise.reject(new Error("Действие недоступно на этом экране."));
        return fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrfToken(),
                "X-Requested-With": "XMLHttpRequest"
            },
            body: JSON.stringify(payload || {})
        }).then(function (response) {
            if (response.redirected || response.status === 401) {
                var sessionError = new Error("Сессия истекла, войдите заново.");
                sessionError.httpStatus = response.status;
                throw sessionError;
            }
            return response.json()
                .catch(function () { return {}; })
                .then(function (body) {
                    if (!response.ok || body.ok === false) {
                        var error = new Error(
                            body && body.error
                                ? body.error
                                : "Не удалось выполнить действие."
                        );
                        error.code = body && body.code ? body.code : "";
                        error.httpStatus = response.status;
                        throw error;
                    }
                    return body;
                });
        });
    }

    function bedUrl(template, identifier) {
        return String(template || "").replace("/0/", "/" + identifier + "/");
    }

    function selectedBed() {
        return root.querySelector("[data-bed].is-selected");
    }

    function findBed(bedStableId) {
        var beds = Array.from(root.querySelectorAll("[data-bed]"));
        return beds.find(function (bed) {
            return bed.dataset.bedId === bedStableId;
        }) || null;
    }

    function isTransferredBed(bed) {
        var room = bed && bed.closest("[data-room-card]");
        return Boolean(room && room.dataset.transferStatus === "transferred");
    }

    function controlHeld() {
        return root.dataset.controlState === "held";
    }

    /* --- Карта: бейджи и отметки состояния -------------------------------- */

    function baseAriaLabel(bed) {
        var current = bed.getAttribute("aria-label") || "";
        var stored = bed.dataset.baseAriaLabel || "";
        if (!stored || current.indexOf(stored) !== 0) {
            bed.dataset.baseAriaLabel = current;
            return current;
        }
        return stored;
    }

    function renderBedTags(bed, preparation, temporary, reserve) {
        var holder = bed.querySelector("[data-bed-tags]");
        var tags = [];
        if (reserve) tags.push(["Рез.", "is-reserve"]);
        if (preparation) {
            tags.push(["Подг.", "is-prepared"]);
        } else if (temporary) {
            tags.push(["Врем.", "is-temporary"]);
        }
        if (!tags.length) {
            if (holder) holder.remove();
            return;
        }
        if (!holder) {
            holder = document.createElement("span");
            holder.className = "settlement-bed-tags";
            holder.setAttribute("data-bed-tags", "");
            holder.setAttribute("aria-hidden", "true");
            bed.appendChild(holder);
        }
        holder.replaceChildren();
        tags.forEach(function (entry) {
            holder.appendChild(element("span", "settlement-bed-tag " + entry[1], entry[0]));
        });
    }

    function renderBedAriaLabel(bed, preparation, temporary, reserve) {
        var parts = [baseAriaLabel(bed)];
        if (preparation) parts.push("подготовлено для " + preparation.resident_name);
        if (temporary) parts.push("временно до утверждения");
        if (reserve) parts.push("резерв");
        bed.setAttribute("aria-label", parts.filter(Boolean).join(", "));
    }

    function markBeds() {
        root.querySelectorAll("[data-bed]").forEach(function (bed) {
            var bedId = bed.dataset.bedId;
            var preparation = state.preparations[bedId] || null;
            var temporary = state.temporary[bedId] || null;
            var reserve = state.reserves[bedId] || null;
            bed.dataset.bedPrepared = preparation ? "true" : "";
            bed.dataset.bedTemporary = temporary ? "true" : "";
            bed.dataset.bedReserve = reserve ? "true" : "";
            bed.classList.toggle("is-prepared", Boolean(preparation));
            bed.classList.toggle("is-temporary", Boolean(temporary));
            bed.classList.toggle("is-reserve", Boolean(reserve));
            renderBedTags(bed, preparation, temporary, reserve);
            renderBedAriaLabel(bed, preparation, temporary, reserve);
        });
    }

    /* --- Состояние временного размещения --------------------------------- */

    function indexRows(rows) {
        var index = {};
        rows.forEach(function (row) {
            if (row && row.bed_stable_id) index[row.bed_stable_id] = row;
        });
        return index;
    }

    /* T-029: предложение переселения приходит из state ключом
       relocation_suggestions и относится к временному размещению по
       occupancy_id. Это только подсказка: ничего не переселяется само. */
    function indexRelocationRows(rows) {
        var index = {};
        rows.forEach(function (row) {
            if (!row) return;
            if (row.occupancy_id === null || row.occupancy_id === undefined) return;
            index[String(row.occupancy_id)] = row;
        });
        return index;
    }

    function applyState(payload) {
        state.preparationRows = payload.preparations || [];
        state.temporaryRows = payload.temporary || [];
        state.reserveRows = payload.reserves || [];
        state.relocationRows = payload.relocation_suggestions || [];
        state.preparations = indexRows(state.preparationRows);
        state.temporary = indexRows(state.temporaryRows);
        state.reserves = indexRows(state.reserveRows);
        state.relocationByOccupancy = indexRelocationRows(state.relocationRows);
        state.counts = payload.counts || state.counts;
        state.loaded = true;
        clearDrawerMessage("load");
        markBeds();
        renderTemporaryList();
        syncPanel();
    }

    function loadState() {
        if (!root.dataset.temporaryStateUrl || state.loading) return Promise.resolve();
        state.loading = true;
        return fetch(root.dataset.temporaryStateUrl, {
            credentials: "same-origin",
            headers: {"X-Requested-With": "XMLHttpRequest"}
        })
            .then(function (response) {
                if (response.redirected || response.status === 401) {
                    var sessionError = new Error("Сессия истекла, войдите заново.");
                    sessionError.httpStatus = response.status;
                    throw sessionError;
                }
                return response.json()
                    .catch(function () { return {}; })
                    .then(function (payload) {
                        if (!response.ok || !payload.ok) {
                            var error = new Error(
                                payload && payload.error
                                    ? payload.error
                                    : "Не удалось загрузить состояние срочного размещения."
                            );
                            error.httpStatus = response.status;
                            throw error;
                        }
                        return payload;
                    });
            })
            .then(function (payload) {
                applyState(payload);
            })
            .catch(function (error) {
                /* Ложное «Временных нет» недопустимо: показываем ошибку и
                   сохраняем последнее успешно загруженное состояние. */
                var message = error && error.message
                    ? error.message
                    : "Не удалось загрузить состояние срочного размещения.";
                showDrawerMessage(message, true, "load");
                setHidden(temporaryEmpty, true);
                if (!state.loaded) {
                    if (scopeCount) {
                        scopeCount.textContent = "";
                        scopeCount.hidden = true;
                    }
                    if (temporarySummary) temporarySummary.textContent = "";
                }
            })
            .finally(function () {
                state.loading = false;
            });
    }

    function specialFundHint(error) {
        var code = error && error.code ? String(error.code) : "";
        return SPECIAL_FUND_HINTS[code] || "";
    }

    function actionErrorMessage(error) {
        var message = error && error.message
            ? String(error.message)
            : "Не удалось выполнить действие.";
        var hint = specialFundHint(error);
        if (!hint || message.indexOf(hint) !== -1) return message;
        return message + " " + hint;
    }

    function runAction(url, payload, onSuccess) {
        if (state.saving) return;
        state.saving = true;
        syncPanel();
        return requestJson(url, payload)
            .then(function (body) {
                if (onSuccess) return onSuccess(body);
                return loadState();
            })
            .catch(function (error) {
                if (error && error.httpStatus === 409) {
                    loadState();
                }
                showFeedback(actionErrorMessage(error), true);
            })
            .finally(function () {
                state.saving = false;
                syncPanel();
            });
    }

    /* --- Панель комнаты: подготовка места -------------------------------- */

    function preparationForBed(bed) {
        if (!bed) return null;
        return state.preparations[bed.dataset.bedId] || null;
    }

    function preparationById(preparationId) {
        var found = null;
        state.preparationRows.forEach(function (row) {
            if (found) return;
            if (Number(row.id) === Number(preparationId)) found = row;
        });
        return found;
    }

    function captureDraft() {
        if (!state.formOpen) return;
        state.draft.query = prepResidentSearch ? prepResidentSearch.value : state.draft.query;
        state.draft.arrivalDate = prepArrivalInput ? prepArrivalInput.value : state.draft.arrivalDate;
        state.draft.basis = prepBasisInput ? prepBasisInput.value : state.draft.basis;
    }

    function restoreDraft() {
        if (prepResidentSearch) prepResidentSearch.value = state.draft.query || "";
        if (prepArrivalInput && !prepArrivalInput.value) {
            prepArrivalInput.value = state.draft.arrivalDate
                || String(root.dataset.currentDate || "");
        }
        if (prepBasisInput) prepBasisInput.value = state.draft.basis || "";
        renderSelectedResident();
    }

    function renderSelectedResident() {
        if (!prepSelectedCard) return;
        var resident = state.draft.resident;
        prepSelectedCard.hidden = !resident;
        if (!resident) return;
        if (prepSelectedName) {
            prepSelectedName.replaceChildren((resident.full_name || "") + " ",
                window.settlementPersonnelNumber(resident.meta?.personnel_number));
        }
        if (prepSelectedMeta) {
            prepSelectedMeta.textContent = [
                resident.resident_type_label || "",
                resident.meta && resident.meta.position ? resident.meta.position : "",
                resident.meta && resident.meta.organization ? resident.meta.organization : ""
            ].filter(Boolean).join(" · ");
        }
    }

    function resetPreparationDraft() {
        var kind = residentKind();
        state.draft = {resident: null, kind: kind, arrivalDate: "", basis: "", query: ""};
        state.formBedId = "";
        if (prepResidentSearch) prepResidentSearch.value = "";
        if (prepArrivalInput) prepArrivalInput.value = "";
        if (prepBasisInput) prepBasisInput.value = "";
        if (prepResidentResults) {
            prepResidentResults.hidden = true;
            prepResidentResults.replaceChildren();
        }
        if (prepSuggestions) {
            prepSuggestions.hidden = true;
            prepSuggestions.replaceChildren();
        }
        renderSelectedResident();
        renderResidentKind();
    }

    function openPreparationForm() {
        var bed = selectedBed();
        if (!bed || bed.dataset.occupied === "true" || !controlHeld() || !isTransferredBed(bed)) return;
        state.formOpen = true;
        state.formBedId = bed.dataset.bedId;
        setHidden(prepActions, true);
        setHidden(prepForm, false);
        setHidden(settlementForm, true);
        restoreDraft();
        renderResidentKind();
        if (prepResidentSearch) prepResidentSearch.focus();
    }

    function closePreparationForm() {
        captureDraft();
        state.formOpen = false;
        setHidden(prepForm, true);
        setHidden(prepExtendBlock, true);
        syncPanel();
    }

    function renderPreparationBlock(preparation) {
        if (prepResidentLabel) {
            prepResidentLabel.replaceChildren("Подготовлено для: " + (preparation.resident_name || "") + " ",
                window.settlementPersonnelNumber(preparation.personnel_number));
        }
        if (prepHoldLabel) {
            prepHoldLabel.textContent = "До: " + formatMoment(preparation.hold_until);
        }
        if (prepBasisLabel) {
            prepBasisLabel.textContent = "Основание: " + (preparation.basis || "не указано");
        }
        setHidden(prepExpiredHint, !preparation.is_expired);
        if (prepConfirmButton) {
            prepConfirmButton.disabled = Boolean(preparation.is_expired) || state.saving;
        }
        if (prepCancelButton) prepCancelButton.disabled = state.saving;
        if (prepExtendOpen) prepExtendOpen.disabled = state.saving;
    }

    function syncPanel() {
        if (!prepBlock || !prepActions || !reserveActions || !prepForm) return;
        var bed = selectedBed();
        var bedId = bed ? bed.dataset.bedId : "";
        var preparation = preparationForBed(bed);
        var reserve = bedId ? state.reserves[bedId] : null;

        if (state.formOpen && state.formBedId && state.formBedId !== bedId) {
            captureDraft();
        }
        if (panel && panel.hidden) {
            captureDraft();
            state.formOpen = false;
        }
        if (state.panelBedId !== bedId) {
            state.panelBedId = bedId;
            setHidden(prepExtendBlock, true);
            if (prepExtendOpen) prepExtendOpen.setAttribute("aria-expanded", "false");
            if (prepExtendReasonInput) prepExtendReasonInput.value = "";
            if (prepExtendHoldInput) prepExtendHoldInput.value = "";
        }

        var available = Boolean(bed && controlHeld() && isTransferredBed(bed) && (!panel || !panel.hidden));
        var free = available && !preparation && bed.dataset.occupied !== "true";
        if (!free) state.formOpen = false;
        var showingForm = free && state.formOpen;
        if (checkinForm) setHidden(checkinForm, !free || showingForm || !checkinForm.dataset.targetBed);

        setHidden(prepBlock, !available || !preparation);
        setHidden(prepActions, !free || showingForm);
        setHidden(reserveActions, !available);
        setHidden(prepForm, !showingForm);
        if (!available) return;

        if (preparation) {
            renderPreparationBlock(preparation);
            setHidden(settlementForm, true);
            setHidden(occupiedActions, true);
        } else if (free) {
            if (showingForm) state.formBedId = bedId;
            setHidden(settlementForm, showingForm);
        }

        if (reserveButton) {
            reserveButton.textContent = reserve ? "Снять резерв" : "В резерв";
            reserveButton.dataset.bedReserveMode = reserve ? "clear" : "set";
            reserveButton.disabled = state.saving;
        }
    }

    function submitPreparation() {
        var bed = selectedBed();
        if (!bed || state.saving) return;
        if (!state.draft.resident) {
            showFeedback("Выберите жильца из результатов поиска.", true);
            return;
        }
        var arrivalDate = prepArrivalInput ? prepArrivalInput.value : "";
        var basis = prepBasisInput ? prepBasisInput.value.trim() : "";
        if (!arrivalDate) {
            showFeedback("Укажите дату приезда.", true);
            return;
        }
        if (!basis) {
            showFeedback("Основание обязательно.", true);
            return;
        }
        state.draft.arrivalDate = arrivalDate;
        state.draft.basis = basis;
        runAction(
            root.dataset.bedPreparationPrepareUrl,
            {
                resident_id: state.draft.resident.resident_id,
                bed_stable_id: bed.dataset.bedId,
                arrival_date: arrivalDate,
                basis: basis
            },
            function () {
                state.formOpen = false;
                state.formBedId = "";
                setHidden(prepForm, true);
                resetPreparationDraft();
                return loadState().then(function () {
                    if (prepConfirmButton) prepConfirmButton.focus();
                });
            }
        );
    }

    function confirmPreparation(preparationId) {
        var preparation = preparationById(preparationId);
        var name = preparation ? preparation.resident_name : "";
        var message = name
            ? "Подтвердить приезд: " + name + "?"
            : "Подтвердить приезд?";
        if (!window.confirm(message)) return;
        runAction(
            bedUrl(root.dataset.bedPreparationConfirmUrl, preparationId),
            {},
            function () {
                showFeedback("Приезд подтверждён. Обновляем карту…", false);
                window.location.reload();
            }
        );
    }

    function cancelPreparation(preparationId) {
        var reason = askReason("Причина снятия подготовки");
        if (!reason) return;
        runAction(
            bedUrl(root.dataset.bedPreparationCancelUrl, preparationId),
            {reason: reason},
            function () {
                setHidden(prepExtendBlock, true);
                return loadState();
            }
        );
    }

    function submitPreparationExtension(preparationId) {
        if (state.saving) return;
        var holdValue = prepExtendHoldInput ? prepExtendHoldInput.value : "";
        var reason = prepExtendReasonInput ? prepExtendReasonInput.value.trim() : "";
        if (!holdValue) {
            showFeedback("Укажите новый срок.", true);
            return;
        }
        if (!reason) {
            showFeedback("Причина продления обязательна.", true);
            return;
        }
        var parts = holdValue.split("-");
        var endOfDay = new Date(
            Number(parts[0]),
            Number(parts[1]) - 1,
            Number(parts[2]),
            23,
            59,
            59
        );
        runAction(
            bedUrl(root.dataset.bedPreparationExtendUrl, preparationId),
            {hold_until: endOfDay.toISOString(), reason: reason},
            function () {
                setHidden(prepExtendBlock, true);
                if (prepExtendReasonInput) prepExtendReasonInput.value = "";
                return loadState();
            }
        );
    }

    /* --- Список «Временные» в ящике «Нерасселённые» ---------------------- */

    function scopeTotal() {
        return (
            Number(state.counts.prepared || 0)
            + Number(state.counts.temporary || 0)
            + Number(state.counts.reserves || 0)
        );
    }

    function buildTemporaryRow(options) {
        var row = element("article", "settlement-temporary-row" + (options.modifier || ""));
        var copy = element("div", "settlement-temporary-row-copy");
        copy.appendChild(element("strong", "", options.title));
        if ("personnelNumber" in options) copy.appendChild(window.settlementPersonnelNumber(options.personnelNumber));
        (options.meta || []).forEach(function (line) {
            copy.appendChild(element("small", "", line));
        });
        /* Предложение переселения: первая строка — статус плановой койки,
           вторая (если есть) — пояснение сервера. */
        (options.suggestionLines || []).forEach(function (line, index) {
            copy.appendChild(element(
                "small",
                index === 0 ? "is-suggestion" : "",
                line
            ));
        });
        row.appendChild(copy);
        var actions = element("div", "settlement-temporary-row-actions");
        (options.actions || []).forEach(function (action) {
            var button = element(
                "button",
                "settlement-temporary-button" + (action.danger ? " is-danger" : ""),
                action.label
            );
            button.type = "button";
            button.addEventListener("click", action.handler);
            actions.appendChild(button);
        });
        row.appendChild(actions);
        return row;
    }

    function preparationRowNode(row) {
        return buildTemporaryRow({
            title: row.resident_name || "",
            personnelNumber: row.personnel_number,
            meta: [
                row.bed_label || "",
                "До: " + formatMoment(row.hold_until),
                "Основание: " + (row.basis || "не указано"),
                row.is_expired ? "Срок истёк" : "",
                row.is_reserve ? "Резерв" : ""
            ].filter(Boolean),
            actions: [
                {
                    label: "Подтвердить",
                    handler: function () {
                        confirmPreparation(row.id);
                    }
                },
                {
                    label: "Продлить",
                    handler: function () {
                        focusBed(row.bed_stable_id);
                        openExtensionForm(row.id);
                    }
                },
                {
                    label: "Отменить",
                    danger: true,
                    handler: function () {
                        cancelPreparation(row.id);
                    }
                }
            ]
        });
    }

    function relocationSuggestionFor(row) {
        if (!row) return null;
        if (row.occupancy_id === null || row.occupancy_id === undefined) return null;
        return state.relocationByOccupancy[String(row.occupancy_id)] || null;
    }

    function relocationStatusLines(suggestion) {
        var label = suggestion.planned_bed_label
            ? String(suggestion.planned_bed_label)
            : "";
        var lines = [];
        if (suggestion.status === "ready") {
            lines.push(label ? "Плановая койка: " + label : "Плановая койка");
        } else if (suggestion.status === "planned_bed_occupied") {
            lines.push(label ? "Плановая койка занята: " + label : "Плановая койка занята");
        } else if (suggestion.status === "not_in_plan") {
            lines.push("Нет в утверждённом списке");
        } else if (suggestion.status === "no_bed") {
            lines.push("Плановая койка не определена");
        }
        if (suggestion.reason) lines.push(String(suggestion.reason));
        return lines;
    }

    /* Переселение на плановую койку — только по кнопке и только с причиной.
       Автоматического переселения нет: это тот же серверный маршрут
       temporary/relocate, что и у ручного переселения. */
    function relocateTemporaryToPlanned(suggestion) {
        if (!suggestion) return;
        if (!suggestion.occupancy_id || !suggestion.planned_bed_stable_id) return;
        var reason = askReason("Причина переселения");
        if (!reason) return;
        runAction(
            root.dataset.temporaryRelocateUrl,
            {
                occupancy_id: Number(suggestion.occupancy_id),
                bed_stable_id: suggestion.planned_bed_stable_id,
                reason: reason
            },
            function () {
                showFeedback("Временное размещение переселено. Обновляем карту…", false);
                window.location.reload();
            }
        );
    }

    function temporaryRowNode(row) {
        var suggestion = relocationSuggestionFor(row);
        var actions = [
            {
                label: "Переселить",
                handler: function () {
                    root.dispatchEvent(new CustomEvent("settlement:choose-move-bed", {
                        detail: {occupancyId: row.occupancy_id}
                    }));
                }
            },
            {
                label: "Освободить",
                danger: true,
                handler: function () {
                    releaseTemporary(row.occupancy_id);
                }
            }
        ];
        if (suggestion && suggestion.status === "ready" && suggestion.planned_bed_stable_id) {
            actions.unshift({
                label: "Переселить на плановую",
                handler: function () {
                    relocateTemporaryToPlanned(suggestion);
                }
            });
        }
        return buildTemporaryRow({
            modifier: " is-temporary",
            title: row.resident_name || "",
            personnelNumber: row.personnel_number,
            meta: [
                row.bed_label || "",
                row.resident_kind || "",
                "С " + formatMoment(row.starts_at),
                row.is_reserve ? "Резерв" : ""
            ].filter(Boolean),
            suggestionLines: suggestion ? relocationStatusLines(suggestion) : [],
            actions: actions
        });
    }

    function reserveRowNode(row) {
        return buildTemporaryRow({
            modifier: " is-reserve",
            title: row.bed_label || "",
            meta: ["Причина: " + (row.reason || "не указана")],
            actions: [
                {
                    label: "Снять резерв",
                    handler: function () {
                        focusBed(row.bed_stable_id);
                        toggleReserve(row.bed_stable_id, "clear");
                    }
                }
            ]
        });
    }

    function renderTemporaryList() {
        if (!temporaryList) return;
        temporaryList.replaceChildren();
        var groups = [
            ["Подготовлено", state.preparationRows, preparationRowNode],
            ["Временно (ждёт утверждения)", state.temporaryRows, temporaryRowNode],
            ["Резерв", state.reserveRows, reserveRowNode]
        ];
        groups.forEach(function (group) {
            if (!group[1].length) return;
            var section = element("section", "settlement-temporary-group");
            section.appendChild(element("h3", "", group[0]));
            group[1].forEach(function (row) {
                section.appendChild(group[2](row));
            });
            temporaryList.appendChild(section);
        });
        var total = scopeTotal();
        if (temporaryEmpty) temporaryEmpty.hidden = !state.loaded || total !== 0;
        if (scopeCount) {
            scopeCount.textContent = state.loaded ? String(total) : "";
            scopeCount.hidden = !state.loaded || total === 0;
        }
        if (temporarySummary) {
            if (!state.loaded) {
                temporarySummary.textContent = "";
            } else {
                var parts = [
                    "Подготовлено: " + Number(state.counts.prepared || 0),
                    "Временно: " + Number(state.counts.temporary || 0),
                    "Резерв: " + Number(state.counts.reserves || 0)
                ];
                if (Number(state.counts.expired_preparations || 0)) {
                    parts.push("Просрочено: " + Number(state.counts.expired_preparations));
                }
                temporarySummary.textContent = parts.join(" · ");
            }
        }
    }

    function setScope(scope) {
        state.scope = scope === "temporary" ? "temporary" : "unsettled";
        var temporary = state.scope === "temporary";
        clearDrawerMessage("");
        scopeButtons.forEach(function (button) {
            var active = button.dataset.temporaryScope === state.scope;
            button.classList.toggle("is-active", active);
            button.setAttribute("aria-pressed", String(active));
        });
        if (unsettledFields) unsettledFields.hidden = temporary;
        if (unsettledBody) unsettledBody.hidden = temporary;
        if (temporaryBody) temporaryBody.hidden = !temporary;
        if (temporary) {
            renderTemporaryList();
            loadState();
        }
    }

    function focusBed(bedStableId) {
        var bed = findBed(bedStableId);
        if (!bed) return false;
        var room = bed.closest("[data-room-card]");
        if (room) {
            var dormButton = root.querySelector('[data-dorm-filter="' + room.dataset.dormitory + '"]');
            var floorButton = root.querySelector('[data-floor-filter="' + room.dataset.floor + '"]');
            if (dormButton && !dormButton.classList.contains("is-active")) dormButton.click();
            if (floorButton && !floorButton.classList.contains("is-active")) floorButton.click();
        }
        bed.click();
        return true;
    }

    function openExtensionForm(preparationId) {
        var preparation = state.preparations[selectedBed() ? selectedBed().dataset.bedId : ""];
        if (!preparation || Number(preparation.id) !== Number(preparationId)) return;
        if (!prepExtendBlock) return;
        prepExtendBlock.hidden = false;
        if (prepExtendOpen) prepExtendOpen.setAttribute("aria-expanded", "true");
        if (prepExtendHoldInput) {
            prepExtendHoldInput.min = String(root.dataset.currentDate || "");
            prepExtendHoldInput.focus();
        }
    }

    function toggleReserve(bedStableId, mode) {
        var reason = mode === "set" ? "Постановка койки в резерв с карты" : "Снятие резерва с карты";
        runAction(
            root.dataset.bedReserveUrl,
            {bed_stable_id: bedStableId, action: mode, reason: reason}
        );
    }

    function releaseTemporary(occupancyId) {
        if (!occupancyId) return;
        runAction(
            root.dataset.temporaryReleaseUrl,
            {occupancy_id: Number(occupancyId), reason: "Освобождение койки с карты"},
            function () {
                showFeedback("Койко-место освобождено. Обновляем карту…", false);
                window.location.reload();
            }
        );
    }

    /* --- Поиск жильца для подготовки ------------------------------------- */

    function residentKind() {
        return state.draft.kind === RESIDENT_KIND_GUEST
            ? RESIDENT_KIND_GUEST
            : RESIDENT_KIND_EMPLOYEE;
    }

    function residentMatchesKind(resident) {
        var type = String(resident && resident.resident_type ? resident.resident_type : "").toUpperCase();
        if (residentKind() === RESIDENT_KIND_GUEST) {
            return GUEST_RESIDENT_TYPES[type] === true;
        }
        return type === "EMPLOYEE";
    }

    function renderResidentKind() {
        var kind = residentKind();
        prepKindButtons.forEach(function (button) {
            var active = button.dataset.preparationKind === kind;
            button.classList.toggle("is-active", active);
            button.setAttribute("aria-pressed", String(active));
        });
        if (prepResidentKindLabel) {
            prepResidentKindLabel.textContent = kind === RESIDENT_KIND_GUEST
                ? "Гость по пропуску"
                : "Жилец по пропуску";
        }
    }

    function setResidentKind(kind) {
        var next = kind === RESIDENT_KIND_GUEST
            ? RESIDENT_KIND_GUEST
            : RESIDENT_KIND_EMPLOYEE;
        if (residentKind() === next) return;
        state.draft.kind = next;
        state.draft.resident = null;
        if (prepResidentResults) {
            prepResidentResults.hidden = true;
            prepResidentResults.replaceChildren();
        }
        renderSelectedResident();
        renderResidentKind();
        var query = prepResidentSearch ? prepResidentSearch.value.trim() : "";
        if (query.length >= 2) runResidentSearch();
    }

    function residentMetaLine(resident) {
        var meta = resident.meta || {};
        var parts = [resident.resident_type_label || ""];
        if (residentKind() === RESIDENT_KIND_GUEST) {
            /* Тип гостя (командированный / другой внешний) уже в первой части. */
            if (meta.organization) parts.push(meta.organization);
            if (meta.position) parts.push(meta.position);
        } else {
            if (meta.position) parts.push(meta.position);
        }
        return parts.filter(Boolean).join(" · ");
    }

    function renderResidentResults(results) {
        if (!prepResidentResults) return;
        prepResidentResults.replaceChildren();
        var rows = (results || []).filter(residentMatchesKind);
        if (!rows.length) {
            var message = RESIDENT_KIND_EMPTY[residentKind()];
            if ((results || []).length) {
                /* Сервер ограничивает выдачу 12 строками; если он не знает
                   фильтр по типу, нужный тип мог не попасть в ответ. Это не
                   «ничего не найдено», а «уточните запрос». */
                message = residentKind() === RESIDENT_KIND_GUEST
                    ? "Среди найденных карточек гостей нет. Уточните ФИО или организацию."
                    : "Среди найденных карточек сотрудников нет. Уточните ФИО или табельный номер.";
            }
            prepResidentResults.appendChild(element("p", "settlement-placement-hint", message));
        } else {
            rows.forEach(function (resident) {
                var button = element("button", "settlement-employee-result");
                button.type = "button";
                button.appendChild(element("strong", "", resident.full_name || ""));
                button.appendChild(window.settlementPersonnelNumber(resident.meta?.personnel_number));
                button.appendChild(element("small", "", residentMetaLine(resident)));
                button.addEventListener("click", function () {
                    chooseResident(resident);
                });
                prepResidentResults.appendChild(button);
            });
        }
        prepResidentResults.hidden = false;
    }

    function chooseResident(resident) {
        state.draft.resident = resident;
        if (prepResidentResults) prepResidentResults.hidden = true;
        if (prepResidentSearch) prepResidentSearch.value = resident.full_name || "";
        renderSelectedResident();
    }

    function residentSearchRequest(query, residentType) {
        var body = {q: query};
        if (residentType) body.resident_type = residentType;
        return fetch(root.dataset.residentSearchUrl, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrfToken(),
                "X-Requested-With": "XMLHttpRequest"
            },
            body: JSON.stringify(body)
        }).then(function (response) {
            return response.json()
                .catch(function () { return {}; })
                .then(function (payload) {
                    if (!response.ok || !payload.ok) {
                        var error = new Error(
                            payload && payload.error
                                ? payload.error
                                : "Не удалось выполнить поиск."
                        );
                        error.code = payload && payload.code ? payload.code : "";
                        error.httpStatus = response.status;
                        throw error;
                    }
                    return payload;
                });
        });
    }

    function runResidentSearch() {
        if (!prepResidentSearch) return;
        var query = prepResidentSearch.value.trim();
        state.draft.query = query;
        state.draft.resident = null;
        renderSelectedResident();
        if (query.length < 2) {
            if (prepResidentResults) {
                prepResidentResults.hidden = true;
                prepResidentResults.replaceChildren();
            }
            return;
        }
        state.searchSequence += 1;
        var sequence = state.searchSequence;
        var searchType = RESIDENT_KIND_SEARCH_TYPE[residentKind()];
        residentSearchRequest(query, searchType)
            .catch(function (error) {
                /* Сервер без backend-20 не знает resident_type=GUEST и отвечает
                   400. Повторяем без параметра: клиентский фильтр по типам
                   остаётся страховкой, поиск не ломается. */
                if (searchType === "GUEST" && error && Number(error.httpStatus) === 400) {
                    return residentSearchRequest(query, null);
                }
                throw error;
            })
            .then(function (payload) {
                if (sequence !== state.searchSequence) return;
                renderResidentResults(payload.results || []);
            })
            .catch(function (error) {
                if (sequence !== state.searchSequence) return;
                showFeedback(error.message || "Не удалось выполнить поиск.", true);
            });
    }

    function renderSuggestions(payload) {
        if (!prepSuggestions) return;
        prepSuggestions.replaceChildren();
        var suggestions = payload.suggestions || [];
        if (!suggestions.length) {
            var hint = payload.reason && payload.reason.message
                ? payload.reason.message
                : "Подходящих коек нет.";
            prepSuggestions.appendChild(element("p", "settlement-placement-hint", hint));
            prepSuggestions.hidden = false;
            return;
        }
        suggestions.forEach(function (suggestion) {
            var button = element("button", "settlement-employee-result");
            button.type = "button";
            button.appendChild(element("strong", "", suggestion.bed_label || ""));
            button.appendChild(element(
                "small",
                "",
                suggestion.is_reserve ? "Резерв" : "Свободная койка"
            ));
            button.addEventListener("click", function () {
                if (prepSuggestions) prepSuggestions.hidden = true;
                captureDraft();
                focusBed(suggestion.bed_stable_id);
            });
            prepSuggestions.appendChild(button);
        });
        prepSuggestions.hidden = false;
    }

    function requestSuggestions() {
        if (!state.draft.resident) {
            showFeedback("Сначала выберите жильца.", true);
            return;
        }
        var arrivalDate = prepArrivalInput ? prepArrivalInput.value : "";
        if (!arrivalDate) {
            showFeedback("Укажите дату приезда.", true);
            return;
        }
        state.draft.arrivalDate = arrivalDate;
        requestJson(root.dataset.bedPreparationSuggestUrl, {
            resident_id: state.draft.resident.resident_id,
            arrival_date: arrivalDate
        })
            .then(renderSuggestions)
            .catch(function (error) {
                showFeedback(
                    error && error.message
                        ? error.message
                        : "Не удалось подобрать койку.",
                    true
                );
            });
    }

    /* --- Административный перехват управления ---------------------------- */

    function takeoverControl() {
        if (!takeoverButton || state.saving) return;
        var reason = askReason("Причина перехвата управления");
        if (!reason) return;
        state.saving = true;
        takeoverButton.disabled = true;
        showControlFeedback("", false);
        requestJson(root.dataset.controlTakeoverUrl, {reason: reason})
            .then(function () {
                showControlFeedback("Управление получено. Обновляем карту…", false);
                window.location.reload();
            })
            .catch(function (error) {
                showControlFeedback(
                    error && error.message
                        ? error.message
                        : "Не удалось забрать управление.",
                    true
                );
            })
            .finally(function () {
                state.saving = false;
                takeoverButton.disabled = false;
            });
    }

    /* --- Обработчики ----------------------------------------------------- */

    scopeButtons.forEach(function (button) {
        button.addEventListener("click", function () {
            setScope(button.dataset.temporaryScope);
        });
    });

    root.querySelectorAll("[data-unsettled-panel-toggle]").forEach(function (toggle) {
        toggle.addEventListener("click", function () {
            if (!unsettledPanel || unsettledPanel.hidden) return;
            if (state.scope !== "temporary") return;
            clearDrawerMessage("");
            loadState();
            var active = scopeButtons.find(function (button) {
                return button.dataset.temporaryScope === "temporary";
            });
            if (active) active.focus();
        });
    });

    if (prepOpenButton) {
        prepOpenButton.addEventListener("click", openPreparationForm);
    }
    if (prepFormCancel) {
        prepFormCancel.addEventListener("click", closePreparationForm);
    }
    if (prepSubmitButton) {
        prepSubmitButton.addEventListener("click", submitPreparation);
    }
    if (prepForm) {
        prepForm.addEventListener("submit", function (event) {
            event.preventDefault();
            submitPreparation();
        });
    }
    prepKindButtons.forEach(function (button) {
        button.addEventListener("click", function () {
            setResidentKind(button.dataset.preparationKind);
        });
    });
    renderResidentKind();
    if (prepResidentSearch) {
        prepResidentSearch.addEventListener("input", function () {
            window.clearTimeout(state.searchTimer);
            state.searchTimer = window.setTimeout(runResidentSearch, 220);
        });
    }
    if (prepSuggestButton) {
        prepSuggestButton.addEventListener("click", requestSuggestions);
    }
    if (prepArrivalInput) {
        prepArrivalInput.addEventListener("change", function () {
            state.draft.arrivalDate = prepArrivalInput.value;
        });
    }
    if (prepBasisInput) {
        prepBasisInput.addEventListener("input", function () {
            state.draft.basis = prepBasisInput.value;
        });
    }
    if (prepConfirmButton) {
        prepConfirmButton.addEventListener("click", function () {
            var bed = selectedBed();
            var preparation = preparationForBed(bed);
            if (!preparation) return;
            confirmPreparation(preparation.id);
        });
    }
    if (prepExtendOpen) {
        prepExtendOpen.addEventListener("click", function () {
            var bed = selectedBed();
            var preparation = preparationForBed(bed);
            if (!preparation) return;
            if (!prepExtendBlock) return;
            var open = prepExtendBlock.hidden;
            prepExtendBlock.hidden = !open;
            prepExtendOpen.setAttribute("aria-expanded", String(open));
            if (open) {
                if (prepExtendHoldInput) {
                    prepExtendHoldInput.min = String(root.dataset.currentDate || "");
                    prepExtendHoldInput.focus();
                }
            }
        });
    }
    if (prepExtendSubmit) {
        prepExtendSubmit.addEventListener("click", function () {
            var bed = selectedBed();
            var preparation = preparationForBed(bed);
            if (!preparation) return;
            submitPreparationExtension(preparation.id);
        });
    }
    if (prepCancelButton) {
        prepCancelButton.addEventListener("click", function () {
            var bed = selectedBed();
            var preparation = preparationForBed(bed);
            if (!preparation) return;
            cancelPreparation(preparation.id);
        });
    }
    if (reserveButton) {
        reserveButton.addEventListener("click", function () {
            var bed = selectedBed();
            if (!bed) return;
            toggleReserve(
                bed.dataset.bedId,
                reserveButton.dataset.bedReserveMode === "clear" ? "clear" : "set"
            );
        });
    }
    if (takeoverButton) {
        takeoverButton.addEventListener("click", takeoverControl);
    }

    /* Освобождение и переселение временного жильца обрабатывают маршруты T-029. */
    root.addEventListener("click", function (event) {
        var release = event.target.closest("[data-release-button]");
        if (release) {
            var releaseBed = selectedBed();
            if (!releaseBed || releaseBed.dataset.assignmentType !== "temporary_unapproved") return;
            event.preventDefault();
            event.stopImmediatePropagation();
            releaseTemporary(Number(releaseBed.dataset.occupancyId));
            return;
        }
    }, true);

    /* Обновление должно быть идемпотентным: наблюдатель также получает
       изменения hidden, которые вносит syncPanel. */
    if (placementPanel && typeof window.MutationObserver === "function") {
        var observer = new window.MutationObserver(function () {
            syncPanel();
        });
        observer.observe(placementPanel, {
            attributes: true,
            subtree: true,
            attributeFilter: ["hidden"]
        });
        observer.observe(root, {
            attributes: true,
            attributeFilter: ["data-control-state"]
        });
        if (panel) {
            observer.observe(panel, {
                attributes: true,
                attributeFilter: ["hidden"]
            });
        }
    }

    root.addEventListener("settlement:checked-in", loadState);
    loadState();
}());
