/* T-036 W3: «одна кнопка» авторасселения в существующей панели делопроизводителя.
   Поток: «Рассчитать расселение» → сводка и исключения → «Подтвердить» →
   «Применить» (день и ночь последовательно, независимо).

   Границы:
   - маршруты берутся из data-атрибутов корня карты (data-auto-state-url,
     data-auto-create-url, data-auto-confirm-url, data-auto-apply-day-url,
     data-auto-apply-night-url). Новых endpoints нет;
   - карта, комнаты, _room_card.html и settlement-clerk.js не меняются;
   - состояние и разметка панели не перерисовываются целиком: только блок
     [data-settlement-one-button].

   Прогресс: состояние auto/state не отдаёт поле прогресса расчёта
   (проверено в settlement/views.py:settlement_auto_state_view), поэтому
   показывается честный таймер со спиннером, без выдуманных процентов. */
(function () {
    "use strict";

    var root = document.querySelector("[data-settlement-root]");
    if (!root) return;

    var block = root.querySelector("[data-settlement-one-button]");
    if (!block) return;

    var feedback = block.querySelector("[data-one-button-feedback]");
    var empty = block.querySelector("[data-one-button-empty]");
    var leaseHint = block.querySelector("[data-one-button-lease-hint]");
    var cohortRow = block.querySelector("[data-one-button-cohort-row]");
    var cohortSelect = block.querySelector("[data-one-button-cohort]");
    var cohortCopy = block.querySelector("[data-one-button-cohort-copy]");
    var progress = block.querySelector("[data-one-button-progress]");
    var progressLabel = block.querySelector("[data-one-button-progress-label]");
    var progressElapsed = block.querySelector("[data-one-button-progress-elapsed]");
    var summary = block.querySelector("[data-one-button-summary]");
    var exceptions = block.querySelector("[data-one-button-exceptions]");
    var shifts = block.querySelector("[data-one-button-shifts]");
    var calculateButton = block.querySelector("[data-one-button-calculate]");
    var confirmButton = block.querySelector("[data-one-button-confirm]");
    var applyButton = block.querySelector("[data-one-button-apply]");
    var refreshButton = block.querySelector("[data-one-button-refresh]");

    /* Порядок применения: день, затем ночь — как в постановке. Смены
       применяются независимо: ошибка одной не отменяет вторую. */
    var SHIFTS = ["day", "night"];
    var SHIFT_LABELS = {day: "Дневная смена", night: "Ночная смена"};
    var RUN_STATUS_LABELS = {
        draft: "Черновик",
        confirmed: "Подтверждён",
        superseded: "Заменён"
    };

    var state = {
        payload: null,
        cohortId: null,
        loading: false,
        busy: false,
        readSequence: 0,
        timer: null,
        startedAt: 0,
        results: {}
    };

    /* --- Мелкие помощники ------------------------------------------------- */

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
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

    function formatIsoDate(value) {
        var parts = String(value || "").split("-");
        if (parts.length !== 3) return String(value || "");
        return parts[2] + "." + parts[1] + "." + parts[0];
    }

    function formatMoment(value) {
        if (!value) return "";
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

    function shortCode(code) {
        var parts = String(code || "").split(".");
        return parts[parts.length - 1] || "";
    }

    function shiftLabel(shift) {
        return SHIFT_LABELS[shift] || shift;
    }

    function currentPreview() {
        return state.payload && state.payload.preview ? state.payload.preview : null;
    }

    function controlHeld() {
        return root.dataset.controlState === "held";
    }

    /* --- Обратная связь, прогресс, аренда управления ---------------------- */

    function showFeedback(message, kind) {
        if (!feedback) return;
        feedback.textContent = "";
        feedback.hidden = !message;
        feedback.className = "settlement-one-button-feedback" + (kind ? " is-" + kind : "");
        if (!message) return;
        feedback.appendChild(el("strong", "", message));
    }

    function startProgress(label) {
        state.startedAt = Date.now();
        if (progressLabel) progressLabel.textContent = label;
        if (progressElapsed) progressElapsed.textContent = "0 с";
        setHidden(progress, false);
        stopTimer();
        state.timer = window.setInterval(function () {
            if (!progressElapsed) return;
            var seconds = Math.floor((Date.now() - state.startedAt) / 1000);
            progressElapsed.textContent = seconds + " с";
        }, 1000);
    }

    function stopTimer() {
        if (state.timer === null) return;
        window.clearInterval(state.timer);
        state.timer = null;
    }

    function stopProgress() {
        stopTimer();
        setHidden(progress, true);
    }

    function leaseMessage() {
        var controlState = root.dataset.controlState || "";
        if (controlState === "held") return "";
        if (controlState === "busy") {
            return "Управление расселением занято другим сотрудником. Пока управление занято, сервер отклонит расчёт и применение.";
        }
        if (controlState === "lost") {
            return "Связь с управлением потеряна. Нажмите «Начать работу» в верхней панели, иначе сервер отклонит действие.";
        }
        if (controlState === "free") {
            return "Сначала нажмите «Начать работу» в верхней панели: без управления расселением сервер отклонит расчёт и применение.";
        }
        return "Проверяем управление расселением…";
    }

    function renderLeaseHint() {
        if (!leaseHint) return;
        var message = leaseMessage();
        leaseHint.textContent = message;
        leaseHint.className = "settlement-one-button-lease-hint" + (message ? " is-warning" : "");
        setHidden(leaseHint, !message);
    }

    /* --- Сеть ------------------------------------------------------------- */

    function requestJson(url, options) {
        var config = options || {};
        return fetch(url, {
            method: config.method || "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrfToken(),
                "X-Requested-With": "XMLHttpRequest"
            },
            body: config.payload === undefined ? undefined : JSON.stringify(config.payload)
        }).then(function (response) {
            if (response.redirected || response.status === 401) {
                var sessionError = new Error("Сессия истекла, войдите заново.");
                sessionError.httpStatus = response.status || 401;
                throw sessionError;
            }
            return response.json()
                .catch(function () { return {}; })
                .then(function (body) {
                    if (!response.ok || body.ok === false) {
                        var error = new Error(
                            body && body.error
                                ? body.error
                                : "Сервер не выполнил действие."
                        );
                        error.code = body && body.code ? body.code : "";
                        error.details = body && body.details ? body.details : {};
                        error.httpStatus = response.status;
                        throw error;
                    }
                    return body;
                });
        }, function () {
            var networkError = new Error(
                "Не удалось связаться с сервером. Проверьте связь и повторите."
            );
            networkError.httpStatus = 0;
            throw networkError;
        });
    }

    function errorMessage(error) {
        var message = error && error.message ? String(error.message) : "";
        var code = shortCode(error && error.code);
        var status = error && error.httpStatus ? Number(error.httpStatus) : 0;
        if (!status) {
            return message || "Не удалось связаться с сервером. Проверьте связь и повторите.";
        }
        if (code === "not_held") {
            return "Управление расселением не начато. Нажмите «Начать работу» в верхней панели и повторите действие.";
        }
        if (code === "session_mismatch") {
            return "Управление расселением принадлежит другой сессии. Нажмите «Начать работу» заново и повторите действие.";
        }
        if (status === 403) {
            return "Нет доступа к авторасселению (403). " + (message || "Обратитесь к администратору.");
        }
        if (status >= 500) {
            return "Сервер не смог выполнить действие (ошибка " + status + "). "
                + (message || "Повторите позже.");
        }
        if (status === 409) {
            return (message || "Сервер отклонил действие.")
                + (error.code ? " (" + error.code + ")" : "");
        }
        return message || ("Не удалось выполнить действие (ошибка " + status + ").");
    }

    function canMutate() {
        return Boolean(
            !state.busy
            && !state.loading
            && controlHeld()
            && state.payload
            && state.payload.state !== "no_cohort"
            && (state.payload.cohorts || []).length
        );
    }

    /* --- Кнопки ----------------------------------------------------------- */

    function previewHasReadyShift(preview) {
        if (!preview || !preview.shift_apply) return false;
        return SHIFTS.some(function (shift) {
            var row = preview.shift_apply[shift];
            return Boolean(row && row.status === "ready");
        });
    }

    function syncButtons() {
        var preview = currentPreview();
        var allowed = canMutate();
        if (calculateButton) calculateButton.disabled = !allowed;
        if (refreshButton) refreshButton.disabled = state.busy || state.loading;
        if (cohortSelect) cohortSelect.disabled = state.busy || state.loading || !state.payload;
        if (confirmButton) {
            var showConfirm = Boolean(preview && preview.status === "draft");
            setHidden(confirmButton, !showConfirm);
            confirmButton.disabled = !allowed || !showConfirm || Boolean(preview && preview.stale);
        }
        if (applyButton) {
            var showApply = Boolean(preview);
            setHidden(applyButton, !showApply);
            applyButton.disabled = !allowed
                || !showApply
                || preview.status !== "confirmed"
                || Boolean(preview.stale)
                || !previewHasReadyShift(preview);
        }
    }

    /* --- Отображение ------------------------------------------------------ */

    function residentLabel(resident) {
        if (!resident) return "Жилец";
        var label = resident.name || "Жилец";
        if (resident.kind_label) label += " · " + resident.kind_label;
        if (resident.organization) label += " · " + resident.organization;
        return label;
    }

    function ruleChips(ruleIds) {
        if (!Array.isArray(ruleIds) || !ruleIds.length) return null;
        var wrap = el("span", "settlement-one-button-rules");
        ruleIds.forEach(function (ruleId) {
            var full = String(ruleId || "");
            if (!full) return;
            /* В реестре правило называется SET-R095; в интерфейсе показываем
               короткий номер, полный идентификатор остаётся в подсказке. */
            var short = full.indexOf("SET-") === 0 ? full.slice(4) : full;
            var chip = el("code", "settlement-one-button-rule", short);
            chip.title = full;
            wrap.appendChild(chip);
        });
        return wrap.children.length ? wrap : null;
    }

    function summaryCard(label, value, className, title) {
        var card = el("article", className || "");
        if (title) card.title = title;
        card.appendChild(el("span", "", label));
        card.appendChild(el("strong", "", value));
        return card;
    }

    function renderCohort(payload) {
        if (!cohortRow || !cohortSelect) return;
        var cohorts = (payload && payload.cohorts) || [];
        if (!cohorts.length) {
            cohortSelect.replaceChildren();
            setHidden(cohortRow, true);
            return;
        }
        var selectedId = String(payload.selected_cohort_id || cohorts[0].id);
        cohortSelect.replaceChildren();
        cohorts.forEach(function (cohort) {
            var period = cohort.watch_period || {};
            var option = new Option(
                (period.name || "Вахта")
                    + " · " + formatIsoDate(period.starts_on)
                    + " — " + formatIsoDate(period.ends_on)
                    + " · " + (cohort.member_count || 0) + " чел."
                    + " · версия " + (cohort.version || "—"),
                String(cohort.id),
                false,
                String(cohort.id) === selectedId
            );
            cohortSelect.appendChild(option);
        });
        var selected = cohorts.filter(function (cohort) {
            return String(cohort.id) === selectedId;
        })[0] || cohorts[0];
        if (cohortCopy) {
            var period = selected.watch_period || {};
            cohortCopy.textContent = "Выбран: " + (period.name || "вахта")
                + " (" + formatIsoDate(period.starts_on)
                + " — " + formatIsoDate(period.ends_on) + "), "
                + (selected.member_count || 0) + " человек.";
        }
        setHidden(cohortRow, false);
    }

    /* Сводка «Конфликты». preview.application — это только первое применение
       расчёта (settlement/views.py:1450-1451), поэтому после двух смен цифра
       была неполной (review-7, замечание 1). Источники по приоритету:
       1) сумма skipped_count применённых смен (backend-12). Одинаковый
          applied_at считается один раз: историческое применение всего плана
          (legacy_whole_run) отдаётся обеим сменам одной записью;
       2) старый ответ без shift_apply.skipped_count — прежний
          preview.application.skipped_count;
       3) данных нет — «—».
       Возвращает число или null. */
    function conflictCount(preview) {
        var shiftApply = preview && preview.shift_apply ? preview.shift_apply : null;
        if (shiftApply) {
            var seenApplications = {};
            var total = 0;
            var counted = 0;
            var complete = true;
            appliedShiftNames(preview).forEach(function (shift) {
                var row = shiftApply[shift];
                var applicationKey = row && row.applied_at ? String(row.applied_at) : "";
                if (applicationKey && seenApplications[applicationKey]) return;
                if (applicationKey) seenApplications[applicationKey] = true;
                if (row && typeof row.skipped_count === "number") {
                    total += row.skipped_count;
                    counted += 1;
                } else {
                    complete = false;
                }
            });
            if (counted > 0 && complete) return total;
        }
        if (preview && preview.application) {
            return Number(preview.application.skipped_count || 0);
        }
        return null;
    }

    function renderSummary(preview) {
        if (!summary) return;
        summary.replaceChildren();
        if (!preview) {
            setHidden(summary, true);
            return;
        }
        var conflicts = conflictCount(preview);
        summary.appendChild(summaryCard("Размещено автоматически", preview.placement_count || 0, "is-success"));
        summary.appendChild(summaryCard(
            "Не размещено",
            preview.unresolved_count || 0,
            preview.unresolved_count ? "is-conflict" : ""
        ));
        summary.appendChild(summaryCard(
            "Конфликты",
            conflicts === null ? "—" : conflicts,
            conflicts ? "is-conflict" : "",
            conflicts === null
                ? "Появятся после применения: сколько строк плана сервер пропустил."
                : "Строки плана, которые сервер не применил."
        ));
        var heading = el("p", "settlement-one-button-run");
        heading.appendChild(el(
            "strong",
            "",
            "Расчёт № " + (preview.version || "—")
                + " · " + (RUN_STATUS_LABELS[preview.status] || preview.status || "")
                + " · " + (preview.member_count || 0) + " человек"
        ));
        heading.appendChild(el(
            "small",
            "",
            "Создан " + formatMoment(preview.created_at) + ", автор: " + (preview.created_by || "—")
        ));
        summary.appendChild(heading);
        if (preview.stale) {
            summary.appendChild(el(
                "p",
                "settlement-one-button-stale",
                "Источники изменились: результат устарел. Выполните расчёт и подтверждение заново."
            ));
        }
        setHidden(summary, false);
    }

    /* rule_ids сервер отдаёт в пропусках применения (apply). В списке
       неразмещённых строк (preview.unresolved) этого поля пока нет, поэтому
       чипы правил появляются там только когда поле придёт с сервера. */
    function exceptionRow(row) {
        var item = el("article", "settlement-one-button-exception");
        item.appendChild(el("strong", "", residentLabel(row.resident)));
        item.appendChild(window.settlementPersonnelNumber(row.resident?.personnel_number));
        item.appendChild(el("span", "settlement-one-button-exception-reason", row.reason || "Требуется уточнение данных."));
        var rules = ruleChips(row.rule_ids);
        if (rules) item.appendChild(rules);
        return item;
    }

    function excludedRow(row) {
        var item = el("article", "settlement-one-button-exception is-excluded");
        var title = residentLabel(row.resident);
        if (row.work_shift_label) title += " · " + row.work_shift_label;
        item.appendChild(el("strong", "", title));
        item.appendChild(window.settlementPersonnelNumber(row.resident?.personnel_number));
        item.appendChild(el("span", "settlement-one-button-exception-reason", row.reason || "Исключён из плана вручную."));
        return item;
    }

    function listSection(title, rows, kind) {
        var section = el("section", "settlement-one-button-list" + (kind ? " " + kind : ""));
        section.appendChild(el("h3", "", title));
        rows.forEach(function (row) { section.appendChild(row); });
        return section;
    }

    function renderExceptions(preview) {
        if (!exceptions) return;
        exceptions.replaceChildren();
        var unresolved = (preview && preview.unresolved) || [];
        var excluded = (preview && preview.effective_plan && preview.effective_plan.excluded) || [];
        var unlabelledSkips = unlabelledSkippedRows(preview);
        if (!unresolved.length && !excluded.length && !unlabelledSkips.length) {
            setHidden(exceptions, true);
            return;
        }
        setHidden(exceptions, false);
        if (unresolved.length) {
            exceptions.appendChild(listSection(
                "Не размещено автоматически: " + unresolved.length,
                unresolved.map(exceptionRow),
                "is-unresolved"
            ));
        }
        if (excluded.length) {
            exceptions.appendChild(listSection(
                "Исключено вручную: " + excluded.length,
                excluded.map(excludedRow),
                "is-excluded"
            ));
        }
        if (unlabelledSkips.length) {
            var title = "Пропущено при применении: " + unlabelledSkips.length;
            if (preview && preview.application) {
                title += " (первое применение расчёта: сервер не разделяет пропуски по сменам)";
            }
            exceptions.appendChild(listSection(title, unlabelledSkips.map(skipRow), "is-skipped"));
        }
    }

    function appliedCount(counts) {
        if (!counts) return 0;
        return Number(counts.created || 0)
            + Number(counts.reused || 0)
            + Number(counts.replaced_auto || 0)
            + Number(counts.replaced_manual || 0);
    }

    function appliedShiftNames(preview) {
        if (!preview || !preview.shift_apply) return [];
        return SHIFTS.filter(function (shift) {
            var row = preview.shift_apply[shift];
            return Boolean(row && row.status === "applied");
        });
    }

    /* Откуда берутся пропуски смены, по приоритету:
       1) смена применена только что этим экраном и ответ её применения уже
          разобран (result.skippedKnown);
       2) сервер отдал пропуски по смене: shift_apply[shift].skipped для
          применённой смены (backend-12, settlement/views.py:1378-1393);
       3) старый ответ без поля: у расчёта ровно одно применение, значит
          preview.application — это оно.
       Для неприменённой смены skipped приходит пустым массивом — это не
       «пропусков нет», а «применения ещё не было», поэтому known=false. */
    function skippedForShift(shift, preview, result) {
        if (result && result.skippedKnown) {
            return {known: true, rows: result.skipped || []};
        }
        var server = preview && preview.shift_apply ? preview.shift_apply[shift] : null;
        if (server && server.status === "applied" && Array.isArray(server.skipped)) {
            return {known: true, rows: server.skipped};
        }
        var applied = appliedShiftNames(preview);
        if (
            applied.length === 1
            && applied[0] === shift
            && preview
            && preview.application
        ) {
            return {known: true, rows: preview.application.skipped || []};
        }
        return {known: false, rows: []};
    }

    function anyShiftSkippedKnown() {
        return SHIFTS.some(function (shift) {
            return Boolean(state.results[shift] && state.results[shift].skippedKnown);
        });
    }

    /* Все применённые смены сами назвали свои пропуски — отдельный блок без
       привязки к смене не нужен. */
    function allAppliedShiftsHaveSkipped(preview) {
        var applied = appliedShiftNames(preview);
        if (!applied.length) return false;
        return applied.every(function (shift) {
            var row = preview.shift_apply[shift];
            return Boolean(row && Array.isArray(row.skipped));
        });
    }

    /* Старый ответ без shift_apply[shift].skipped: приписать пропуски
       конкретной смене нельзя (preview.application относится к первому по
       времени применению), поэтому строки показываются отдельным блоком без
       разбивки по смене — чтобы ничего не скрывать. */
    function unlabelledSkippedRows(preview) {
        if (!preview || !preview.application) return [];
        if (appliedShiftNames(preview).length === 1) return [];
        if (allAppliedShiftsHaveSkipped(preview)) return [];
        if (anyShiftSkippedKnown()) return [];
        return preview.application.skipped || [];
    }

    function shiftStateText(server, result) {
        if (result) {
            if (result.status === "error") return result.error || "Ошибка применения.";
            if (result.status === "cancelled") return result.note;
            if (result.status === "applied") {
                return result.note || "Применено только что.";
            }
            if (result.status === "too_early" || result.status === "not_ready") return result.note;
            if (result.status === "applying") return "Применяем…";
        }
        if (!server) return "Сервер не отдал состояние смены.";
        if (server.status === "applied") {
            return "Применено " + formatMoment(server.applied_at) + ".";
        }
        if (server.status === "too_early") {
            return "Рано: применить можно с " + formatIsoDate(server.allowed_date) + ".";
        }
        if (server.status === "ready") {
            return "Готово к применению с " + formatIsoDate(server.allowed_date) + ".";
        }
        return "Сначала подтвердите актуальный расчёт.";
    }

    function skipRow(row) {
        var item = el("article", "settlement-one-button-skip");
        var inPlan = row.in_plan;
        var stateText;
        if (inPlan === true) {
            item.classList.add("is-plan-skip");
            stateText = "Плановая строка не применена";
        } else if (inPlan === false) {
            item.classList.add("is-out-of-plan-skip");
            stateText = "Внеплановая занятость";
        } else {
            item.classList.add("is-unclassified-skip");
            stateText = "Причина пропуска не классифицирована";
        }
        item.appendChild(el("strong", "", row.resident_name || ("Жилец № " + (row.resident_id || "?"))));
        item.appendChild(window.settlementPersonnelNumber(row.personnel_number));
        item.appendChild(el("span", "settlement-one-button-skip-state", stateText));
        if (row.message) item.appendChild(el("p", "settlement-one-button-skip-reason", row.message));
        if (row.bed_stable_id) item.appendChild(el("small", "", "Койка: " + row.bed_stable_id));
        var rules = ruleChips(row.rule_ids);
        if (rules) item.appendChild(rules);
        return item;
    }

    function renderShifts(preview) {
        if (!shifts) return;
        shifts.replaceChildren();
        if (!preview) {
            setHidden(shifts, true);
            return;
        }
        setHidden(shifts, false);
        SHIFTS.forEach(function (shift) {
            var server = preview.shift_apply ? preview.shift_apply[shift] : null;
            var result = state.results[shift] || null;
            var status = result ? result.status : (server ? server.status : "not_ready");
            var card = el("article", "settlement-one-button-shift is-" + (status || "not_ready"));
            var head = el("div", "settlement-one-button-shift-head");
            head.appendChild(el("strong", "", shiftLabel(shift)));
            head.appendChild(el("span", "settlement-one-button-shift-state", shiftStateText(server, result)));
            card.appendChild(head);

            var counts = (result && result.counts)
                || (server && server.status === "applied" ? server.counts : null);
            var skipped = skippedForShift(shift, preview, result);
            if (counts || skipped.known) {
                var counters = el("div", "settlement-one-button-shift-counts");
                if (counts) {
                    counters.appendChild(el("span", "", "Применено: " + appliedCount(counts)));
                    counters.appendChild(el(
                        "span",
                        "",
                        "Создано: " + Number(counts.created || 0)
                            + " · повторно: " + Number(counts.reused || 0)
                            + " · заменено: " + (Number(counts.replaced_auto || 0) + Number(counts.replaced_manual || 0))
                    ));
                }
                if (skipped.known) {
                    counters.appendChild(el("span", "", "Пропущено: " + skipped.rows.length));
                } else if (status === "applied") {
                    counters.appendChild(el("span", "", "Пропущено: сервер не отдал список отдельно"));
                }
                card.appendChild(counters);
            }
            if (skipped.rows.length) {
                var list = el("div", "settlement-one-button-skip-list");
                skipped.rows.forEach(function (row) { list.appendChild(skipRow(row)); });
                card.appendChild(list);
            }
            shifts.appendChild(card);
        });
    }

    function renderAll() {
        var payload = state.payload;
        var cohorts = (payload && payload.cohorts) || [];
        var noCohort = !payload
            || payload.state === "no_cohort"
            || !cohorts.length;
        if (empty) {
            setHidden(empty, !(payload && noCohort));
            if (payload && noCohort) {
                empty.textContent = "Нет утверждённого состава заезда. Сначала утвердите список заезда — карта и ручное расселение продолжают работать.";
            }
        }
        if (noCohort) {
            setHidden(cohortRow, true);
            renderSummary(null);
            renderExceptions(null);
            renderShifts(null);
        } else {
            renderCohort(payload);
            var preview = payload.preview || null;
            renderSummary(preview);
            renderExceptions(preview);
            renderShifts(preview);
        }
        renderLeaseHint();
        syncButtons();
    }

    /* --- Действия --------------------------------------------------------- */

    function loadState(cohortId) {
        if (!root.dataset.autoStateUrl) return Promise.resolve();
        var sequence = ++state.readSequence;
        state.loading = true;
        syncButtons();
        var url = root.dataset.autoStateUrl;
        if (cohortId) url += "?cohort_id=" + encodeURIComponent(cohortId);
        return requestJson(url, {method: "GET"})
            .then(function (payload) {
                if (sequence !== state.readSequence) return;
                state.payload = payload || null;
                state.cohortId = payload && payload.selected_cohort_id
                    ? payload.selected_cohort_id
                    : cohortId;
                renderAll();
            })
            .catch(function (error) {
                if (sequence !== state.readSequence) return;
                showFeedback(errorMessage(error), "error");
            })
            .finally(function () {
                if (sequence !== state.readSequence) return;
                state.loading = false;
                syncButtons();
            });
    }

    function selectedCohortId() {
        if (cohortSelect && cohortSelect.value) return Number(cohortSelect.value);
        if (state.payload && state.payload.selected_cohort_id) {
            return Number(state.payload.selected_cohort_id);
        }
        return null;
    }

    function calculate() {
        if (!canMutate()) return;
        var cohortId = selectedCohortId();
        if (!cohortId) {
            showFeedback("Не выбран утверждённый состав заезда.", "warning");
            return;
        }
        state.busy = true;
        state.results = {};
        state.cohortId = cohortId;
        showFeedback("", "");
        syncButtons();
        startProgress("Рассчитываем расселение…");
        requestJson(root.dataset.autoCreateUrl, {payload: {cohort_id: cohortId}})
            .then(function (body) {
                if (body && body.preview && state.payload) {
                    state.payload.preview = body.preview;
                    state.payload.selected_cohort_id = cohortId;
                    state.payload.state = "ready";
                }
                showFeedback("Расчёт готов. Проверьте сводку и список исключений.", "success");
            })
            .catch(function (error) {
                showFeedback(errorMessage(error), "error");
                return loadState(cohortId);
            })
            .finally(function () {
                state.busy = false;
                stopProgress();
                renderAll();
            });
    }

    function confirmPreview() {
        var preview = currentPreview();
        if (!preview || preview.status !== "draft" || !canMutate()) return;
        if (!window.confirm("Подтвердить расчёт? План будет зафиксирован, карта фактического проживания ещё не изменится.")) {
            return;
        }
        state.busy = true;
        state.results = {};
        showFeedback("", "");
        syncButtons();
        startProgress("Подтверждаем расчёт…");
        requestJson(root.dataset.autoConfirmUrl, {payload: {run_id: preview.id}})
            .then(function (body) {
                if (body && body.preview && state.payload) {
                    state.payload.preview = body.preview;
                }
                showFeedback("Расчёт подтверждён. Теперь можно применить день и ночь.", "success");
            })
            .catch(function (error) {
                showFeedback(errorMessage(error), "error");
            })
            .finally(function () {
                state.busy = false;
                stopProgress();
                renderAll();
            });
    }

    function confirmApplyDialog(preview) {
        var lines = ["Применить расчёт к карте фактического расселения?"];
        SHIFTS.forEach(function (shift) {
            var row = preview.shift_apply ? preview.shift_apply[shift] : null;
            var label = shiftLabel(shift) + ": ";
            if (!row) {
                lines.push(label + "состояние неизвестно");
            } else if (row.status === "ready") {
                lines.push(label + "с " + formatIsoDate(row.allowed_date));
            } else if (row.status === "applied") {
                lines.push(label + "уже применена");
            } else if (row.status === "too_early") {
                lines.push(label + "рано, с " + formatIsoDate(row.allowed_date));
            } else {
                lines.push(label + "не готова");
            }
        });
        lines.push("Смены применяются последовательно и независимо: конфликт одной не отменяет вторую.");
        return window.confirm(lines.join("\n"));
    }

    function applyRequest(shift, runId, replaceManual) {
        var url = shift === "night"
            ? root.dataset.autoApplyNightUrl
            : root.dataset.autoApplyDayUrl;
        return requestJson(url, {
            payload: {
                run_id: runId,
                confirm_replace_manual: Boolean(replaceManual)
            }
        });
    }

    function applySucceeded(shift, result, fresh, beforeAt) {
        if (fresh && state.payload) state.payload.preview = fresh;
        var current = currentPreview();
        var server = current && current.shift_apply ? current.shift_apply[shift] : null;
        var application = current ? current.application : null;
        result.status = "applied";
        result.counts = server && server.counts ? server.counts : null;
        result.note = "";
        if (server && Array.isArray(server.skipped)) {
            /* backend-12: сервер сам отдаёт пропуски применённой смены. */
            result.skipped = server.skipped;
            result.skippedKnown = true;
        } else if (application && application.applied_at !== beforeAt) {
            /* Старый ответ: применение только что созданное, значит
               preview.application описывает именно эту смену. */
            result.skipped = application.skipped || [];
            result.skippedKnown = true;
        } else {
            result.skipped = [];
            result.skippedKnown = false;
            result.note = "Пропуски этой смены сервер не вернул отдельно.";
        }
        return result;
    }

    function applyShift(shift) {
        var preview = currentPreview();
        var server = preview && preview.shift_apply ? preview.shift_apply[shift] : null;
        var result = {
            shift: shift,
            status: "",
            note: "",
            error: "",
            counts: null,
            skipped: [],
            skippedKnown: false
        };
        state.results[shift] = result;
        if (!server) {
            result.status = "not_ready";
            result.note = "Сервер не отдал состояние смены.";
            renderShifts(preview);
            return Promise.resolve();
        }
        if (server.status === "applied") {
            result.status = "applied";
            result.counts = server.counts || null;
            result.note = "Уже применена ранее: " + formatMoment(server.applied_at) + ".";
            return Promise.resolve();
        }
        if (server.status === "too_early") {
            result.status = "too_early";
            result.note = "Рано: применить можно с " + formatIsoDate(server.allowed_date) + ".";
            return Promise.resolve();
        }
        if (server.status !== "ready") {
            result.status = "not_ready";
            result.note = "Сначала подтвердите актуальный расчёт.";
            return Promise.resolve();
        }

        result.status = "applying";
        var beforeAt = preview.application ? preview.application.applied_at : "";
        startProgress("Применяем " + shiftLabel(shift).toLowerCase() + "…");
        renderShifts(preview);

        return applyRequest(shift, preview.id, false)
            .then(function (body) {
                return applySucceeded(
                    shift,
                    result,
                    body && body.preview ? body.preview : null,
                    beforeAt
                );
            })
            .catch(function (error) {
                if (shortCode(error.code) === "manual_replacement_confirmation_required") {
                    var count = Number((error.details || {}).manual_occupancy_count || 0);
                    var question = "Будет заменено ручных размещений: " + count
                        + ".\n\nРучные изменения сохранятся в истории.\n\nЗаменить ручные изменения и применить "
                        + shiftLabel(shift).toLowerCase() + "?";
                    if (!window.confirm(question)) {
                        result.status = "cancelled";
                        result.note = "Замену ручных размещений не подтвердили — смена не применена.";
                        return result;
                    }
                    return applyRequest(shift, preview.id, true)
                        .then(function (body) {
                            return applySucceeded(
                                shift,
                                result,
                                body && body.preview ? body.preview : null,
                                beforeAt
                            );
                        });
                }
                result.status = "error";
                result.error = errorMessage(error);
                var rows = error.details && error.details.skipped_rows;
                if (Array.isArray(rows) && rows.length) {
                    result.skipped = rows;
                    result.skippedKnown = true;
                }
                return result;
            })
            .then(function (value) {
                stopProgress();
                renderSummary(currentPreview());
                renderShifts(currentPreview());
                return value;
            }, function (error) {
                result.status = "error";
                result.error = errorMessage(error);
                stopProgress();
                renderSummary(currentPreview());
                renderShifts(currentPreview());
                return result;
            });
    }

    function applyAll() {
        var preview = currentPreview();
        if (!preview || !canMutate()) return;
        if (preview.status !== "confirmed") {
            showFeedback("Сначала подтвердите расчёт — до подтверждения применять нечего.", "warning");
            return;
        }
        if (preview.stale) {
            showFeedback("Источники изменились: расчёт устарел. Выполните расчёт и подтверждение заново.", "warning");
            return;
        }
        if (!confirmApplyDialog(preview)) return;
        state.busy = true;
        state.results = {};
        showFeedback("", "");
        syncButtons();
        var chain = Promise.resolve();
        SHIFTS.forEach(function (shift) {
            chain = chain.then(function () { return applyShift(shift); });
        });
        chain.then(function () {
            var failed = SHIFTS.filter(function (shift) {
                return state.results[shift] && state.results[shift].status === "error";
            });
            var applied = SHIFTS.filter(function (shift) {
                return state.results[shift] && state.results[shift].status === "applied";
            });
            if (failed.length) {
                showFeedback(
                    "Применение завершено с конфликтами: "
                        + failed.map(shiftLabel).join(", ")
                        + ". Остальные смены применены.",
                    "warning"
                );
            } else if (applied.length) {
                showFeedback("Применение завершено.", "success");
            } else {
                showFeedback("Ни одна смена не применена — смотрите причину по каждой смене.", "warning");
            }
        }).finally(function () {
            state.busy = false;
            stopProgress();
            renderAll();
        });
    }

    /* --- Подписки --------------------------------------------------------- */

    if (calculateButton) calculateButton.addEventListener("click", calculate);
    if (confirmButton) confirmButton.addEventListener("click", confirmPreview);
    if (applyButton) applyButton.addEventListener("click", applyAll);
    if (refreshButton) {
        refreshButton.addEventListener("click", function () {
            loadState(state.cohortId);
        });
    }
    if (cohortSelect) {
        cohortSelect.addEventListener("change", function () {
            var value = Number(cohortSelect.value);
            if (!value) return;
            state.results = {};
            loadState(value);
        });
    }

    /* Карта ставит data-control-state на корень при acquire/heartbeat/release.
       Без управления сервер отклонит расчёт, поэтому подсказка и доступность
       кнопок пересчитываются сразу. */
    if (typeof window.MutationObserver === "function") {
        var observer = new window.MutationObserver(function () {
            renderLeaseHint();
            syncButtons();
        });
        observer.observe(root, {attributes: true, attributeFilter: ["data-control-state"]});
    }

    renderAll();
    showFeedback("Загружаем состояние авторасселения…", "progress");
    loadState(null).then(function () {
        if (feedback && feedback.classList.contains("is-progress")) showFeedback("", "");
    });
}());
