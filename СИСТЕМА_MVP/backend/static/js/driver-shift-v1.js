/* Экран водителя: сборка и привязка обработчиков при открытии страницы.
   Остальные части лежат в соседних driver-shift-*.js и подключаются раньше. */
window.bindDriverMobileShell = function () {
    var shell = document.querySelector("[data-driver-shell]");
    if (!shell) {
        return;
    }
    if (shell.dataset.driverShellBound === "true") {
        return;
    }
    shell.dataset.driverShellBound = "true";
    /* Смена, открытая или закрытая на телефоне без связи, переключает экран
       раньше всех остальных модулей: они читают ID смены из оболочки. */
    if (window.DriverLocalShift && typeof window.DriverLocalShift.project === "function") {
        window.DriverLocalShift.project(shell);
    }
    if (typeof window.bindMobileShiftScreens === "function") {
        window.bindMobileShiftScreens();
    }
    /* Banners rendered by base.html sit in normal flow above the shell and steal
       height from it; without subtracting them the bottom actions fall off screen. */
    function driverLeadingBannerHeight() {
        var total = 0;
        [".app-observer-banner", ".app-inactive-role-banner"].forEach(function (selector) {
            document.querySelectorAll(selector).forEach(function (banner) {
                if (window.getComputedStyle(banner).position === "fixed") return;
                total += banner.getBoundingClientRect().height || 0;
            });
        });
        return Math.round(total);
    }
    var driverStableViewportHeight = 0;
    var driverStableViewportWidth = 0;
    function driverIsEditingField() {
        var active = document.activeElement;
        return !!(active && active.matches && active.matches("input, textarea, select"));
    }
    function driverCurrentViewportMetrics() {
        var viewport = window.visualViewport;
        var height = viewport && Number(viewport.height) > 0 ? Number(viewport.height) : Number(window.innerHeight);
        height = Math.round(height || document.documentElement.clientHeight || 0);
        var width = viewport && Number(viewport.width) > 0 ? Number(viewport.width) : Number(window.innerWidth);
        width = Math.round(width || document.documentElement.clientWidth || 0);
        return {height: height, width: width};
    }
    function driverViewportIsTemporarilyReduced() {
        if (!(driverStableViewportHeight > 0) || !(driverStableViewportWidth > 0)) return false;
        var metrics = driverCurrentViewportMetrics();
        var sameWidth = Math.abs(metrics.width - driverStableViewportWidth) <= 24;
        var keyboardGap = Math.max(96, Math.round(driverStableViewportHeight * 0.18));
        return sameWidth && metrics.height < driverStableViewportHeight - keyboardGap;
    }
    function driverVisualViewportHeight() {
        var metrics = driverCurrentViewportMetrics();
        var height = metrics.height;
        /* Пока открыта клавиатура, видимая часть экрана сжимается почти вдвое.
           Подгонять под неё всю разметку нельзя — строки налезают друг на
           друга. Держим высоту, измеренную без клавиатуры: до нужного поля
           экран и так доводит прокрутка. */
        if (driverIsEditingField() || driverViewportIsTemporarilyReduced()) {
            if (driverStableViewportHeight > 0) height = driverStableViewportHeight;
        } else {
            driverStableViewportHeight = height;
            driverStableViewportWidth = metrics.width;
        }
        return Math.max(320, height - driverLeadingBannerHeight());
    }
    function driverPanelOverflows(panel) {
        if (!panel) return false;
        if (panel.scrollHeight > panel.clientHeight + 1 || panel.scrollWidth > panel.clientWidth + 1) return true;
        var nav = document.querySelector("[data-driver-bottom-nav]");
        var navTop = nav ? nav.getBoundingClientRect().top : driverVisualViewportHeight();
        var probes = panel.querySelectorAll(
            ".driver-shift-scroll, .driver-shift-section, .driver-downtime-list, " +
            ".driver-report-scroll, .driver-report-grid, .driver-report-section, .driver-report-actions, " +
            ".driver-timeline, button, input, select"
        );
        return Array.prototype.some.call(probes, function (node) {
            /* Грани барабана простоев повёрнуты в 3D: по замерам они выходят за экран, хотя
               сцена их обрезает. Считать это переполнением нельзя — из-за него весь экран
               водителя жил в плотности «tight», а при каждом простое дёргался на 2 px. */
            // То же для барабана точек разгрузки над кругом.
            if (node.closest && node.closest("[data-driver-downtime-drum], [data-driver-point-drum]")) return false;
            var rect = node.getBoundingClientRect();
            return (
                node.scrollHeight > node.clientHeight + 1
                || node.scrollWidth > node.clientWidth + 1
                || rect.bottom > navTop + 1
                || rect.right > window.innerWidth + 1
                || rect.left < -1
            );
        });
    }
    /* Boxes here have capped sizes, so long Russian labels — or a phone with
       enlarged system text — get cut off mid-word. Shrink the text to fit
       instead of silently clipping it. */
    /* У однострочных подписей высота строки шрифта всегда чуть больше заданной
       line-height, и проверка по высоте срабатывала вхолостую, ужимая текст до
       минимума. Для них меряем только ширину. */
    function driverTextOverflows(node, widthOnly) {
        if (node.scrollWidth > node.clientWidth + 1) return true;
        return !widthOnly && node.scrollHeight > node.clientHeight + 1;
    }
    function shrinkDriverTextToFit(node, minSize, widthOnly) {
        if (!node) return;
        node.style.removeProperty("font-size");
        if (!driverTextOverflows(node, widthOnly)) return;
        var size = parseFloat(window.getComputedStyle(node).fontSize);
        if (!(size > 0)) return;
        var guard = 40;
        while (size > minSize && driverTextOverflows(node, widthOnly) && guard > 0) {
            /* Без ограничения снизу цикл проскакивал минимум на один шаг. */
            size = Math.max(minSize, size - 1);
            guard -= 1;
            node.style.fontSize = size + "px";
        }
    }
    var DRIVER_FIT_TARGETS = [
        {selector: ".driver-header-truck", min: 13, widthOnly: true},
        {selector: ".driver-header-person", min: 11, widthOnly: true},
        {selector: ".driver-shift-open, .driver-shift-logout, .driver-shift-submit, .driver-primary-action", min: 12},
        {selector: ".driver-shift-result-cell strong", min: 13, widthOnly: true},
        {selector: ".driver-work-context-machine strong", min: 11, widthOnly: true},
        {selector: ".driver-work-context-machine small", min: 9, widthOnly: true},
        {selector: ".driver-work-context-location", min: 11, widthOnly: true},
        {selector: ".driver-work-context-rock", min: 11, widthOnly: true}
    ];
    function fitDriverText(root) {
        var fitRoot = root && typeof root.querySelectorAll === "function" ? root : shell;
        DRIVER_FIT_TARGETS.forEach(function (target) {
            fitRoot.querySelectorAll(target.selector).forEach(function (node) {
                shrinkDriverTextToFit(node, target.min, target.widthOnly);
            });
        });
    }
    /* Клавиатура закрывает нижнюю половину экрана, поэтому поле, в которое
       водитель ткнул, надо подвести в видимую часть — иначе он печатает
       вслепую или вообще не видит, куда попал. */
    function bindDriverFieldScrollIntoView() {
        if (shell.dataset.driverFieldFocusBound === "true") return;
        shell.dataset.driverFieldFocusBound = "true";
        shell.addEventListener("focusin", function (event) {
            var field = event.target;
            if (!field || !field.matches || !field.matches("input, select, textarea")) return;
            if (field.closest(".mobile-shift")) return;
            var reveal = function () {
                if (!field.scrollIntoView) return;
                try {
                    field.scrollIntoView({block: "center", behavior: "smooth"});
                } catch (error) {
                    field.scrollIntoView();
                }
            };
            /* Ждём, пока клавиатура выедет и пересчитается высота. */
            window.setTimeout(reveal, 60);
            window.setTimeout(reveal, 320);
        });
    }
    function cancelDriverViewportFitFrames() {
        [
            "driverViewportFitFrame",
            "driverViewportFitTextFrame",
            "driverViewportFitDensityFrame"
        ].forEach(function (key) {
            if (window[key] !== null && typeof window[key] !== "undefined") {
                window.cancelAnimationFrame(window[key]);
                window[key] = null;
            }
        });
    }
    function invalidateDriverViewportFit() {
        window.driverViewportFitGeneration = Number(window.driverViewportFitGeneration || 0) + 1;
        cancelDriverViewportFitFrames();
        return window.driverViewportFitGeneration;
    }
    function driverViewportFitIsCurrent(generation) {
        return generation === window.driverViewportFitGeneration && shell.isConnected;
    }
    function fitDriverViewport(generation) {
        if (!driverViewportFitIsCurrent(generation)) return;
        var height = driverVisualViewportHeight();
        var viewport = window.visualViewport;
        var width = Math.max(240, Math.round(
            viewport && Number(viewport.width) > 0
                ? Number(viewport.width)
                : Number(window.innerWidth || document.documentElement.clientWidth || 0)
        ));
        document.documentElement.style.setProperty("--driver-viewport-h", height + "px");
        document.body.style.setProperty("--driver-viewport-h", height + "px");
        shell.style.setProperty("--driver-viewport-h", height + "px");
        shell.style.setProperty("--driver-viewport-w", width + "px");
        var baseDensity = height < 620 || width < 340
            ? "tight"
            : (height < 780 || width < 390 ? "compact" : "normal");
        shell.dataset.driverViewportDensity = baseDensity;
        shell.dataset.driverDensity = baseDensity;
        window.driverViewportFitTextFrame = window.requestAnimationFrame(function () {
            window.driverViewportFitTextFrame = null;
            if (!driverViewportFitIsCurrent(generation)) return;
            fitDriverText();
            /* С открытой клавиатурой всё «не влезает» по определению: она
               закрывает пол-экрана. Уплотнять разметку из-за этого нельзя —
               строки сойдутся друг на друга. */
            if (driverIsEditingField() || driverViewportIsTemporarilyReduced()) return;
            var panel = shell.querySelector("[data-driver-tab-panel].is-active");
            if (panel && panel.isConnected && driverPanelOverflows(panel)) {
                shell.dataset.driverDensity = shell.dataset.driverDensity === "normal" ? "compact" : "tight";
                window.driverViewportFitDensityFrame = window.requestAnimationFrame(function () {
                    window.driverViewportFitDensityFrame = null;
                    if (!driverViewportFitIsCurrent(generation)) return;
                    if (!panel.isConnected || driverViewportIsTemporarilyReduced()) return;
                    if (driverPanelOverflows(panel)) shell.dataset.driverDensity = "tight";
                });
            }
        });
    }
    function scheduleDriverViewportFit() {
        invalidateDriverTabSettle();
        var generation = invalidateDriverViewportFit();
        window.driverViewportFitFrame = window.requestAnimationFrame(function () {
            window.driverViewportFitFrame = null;
            if (!driverViewportFitIsCurrent(generation)) return;
            fitDriverViewport(generation);
        });
    }
    /* DRIVER_TAB_SETTLE_START */
    function cancelDriverTabSettleFrames() {
        [
            "driverTabSettleFirstFrame",
            "driverTabSettleSecondFrame",
            "driverTabSettleDensityFrame"
        ].forEach(function (key) {
            if (window[key] !== null && typeof window[key] !== "undefined") {
                window.cancelAnimationFrame(window[key]);
                window[key] = null;
            }
        });
    }
    function invalidateDriverTabSettle() {
        window.driverTabSettleGeneration = Number(window.driverTabSettleGeneration || 0) + 1;
        cancelDriverTabSettleFrames();
        return window.driverTabSettleGeneration;
    }
    function driverTabSettleIsCurrent(generation, expectedTab, panel) {
        return (
            generation === window.driverTabSettleGeneration
            && shell.isConnected
            && shell.dataset.activeTab === expectedTab
            && panel
            && panel.isConnected
            && panel.classList.contains("is-active")
            && panel.dataset.driverTabPanel === expectedTab
        );
    }
    function settleDriverTabDensity(generation, expectedTab, panel) {
        if (!driverTabSettleIsCurrent(generation, expectedTab, panel)) return;
        if (driverIsEditingField() || driverViewportIsTemporarilyReduced()) return;
        if (!driverPanelOverflows(panel)) return;
        var currentDensity = shell.dataset.driverDensity || "normal";
        var nextDensity = currentDensity === "normal"
            ? "compact"
            : (currentDensity === "compact" ? "tight" : currentDensity);
        if (nextDensity === currentDensity) return;
        shell.dataset.driverDensity = nextDensity;
        if (nextDensity !== "tight") {
            window.driverTabSettleDensityFrame = window.requestAnimationFrame(function () {
                window.driverTabSettleDensityFrame = null;
                settleDriverTabDensity(generation, expectedTab, panel);
            });
        }
    }
    function scheduleDriverTabSettle(expectedTab) {
        invalidateDriverViewportFit();
        var generation = invalidateDriverTabSettle();
        /* Все вызовы планировщика начинают с той же стабильной базы. Основной
           click-path выставляет её ещё раньше — до смены active-классов. */
        shell.dataset.driverDensity = shell.dataset.driverViewportDensity
            || shell.dataset.driverDensity
            || "normal";
        window.driverTabSettleFirstFrame = window.requestAnimationFrame(function () {
            window.driverTabSettleFirstFrame = null;
            if (
                generation !== window.driverTabSettleGeneration
                || !shell.isConnected
                || shell.dataset.activeTab !== expectedTab
            ) {
                return;
            }
            window.driverTabSettleSecondFrame = window.requestAnimationFrame(function () {
                window.driverTabSettleSecondFrame = null;
                var panel = shell.querySelector("[data-driver-tab-panel].is-active");
                if (!driverTabSettleIsCurrent(generation, expectedTab, panel)) return;
                fitDriverText(panel);
                settleDriverTabDensity(generation, expectedTab, panel);
            });
        });
    }
    /* DRIVER_TAB_SETTLE_END */
    bindDriverFieldScrollIntoView();
    window.driverScheduleViewportFit = scheduleDriverViewportFit;
    if (!window.driverViewportFitBound) {
        window.driverViewportFitBound = true;
        var requestCurrentDriverViewportFit = function () {
            if (typeof window.driverScheduleViewportFit === "function") window.driverScheduleViewportFit();
        };
        window.addEventListener("resize", requestCurrentDriverViewportFit, {passive: true});
        window.addEventListener("orientationchange", requestCurrentDriverViewportFit, {passive: true});
        if (window.visualViewport) {
            window.visualViewport.addEventListener("resize", requestCurrentDriverViewportFit, {passive: true});
        }
    }
    scheduleDriverViewportFit();
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(scheduleDriverViewportFit);
    if (window.driverUnloadHoldGuard && typeof window.driverUnloadHoldGuard.destroy === "function") {
        window.driverUnloadHoldGuard.destroy();
        window.driverUnloadHoldGuard = null;
    }
    if (window.driverUnloadRecovery && typeof window.driverUnloadRecovery.destroy === "function") {
        window.driverUnloadRecovery.destroy();
        window.driverUnloadRecovery = null;
    }
    if (window.driverUnloadGesture && typeof window.driverUnloadGesture.destroy === "function") {
        window.driverUnloadGesture.destroy();
        window.driverUnloadGesture = null;
    }
    function driverRoleIsReadonly() {
        return (
            typeof window.isAppRoleReadonly === "function"
            && window.isAppRoleReadonly()
        ) || (document.body && document.body.dataset.driverShiftClosePending === "true");
    }
    shell.querySelectorAll(".mobile-shift__metric-value input").forEach(function (input) {
        if (!input.getAttribute("placeholder")) input.setAttribute("placeholder", "0");
    });

    /* Резервная навигация для старой разметки: Enter уводит на следующее поле,
       а с последнего убирает клавиатуру и фокусирует кнопку. Общий экран Смены
       использует mobile-shift-unified-v1.js и в этот блок не попадает. */
    shell.querySelectorAll("[data-driver-shift-inputs], .driver-shift-opening-form").forEach(function (group) {
        if (group.closest(".mobile-shift") || group.querySelector(".mobile-shift")) return;
        var fields = Array.prototype.filter.call(
            group.querySelectorAll("input"),
            function (input) {
                return input.type !== "hidden" && !input.disabled && !input.readOnly;
            }
        );
        fields.forEach(function (input, index) {
            var isLast = index === fields.length - 1;
            input.setAttribute("enterkeyhint", isLast ? "done" : "next");
            if (input.dataset.driverEnterBound === "true") return;
            input.dataset.driverEnterBound = "true";
            input.addEventListener("keydown", function (event) {
                if (event.key !== "Enter" && event.keyCode !== 13) return;
                event.preventDefault();
                var next = fields[index + 1];
                if (next) {
                    next.focus();
                    if (next.select) next.select();
                    return;
                }
                /* Одного blur мало: перевод фокуса на кнопку помогает старым
                   WebView закрыть клавиатуру и сразу оставить действие доступным. */
                input.blur();
                var action = group.querySelector("[data-driver-shift-open-button], [data-driver-shift-close-button]")
                    || (group.closest("form") || document).querySelector("[data-driver-shift-open-button], [data-driver-shift-close-button]");
                if (action && !action.disabled) {
                    action.focus();
                    return;
                }
                /* Кнопка ещё не активна — показания не сошлись. Всё равно уводим
                   фокус с поля на саму вкладку, иначе клавиатура может остаться
                   висеть и закрывать пол-экрана. */
                var host = group.closest("[data-driver-tab-panel]") || group;
                if (!host.hasAttribute("tabindex")) host.setAttribute("tabindex", "-1");
                try { host.focus({preventScroll: true}); } catch (error) { host.focus(); }
            });
        });
    });

    bindDriverShiftOpeningForm(shell);
    function syncDriverDialProgress() {
        shell.querySelectorAll(".driver-work-dial").forEach(function (dial) {
            var loopProgress = Number(dial.dataset.driverLoopProgress || 0);
            if (!Number.isFinite(loopProgress)) {
                loopProgress = 0;
            }
            loopProgress = Math.max(0, Math.min(loopProgress, 100));
            var completedLoops = Number(dial.dataset.driverCompletedLoops || 0);
            if (!Number.isFinite(completedLoops)) {
                completedLoops = 0;
            }
            var hasPlan = dial.dataset.driverHasPlan === "1";
            var cappedProgress = hasPlan ? (completedLoops > 0 ? 100 : loopProgress) : 0;
            var overProgress = hasPlan && completedLoops > 0 ? loopProgress : 0;
            dial.style.setProperty("--driver-progress-capped", String(cappedProgress));
            dial.style.setProperty("--driver-over-progress", String(overProgress));
            dial.classList.toggle("is-over-plan", overProgress > 0);
        });
    }
    syncDriverDialProgress();
    /* Назначение (v377): отсчёт тикает в подписи круга и в углу «ПРИНЯТЬ», срок —
       на скрытой форме назначения. В срок назначение вступает само (сервер,
       reconcile_due_haul_assignments): угол гаснет, подпись круга обычная, круг —
       на новом экскаваторе сразу, без сети тоже; экран не перезагружается
       целиком — приходит обычное обновление фрагмента. */
    function bindAssignmentCountdown() {
        var form = shell.querySelector("#driver-assignment-action[data-driver-assignment-deadline]");
        if (!form) {
            return;
        }
        var deadline = Date.parse(form.dataset.driverAssignmentDeadline || "");
        if (!Number.isFinite(deadline)) {
            return;
        }
        function renderCountdown() {
            if (!form.isConnected) {
                window.clearInterval(timerId);
                return;
            }
            var remaining = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
            var minutes = Math.floor(remaining / 60);
            var seconds = remaining % 60;
            var text = String(minutes).padStart(2, "0") + ":" + String(seconds).padStart(2, "0");
            shell.querySelectorAll("[data-driver-assignment-countdown]").forEach(function (output) {
                if (output.textContent !== text) output.textContent = text;
            });
            if (remaining > 0 || form.dataset.driverDeadlineReached === "true") {
                return;
            }
            form.dataset.driverDeadlineReached = "true";
            window.clearInterval(timerId);
            applyDriverAssignmentDue(form);
            if (window.AppRealtime && typeof window.AppRealtime.wake === "function") {
                window.AppRealtime.wake("assignment-deadline");
            } else if (typeof window.applyOperationalStateRefresh === "function") {
                window.applyOperationalStateRefresh({
                    version: Number(document.body.dataset.operationalStateVersion || 0),
                    reason: "assignment-deadline"
                });
            }
        }
        var timerId = window.setInterval(renderCountdown, 1000);
        renderCountdown();
        window.syncDriverAssignmentCorner();
    }

    /* «ПРИНЯТЬ» в углу у круга (v377): событие очереди, без ожидания сервера —
       и без сети тоже. Угол гаснет сразу; пока событие не дошло, экран сервера
       может снова нарисовать угол — его гасит сверка с очередью ниже. */
    function hideDriverAssignmentCorner() {
        var corner = shell.querySelector("[data-driver-assignment-accept]");
        if (!corner || !corner.classList.contains("is-assignment")) return;
        corner.classList.remove("is-assignment");
        corner.disabled = true;
        corner.setAttribute("aria-hidden", "true");
        corner.setAttribute("tabindex", "-1");
    }

    window.syncDriverAssignmentCorner = function (events) {
        var form = shell.querySelector("#driver-assignment-action");
        if (!form) return;
        var id = String(form.dataset.driverAssignmentId || "");
        var accepted = (events || window.driverOfflineEvents || []).some(function (event) {
            return event && event.event_type === "driver.assignment.accepted"
                && String(event.payload && event.payload.assignment_id || "") === id
                && ["conflict", "auth_required", "invalid"].indexOf(String(event.state || "pending")) < 0;
        });
        if (accepted) hideDriverAssignmentCorner();
    };

    window.driverAcceptAssignmentLocally = function (form) {
        var assignmentId = Number(form && form.dataset.driverAssignmentId || 0);
        if (!assignmentId || !driverOfflineOutbox) return Promise.resolve(false);
        hideDriverAssignmentCorner();
        return driverOfflineOutbox.enqueue({
            event_id: generateClientActionId("driver-assignment-accept"),
            event_type: "driver.assignment.accepted",
            occurred_at: new Date().toISOString(),
            payload: {assignment_id: assignmentId}
        }).then(function () {
            if (typeof playDriverSound === "function") playDriverSound("action_ok");
            driverOfflineOutbox.flush().catch(function () {});
            return true;
        });
    };

    /* Срок назначения вышел — на телефоне сразу, не дожидаясь сервера. */
    function applyDriverAssignmentDue(form) {
        var corner = shell.querySelector("[data-driver-assignment-accept]");
        if (corner) {
            corner.classList.remove("is-assignment");
            corner.disabled = true;
            corner.setAttribute("aria-hidden", "true");
            corner.setAttribute("tabindex", "-1");
        }
        var note = shell.querySelector("[data-driver-assignment-note]");
        if (note) note.hidden = true;
        var target = String(form.dataset.driverAssignmentTarget || "");
        var label = shell.querySelector("[data-driver-dial-label]");
        var hold = shell.querySelector("[data-driver-hold-button]");
        // Круг называет экскаватор только пустым и без простоя: иначе на нём рейс/причина.
        var dialShowsExcavator = !!(
            label && hold && hold.classList.contains("is-empty")
            && hold.dataset.driverManualDial !== "true"
            && !(downtimeCard && downtimeCard.dataset.driverActiveDowntimeId)
        );
        if (form.dataset.driverAssignmentKind === "assign" && target && dialShowsExcavator) {
            label.textContent = target;
            label.dataset.driverDialRaw = target;
            delete label.dataset.driverDialFitKey;
            if (typeof window.fitDriverDialLabelNow === "function") window.fitDriverDialLabelNow(label);
        }
    }
    bindAssignmentCountdown();
    function bindDriverShiftControls() {
        var form = shell.querySelector("[data-driver-shift-close-form]");
        var closeButton = shell.querySelector("[data-driver-shift-close-button]");
        var logoutButton = shell.querySelector("[data-driver-shift-logout]");
        var shiftScroll = form ? form.querySelector("[data-driver-shift-scroll]") : null;

        if (form && closeButton && closeButton.dataset.driverShiftBound !== "true") {
            closeButton.dataset.driverShiftBound = "true";
            form.addEventListener("submit", function (event) {
                if (
                    driverRoleIsReadonly()
                    || form.dataset.driverShiftHoldComplete !== "true"
                ) {
                    event.preventDefault();
                    if (
                        !driverRoleIsReadonly()
                        && form.dataset.driverShiftHoldComplete !== "true"
                        && typeof window.showDriverToast === "function"
                    ) {
                        window.showDriverToast("Удерживайте кнопку, чтобы закрыть смену");
                    }
                    return;
                }
                delete form.dataset.driverShiftHoldComplete;
                closeButton.disabled = true;
                closeButton.classList.add("is-pending");
                var closeShiftLabel = closeButton.querySelector("[data-mobile-shift-label]");
                if (closeShiftLabel) closeShiftLabel.textContent = "Закрываем смену";
            });
            bindDriverShiftHoldAction(form, closeButton, {
                holdMs: 1000,
                readyLabel: "Закрыть смену",
                progressProperty: "--driver-shift-hold"
            });

            var firstError = form.querySelector(".mobile-shift__field .errorlist");
            if (firstError) {
                window.requestAnimationFrame(function () {
                    var errorField = firstError.closest(".mobile-shift__field");
                    var errorInput = errorField ? errorField.querySelector("input") : null;
                    if (errorInput) {
                        errorInput.focus({preventScroll: true});
                    }
                });
            }
        }

        if (logoutButton && logoutButton.dataset.driverLogoutBound !== "true") {
            logoutButton.dataset.driverLogoutBound = "true";
            window.MobileShiftHold.bind(logoutButton, {
                holdMs: 2000,
                readyLabel: "Выйти",
                onShortPress: function () {
                    if (typeof window.showDriverToast === "function") window.showDriverToast("Удерживайте кнопку");
                },
                onComplete: function () {
                    var logoutLabel = logoutButton.querySelector("[data-driver-logout-label]");
                    if (logoutLabel) logoutLabel.textContent = "Выходим";
                    if (typeof window.navigateAfterNativeConnectionStop === "function") {
                        window.navigateAfterNativeConnectionStop(logoutButton.dataset.driverLogoutUrl);
                        return;
                    }
                    var stopPromise = window.NativeBackgroundConnection
                        && typeof window.NativeBackgroundConnection.stop === "function"
                        ? window.NativeBackgroundConnection.stop()
                        : null;
                    Promise.resolve(stopPromise).finally(function () {
                        window.location.href = logoutButton.dataset.driverLogoutUrl;
                    });
                }
            });
        }
    }
    bindDriverShiftControls();
    /* Форм смены на странице две: вторая скрыта и включается, когда смену
       открывают или закрывают на телефоне без сервера (driver-local-shift-v1.js).
       Её кнопке «Выйти» — то же удержание, что и у основной. */
    Array.prototype.slice.call(shell.querySelectorAll("[data-driver-shift-logout]")).forEach(function (extraLogout) {
        if (extraLogout.dataset.driverLogoutBound === "true" || !window.MobileShiftHold) return;
        extraLogout.dataset.driverLogoutBound = "true";
        window.MobileShiftHold.bind(extraLogout, {
            holdMs: 2000,
            readyLabel: "Выйти",
            onShortPress: function () {
                if (typeof window.showDriverToast === "function") window.showDriverToast("Удерживайте кнопку");
            },
            onComplete: function () {
                var url = extraLogout.dataset.driverLogoutUrl;
                if (typeof window.navigateAfterNativeConnectionStop === "function") {
                    window.navigateAfterNativeConnectionStop(url);
                    return;
                }
                var stop = window.NativeBackgroundConnection && typeof window.NativeBackgroundConnection.stop === "function"
                    ? window.NativeBackgroundConnection.stop()
                    : null;
                Promise.resolve(stop).finally(function () { window.location.href = url; });
            }
        });
    });
    function isTextEditable(target) {
        return Boolean(target && target.closest && target.closest("input, textarea, select, option, [contenteditable='true'], [contenteditable='']"));
    }
    function clearDriverSelection() {
        var selection = window.getSelection ? window.getSelection() : null;
        if (selection && selection.removeAllRanges) {
            selection.removeAllRanges();
        }
    }
    shell.addEventListener("selectstart", function (event) {
        if (!isTextEditable(event.target)) {
            event.preventDefault();
            clearDriverSelection();
        }
    });
    shell.addEventListener("dragstart", function (event) {
        if (event.target && event.target.closest && event.target.closest("img, svg, canvas, video")) {
            event.preventDefault();
        }
    });
    shell.addEventListener("pointerdown", function (event) {
        if (!isTextEditable(event.target)) {
            clearDriverSelection();
        }
    }, true);
    shell.querySelectorAll(".driver-unload-tile strong").forEach(function (label) {
        var length = label.textContent.trim().length;
        if (length > 15) {
            label.classList.add("is-extra-long");
        } else if (length > 8) {
            label.classList.add("is-long");
        }
    });
    function splitDriverDialLabel(text) {
        var words = String(text || "").trim().replace(/\s+/g, " ").split(" ").filter(Boolean);
        if (words.length <= 3) {
            return words;
        }
        var bestLines = [words.slice(0, 1).join(" "), words.slice(1, -1).join(" "), words.slice(-1).join(" ")];
        var bestScore = Infinity;
        for (var firstBreak = 1; firstBreak <= words.length - 2; firstBreak += 1) {
            for (var secondBreak = firstBreak + 1; secondBreak <= words.length - 1; secondBreak += 1) {
                var lines = [
                    words.slice(0, firstBreak).join(" "),
                    words.slice(firstBreak, secondBreak).join(" "),
                    words.slice(secondBreak).join(" ")
                ];
                var lengths = lines.map(function (line) { return line.length; });
                var longest = Math.max.apply(Math, lengths);
                var shortest = Math.min.apply(Math, lengths);
                var score = (longest * 3) + (longest - shortest);
                if (score < bestScore) {
                    bestScore = score;
                    bestLines = lines;
                }
            }
        }
        return bestLines;
    }
    function preferredDriverDialFontSize(coreWidth, lineCount, textLength) {
        if (lineCount >= 3) {
            return Math.min(40, Math.max(30, coreWidth * 0.15));
        }
        if (lineCount === 2) {
            return Math.min(48, Math.max(35, coreWidth * 0.18));
        }
        if (textLength > 5) {
            return Math.min(54, Math.max(42, coreWidth * 0.2));
        }
        return Math.min(60, Math.max(50, coreWidth * 0.23));
    }
    function minimumDriverDialFontSize(lineCount, textLength) {
        if (lineCount >= 3) {
            return 25;
        }
        if (lineCount === 2) {
            return 28;
        }
        return textLength > 5 ? 34 : 42;
    }
    function renderDriverDialLabel(label, text) {
        var lines = splitDriverDialLabel(text);
        label.replaceChildren();
        lines.forEach(function (line) {
            var lineNode = document.createElement("span");
            lineNode.className = "driver-work-label-line";
            lineNode.textContent = line;
            label.appendChild(lineNode);
        });
        label.dataset.driverDialRaw = text;
        label.setAttribute("aria-label", text);
        label.classList.remove("is-single-medium", "is-two-line", "is-three-line");
        if (lines.length >= 3) {
            label.classList.add("is-three-line");
        } else if (lines.length === 2) {
            label.classList.add("is-two-line");
        } else if (text.replace(/\s+/g, "").length > 5) {
            label.classList.add("is-single-medium");
        }
        return lines;
    }
    function driverDialCoreHasVisibleGeometry(core) {
        if (!core || !core.isConnected || core.hidden) return false;
        if (core.closest && core.closest("[hidden]")) return false;
        if (!core.getClientRects || core.getClientRects().length === 0) return false;
        return core.clientWidth >= 1 && core.clientHeight >= 1;
    }
    function fitDriverDialLabel(label, force) {
        var core = label.closest(".driver-work-dial-core");
        var rawText = label.dataset.driverDialRaw || label.textContent.trim().replace(/\s+/g, " ");
        /* Если текст записали напрямую, строк-спанов нет: значит показывают не то, что
           лежит в driverDialRaw. Берём показанное за исходное и подгоняем заново —
           иначе подпись осталась бы кеглем прежней надписи и вылезла за круг. */
        var firstLine = label.firstElementChild;
        var hasLineNodes = !!(firstLine && firstLine.classList && firstLine.classList.contains("driver-work-label-line"));
        if (!hasLineNodes) {
            var shownText = label.textContent.trim().replace(/\s+/g, " ");
            if (shownText && shownText !== rawText) {
                rawText = shownText;
                label.dataset.driverDialRaw = shownText;
                delete label.dataset.driverDialFitKey;
            }
        }
        if (!rawText) return;
        if (!driverDialCoreHasVisibleGeometry(core)) {
            if (force) delete label.dataset.driverDialFitKey;
            return;
        }
        var coreWidth = Math.round(core.clientWidth);
        var coreHeight = Math.round(core.clientHeight);
        var fitKey = JSON.stringify([rawText, coreWidth, coreHeight]);
        if (!force && label.dataset.driverDialFitKey === fitKey) return;
        delete label.dataset.driverDialFitKey;
        label.style.removeProperty("font-size");
        var lines = renderDriverDialLabel(label, rawText);
        var textLength = rawText.replace(/\s+/g, "").length;
        core.classList.toggle("has-multiline-label", lines.length > 1);
        var maxSize = preferredDriverDialFontSize(coreWidth, lines.length, textLength);
        var minSize = Math.min(maxSize, minimumDriverDialFontSize(lines.length, textLength));
        var low = minSize;
        var high = maxSize;
        var best = minSize;
        /* Высота соседей, зазор сетки и ширина подписи от кегля не зависят — меряем их
           один раз до перебора. Раньше каждый шаг перебора читал их заново, и браузер
           пересчитывал раскладку круга до десяти раз подряд: на телефоне водителя это
           116 мс на каждую смену подписи (а при разгрузке их две подряд). */
        var percentNode = core.querySelector(".driver-work-percent");
        var noteNode = core.querySelector(".driver-work-note");
        var coreStyle = window.getComputedStyle(core);
        var gap = parseFloat(coreStyle.rowGap || coreStyle.gap) || 0;
        var availableHeight = core.clientHeight
            - (percentNode ? percentNode.offsetHeight : 0)
            - (noteNode ? noteNode.offsetHeight : 0)
            - (gap * 2)
            - 4;
        /* clientWidth включает внутренние отступы подписи, а строки меряются по полю
           содержимого: без вычета отступов длинная строка «пролезала» проверку и
           вылезала за рамку на их ширину. */
        var labelStyle = window.getComputedStyle(label);
        var availableWidth = label.clientWidth
            - (parseFloat(labelStyle.paddingLeft) || 0)
            - (parseFloat(labelStyle.paddingRight) || 0)
            + 1;
        function fits(size) {
            label.style.fontSize = size.toFixed(2) + "px";
            var linesFit = Array.prototype.every.call(label.children, function (lineNode) {
                return lineNode.scrollWidth <= availableWidth;
            });
            return linesFit && label.scrollHeight <= availableHeight;
        }
        if (fits(high)) {
            best = high;
        } else {
            /* Шесть шагов дают точность меньше половины пикселя на всём рабочем
               диапазоне кеглей — дальше перебирать нечего. */
            for (var step = 0; step < 6; step += 1) {
                var middle = (low + high) / 2;
                if (fits(middle)) {
                    best = middle;
                    low = middle;
                } else {
                    high = middle;
                }
            }
        }
        label.style.fontSize = best.toFixed(2) + "px";
        label.dataset.driverDialFitKey = fitKey;
    }
    function fitDriverDialLabels(force) {
        shell.querySelectorAll("[data-driver-dial-label]").forEach(function (label) {
            fitDriverDialLabel(label, force);
        });
    }
    function scheduleDriverDialLabelFit(force) {
        if (force) window.driverDialLabelFitForce = true;
        if (
            window.driverDialLabelFitFrame !== null
            && typeof window.driverDialLabelFitFrame !== "undefined"
        ) {
            window.cancelAnimationFrame(window.driverDialLabelFitFrame);
            window.driverDialLabelFitFrame = null;
        }
        window.driverDialLabelFitFrame = window.requestAnimationFrame(function () {
            window.driverDialLabelFitFrame = null;
            var shouldForce = window.driverDialLabelFitForce === true;
            window.driverDialLabelFitForce = false;
            fitDriverDialLabels(shouldForce);
        });
    }
    window.scheduleDriverDialLabelFit = scheduleDriverDialLabelFit;
    /* Синхронный вариант — для мест, где смена текста и подгонка кегля обязаны попасть
       в один и тот же кадр: обычный scheduleDriverDialLabelFit считает через
       requestAnimationFrame, и браузер успевает нарисовать один кадр новым текстом
       ещё старым (слишком крупным) кеглем — на телефоне это была вспышка вылезающей
       за круг подписи при «РАЗГРУЗКА СОХРАНЕНА» (пойман 26.09.2026). */
    window.fitDriverDialLabelNow = function (label) {
        fitDriverDialLabel(label || shell.querySelector("[data-driver-dial-label]"), true);
    };
    if (window.driverDialLabelResizeObserver) {
        window.driverDialLabelResizeObserver.disconnect();
    }
    if ("ResizeObserver" in window) {
        window.driverDialLabelResizeObserver = new ResizeObserver(function (entries) {
            var hasVisibleCore = Array.prototype.some.call(entries, function (entry) {
                return driverDialCoreHasVisibleGeometry(entry.target);
            });
            if (hasVisibleCore) scheduleDriverDialLabelFit();
        });
        shell.querySelectorAll(".driver-work-dial-core").forEach(function (core) {
            window.driverDialLabelResizeObserver.observe(core);
        });
    }
    scheduleDriverDialLabelFit();
    if (document.fonts && document.fonts.ready) {
        document.fonts.ready.then(function () {
            scheduleDriverDialLabelFit(true);
        });
    }
    function generateClientActionId(prefix) {
        if (window.crypto && window.crypto.randomUUID) {
            return prefix + "-" + window.crypto.randomUUID();
        }
        return prefix + "-" + Date.now() + "-" + Math.random().toString(16).slice(2);
    }
    function fillClientAction(form, prefix) {
        var input = form.querySelector("[data-driver-client-action]");
        if (input && !input.value) {
            input.value = generateClientActionId(prefix);
        }
    }
    function clearDriverMessages() {
        var messages = shell.querySelector(".driver-messages");
        if (messages && shell.dataset.activeTab === "downtimes") {
            messages.innerHTML = "";
        }
    }
    /* Форма начала смены привязывается отдельно, за пределами этой области. */
    window.showDriverToast = function (message) { showDriverToast(message); };
    function showDriverToast(message) {
        var toast = shell.querySelector("[data-driver-toast]");
        if (!toast) {
            return;
        }
        toast.textContent = message || "Действие не выполнено";
        toast.hidden = false;
        if (window.driverToastTimerId) {
            window.clearTimeout(window.driverToastTimerId);
        }
        window.driverToastTimerId = window.setTimeout(function () {
            toast.hidden = true;
        }, 3600);
    }
    function openDriverTab(tab) {
        var allowedTabs = ["work", "shift", "downtimes", "manifest"];
        if (allowedTabs.indexOf(tab) < 0) tab = "work";
        /* Новая панель должна получить базовую плотность до первого paint:
           иначе она на кадр наследует tight/compact от предыдущей вкладки. */
        shell.dataset.driverDensity = shell.dataset.driverViewportDensity
            || shell.dataset.driverDensity
            || "normal";
        syncDriverTabMarkup(shell, tab);
        if (
            window.DriverManualExcavatorWorkspace
            && typeof window.DriverManualExcavatorWorkspace.onTabChange === "function"
        ) window.DriverManualExcavatorWorkspace.onTabChange(tab);
        if (tab !== "work" && window.driverDomBehindBaseline === true
            && window.AppRealtime && typeof window.AppRealtime.requestReconcile === "function") {
            window.driverForceFragmentApply = true;
            window.AppRealtime.requestReconcile("driver_tab_opened");
        }
        if (window.history && window.history.replaceState) {
            var url = new URL(window.location.href);
            url.searchParams.set("tab", tab);
            window.history.replaceState({}, "", url.toString());
        }
        try {
            window.localStorage.setItem("driver-active-tab-v1:" + shell.dataset.driverAccessId, tab);
        } catch (error) {}
        clearDriverMessages();
        if (tab === "work") scheduleDriverDialLabelFit();
        scheduleDriverTabSettle(tab);
    }
    (function restoreDriverTab() {
        var allowedTabs = ["work", "shift", "downtimes", "manifest"];
        var requested = "";
        var stored = "";
        try { requested = new URL(window.location.href).searchParams.get("tab") || ""; } catch (error) {}
        try { stored = window.localStorage.getItem("driver-active-tab-v1:" + shell.dataset.driverAccessId) || ""; } catch (error) {}
        var restored = allowedTabs.indexOf(requested) >= 0 ? requested : stored;
        if (allowedTabs.indexOf(restored) >= 0) openDriverTab(restored);
    })();
    document.querySelectorAll("[data-driver-tab-open]").forEach(function (button) {
        button.addEventListener("click", function () {
            openDriverTab(button.dataset.driverTabOpen);
        });
    });

    var manifestPanel = shell.querySelector("[data-driver-tab-panel='manifest']");
    function driverMetricValue(name) {
        var input = shell.querySelector("[name='" + name + "']");
        if (input && String(input.value || "").trim()) return String(input.value).trim();
        if (!manifestPanel) return "—";
        var reportValues = {
            end_fuel: manifestPanel.dataset.driverReportEndFuel,
            end_mileage: manifestPanel.dataset.driverReportEndMileage,
            end_engine_hours: manifestPanel.dataset.driverReportEndEngineHours
        };
        return String(reportValues[name] || "").trim() || "—";
    }
    function syncDriverReportMetrics() {
        if (!manifestPanel) return;
        var units = {end_fuel: " л", end_mileage: " км", end_engine_hours: " м/ч"};
        manifestPanel.querySelectorAll("[data-driver-report-metric]").forEach(function (node) {
            var name = node.dataset.driverReportMetric;
            var value = driverMetricValue(name);
            node.textContent = value === "—" ? value : value + (units[name] || "");
        });
    }
    function buildDriverShiftReportText() {
        if (!manifestPanel) return "";
        syncDriverReportMetrics();
        function tripWord(count) {
            count = Math.abs(Number(count) || 0);
            if (count % 10 === 1 && count % 100 !== 11) return "рейс";
            if ([2, 3, 4].includes(count % 10) && ![12, 13, 14].includes(count % 100)) return "рейса";
            return "рейсов";
        }
        var lines = ["Б-" + manifestPanel.dataset.driverReportTruck + " · " + manifestPanel.dataset.driverReportDriver, "", "РЕЙСЫ"];
        var tripRows = manifestPanel.querySelectorAll("[data-driver-report-trip]");
        if (tripRows.length) {
            tripRows.forEach(function (row) {
                lines.push(row.dataset.excavator + " " + row.dataset.dumpPoint + " - " + row.dataset.count + " " + tripWord(row.dataset.count) + ".");
            });
        } else {
            lines.push("Завершённых рейсов нет");
        }
        lines.push("Всего: " + manifestPanel.dataset.driverReportTripTotal + " " + tripWord(manifestPanel.dataset.driverReportTripTotal) + ".", "", "ПРОСТОИ");
        var downtimeRows = manifestPanel.querySelectorAll("[data-driver-report-downtime]");
        if (downtimeRows.length) {
            downtimeRows.forEach(function (row) {
                lines.push(row.dataset.reason + " — " + row.dataset.duration);
            });
        } else {
            lines.push("Простоев нет");
        }
        lines.push(
            "Всего простоев: " + manifestPanel.dataset.driverReportDowntimeTotal,
            "",
            "ТЕХНИКА НА КОНЕЦ СМЕНЫ",
            "Топливо: " + driverMetricValue("end_fuel") + (driverMetricValue("end_fuel") === "—" ? "" : " л."),
            "Одометр: " + driverMetricValue("end_mileage") + (driverMetricValue("end_mileage") === "—" ? "" : " км."),
            "Моточасы: " + driverMetricValue("end_engine_hours") + (driverMetricValue("end_engine_hours") === "—" ? "" : " м/ч.")
        );
        return lines.join("\n");
    }
    function copyDriverReportText(text) {
        if (navigator.clipboard && window.isSecureContext && typeof navigator.clipboard.writeText === "function") {
            return navigator.clipboard.writeText(text);
        }
        return new Promise(function (resolve, reject) {
            var field = document.createElement("textarea");
            field.value = text;
            field.setAttribute("readonly", "");
            field.style.position = "fixed";
            field.style.opacity = "0";
            document.body.appendChild(field);
            field.select();
            var copied = false;
            try {
                copied = Boolean(document.execCommand && document.execCommand("copy"));
            } catch (error) {}
            field.remove();
            if (copied) resolve();
            else reject(new Error("copy_failed"));
        });
    }
    function openDriverMaxGroup(url) {
        var link = document.createElement("a");
        link.href = url;
        link.target = "_blank";
        link.rel = "noopener noreferrer external";
        link.hidden = true;
        document.body.appendChild(link);
        link.click();
        link.remove();
    }
    function createDriverReportDeliveryController(options) {
        var preparedText = "";
        var copyPending = false;
        function render(ready) {
            options.button.classList.toggle("is-max-ready", ready);
            options.button.dataset.driverReportState = ready ? "max" : "prepare";
            options.title.textContent = ready ? "Открыть группу" : "Подготовить путёвку";
            options.hint.textContent = ready ? "текст уже скопирован" : "для отправки диспетчеру";
        }
        function reset() {
            preparedText = "";
            render(false);
        }
        function handleClick() {
            if (copyPending) return;
            var currentText = options.buildText();
            if (preparedText) {
                if (currentText !== preparedText) {
                    reset();
                    options.notify("Данные изменились. Подготовьте путёвку снова");
                    return;
                }
                options.openGroup(options.groupUrl);
                return;
            }
            copyPending = true;
            options.button.disabled = true;
            Promise.resolve(options.copyText(currentText)).then(function () {
                preparedText = currentText;
                render(true);
                options.notify("Путёвка скопирована. Теперь откройте MAX");
            }).catch(function () {
                reset();
                options.notify("Не удалось скопировать путёвку");
            }).finally(function () {
                copyPending = false;
                options.button.disabled = Boolean(options.isReadonly && options.isReadonly());
            });
        }
        render(false);
        return {handleClick: handleClick, reset: reset};
    }
    if (manifestPanel) {
        manifestPanel.querySelectorAll("[data-driver-manifest-view-open]").forEach(function (button) {
            button.addEventListener("click", function () {
                var view = button.dataset.driverManifestViewOpen;
                manifestPanel.querySelectorAll("[data-driver-manifest-view-open]").forEach(function (item) {
                    var active = item === button;
                    item.classList.toggle("is-active", active);
                    item.setAttribute("aria-selected", active ? "true" : "false");
                });
                manifestPanel.querySelectorAll("[data-driver-manifest-view]").forEach(function (panel) {
                    panel.classList.toggle("is-active", panel.dataset.driverManifestView === view);
                });
            });
        });
        var reportDeliveryButton = manifestPanel.querySelector("[data-driver-report-delivery]");
        var reportDelivery = null;
        if (reportDeliveryButton) {
            reportDelivery = createDriverReportDeliveryController({
                button: reportDeliveryButton,
                title: reportDeliveryButton.querySelector("[data-driver-report-action-title]"),
                hint: reportDeliveryButton.querySelector("[data-driver-report-action-hint]"),
                groupUrl: reportDeliveryButton.dataset.driverMaxGroupUrl,
                buildText: buildDriverShiftReportText,
                copyText: copyDriverReportText,
                openGroup: openDriverMaxGroup,
                notify: showDriverToast,
                isReadonly: driverRoleIsReadonly
            });
            reportDeliveryButton.addEventListener("click", reportDelivery.handleClick);
        }
        shell.querySelectorAll("[name='end_fuel'], [name='end_mileage'], [name='end_engine_hours']").forEach(function (input) {
            input.addEventListener("input", function () {
                syncDriverReportMetrics();
                if (reportDelivery) reportDelivery.reset();
            });
        });
        syncDriverReportMetrics();
    }
    shell.querySelectorAll("form").forEach(function (form) {
        if (!form.matches("[data-driver-hold-form]")) {
            form.addEventListener("submit", function () {
                fillClientAction(form, "driver-action");
            });
        }
    });
    clearDriverMessages();

    var downtimePanel = shell.querySelector("[data-driver-tab-panel='downtimes']");
    var downtimeCard = shell.querySelector("[data-driver-active-downtime-id]");
    var downtimeDuration = shell.querySelector("[data-driver-active-duration]");
    var downtimeTitle = shell.querySelector("[data-driver-active-title]");
    var downtimeReason = shell.querySelector("[data-driver-active-reason]");
    var downtimeClose = shell.querySelector("[data-driver-close-downtime]");
    var downtimeReasonButtons = shell.querySelectorAll("[data-driver-downtime-reason-button]");
    var downtimeUrl = downtimePanel ? downtimePanel.dataset.driverDowntimeUrl : "";
    var downtimeCsrfInput = downtimePanel ? downtimePanel.querySelector("input[name='csrfmiddlewaretoken']") : null;
    var downtimeCsrfToken = downtimeCsrfInput ? downtimeCsrfInput.value : "";
    var holdForm = shell.querySelector("[data-driver-hold-form]");
    var holdButton = shell.querySelector("[data-driver-hold-button]");
    var workDial = shell.querySelector(".driver-work-dial");
    var workDialControl = shell.querySelector("[data-driver-work-dial-control]");
    var driverOfflineEvents = window.driverOfflineEvents || [];

    function driverInstallId() {
        var key = "field-device-install-id-v1";
        var value = "";
        try { value = String(window.localStorage.getItem(key) || ""); } catch (error) {}
        if (!value) {
            value = window.crypto && typeof window.crypto.randomUUID === "function"
                ? window.crypto.randomUUID()
                : "driver-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
            try { window.localStorage.setItem(key, value); } catch (error) { return ""; }
        }
        return value;
    }

    function driverOfflineContext() {
        var current = document.querySelector("[data-driver-shell]");
        return {
            actorId: current && current.dataset.driverActorId,
            accessId: current && current.dataset.driverAccessId,
            shiftId: current && current.dataset.driverShiftId,
            localShiftId: current && current.dataset.driverLocalShiftId,
            equipmentId: current && current.dataset.driverCurrentTruckId,
            authGeneration: current && current.dataset.driverAuthGeneration,
            deviceId: driverInstallId()
        };
    }

    function renderDriverOfflineState(state) {
        var current = document.querySelector("[data-driver-shell]");
        if (!current) return;
        driverOfflineEvents = Array.isArray(state.events) ? state.events : [];
        window.driverOfflineEvents = driverOfflineEvents;
        if (typeof window.syncDriverAssignmentCorner === "function") window.syncDriverAssignmentCorner(driverOfflineEvents);
        var label = current.querySelector("[data-driver-sync-label]");
        var pending = Number(state.pending || 0);
        var review = Number(state.review || 0);
        var previousPending = Number(window.driverOfflinePendingCount || 0);
        window.driverOfflinePendingCount = pending;
        /* Обновление экрана держит только СВЕЖЕЕ действие, которое ещё ни разу
           не пытались отправить: пока идёт первая доставка, серверная разметка
           не должна перекрыть местную проекцию. Запись, у которой уже была
           неудачная попытка или которой больше 15 с, экран не блокирует —
           иначе одна застрявшая запись замораживала экран навсегда
           (боевой случай 20.09.2026: busy=outbox:1 без единого касания). */
        window.driverOfflineBlockingCount = driverOfflineEvents.filter(function (event) {
            if (!event || event.state !== "pending" || Number(event.attempt_count || 0) > 0) return false;
            var occurredAt = Date.parse(event.occurred_at || "");
            return !occurredAt || Date.now() - occurredAt < 15000;
        }).length;
        /* Общей машине связи по-прежнему уходит вся очередь: неотправленное
           действие честно держит индикатор в «восстанавливаем данные», пока
           запись не уйдёт. Экран же держит только свежая запись (см. выше). */
        window.operationalOutboxPendingCount = pending;
        window.dispatchEvent(new CustomEvent("operational-outbox-state", {detail: {
            role: "driver", pendingCount: pending, reviewCount: review
        }}));
        var mode = state.review ? "review" : state.sending ? "sending" : state.pending ? "pending" : "confirmed";
        current.dataset.driverSyncState = mode;
        if (label) {
            label.textContent = mode === "review"
                ? "Не подтверждено"
                : mode === "sending"
                    ? "Отправка"
                    : mode === "pending"
                        ? "Действие сохранено"
                        : "Онлайн";
        }
        // Доступность сервера и accessible-label точки принадлежат общей машине связи.
        applyDriverOfflineProjection(current, driverOfflineEvents);
        if (previousPending > 0 && pending === 0 && window.AppRealtime && typeof window.AppRealtime.requestReconcile === "function") {
            window.AppRealtime.requestReconcile("driver_offline_queue_drained");
        }
    }

    function setDriverPointSyncState(current, mode) {
        var status = current && current.querySelector("[data-driver-point-sync-state]");
        if (!status) return;
        status.classList.toggle("is-local", mode === "local");
        status.classList.toggle("is-review", mode === "review");
        status.textContent = mode === "review"
            ? "Не подтверждено"
            : mode === "local"
                ? "Действие сохранено"
                : "Подтверждено сервером";
    }

    function applyDriverPointSelection(current, pointId, pointName, mode) {
        if (!current || !pointId) return;
        pointId = String(pointId);
        pointName = String(pointName || "").trim();
        current.dataset.driverActualDumpPointId = pointId;
        if (pointName) current.dataset.driverActualDumpPointName = pointName;

        current.querySelectorAll(".driver-unload-tile").forEach(function (tile) {
            tile.classList.remove("is-current");
            tile.removeAttribute("aria-current");
            var tileStatus = tile.querySelector("[data-driver-point-tile-status]");
            if (tileStatus) tileStatus.textContent = "";
        });
        var pointInput = current.querySelector('.driver-unload-tile-form [name="dump_point"][value="' + pointId + '"]');
        var pointButton = pointInput && pointInput.closest("form") && pointInput.closest("form").querySelector(".driver-unload-tile");
        if (pointButton) {
            pointButton.classList.add("is-current");
            pointButton.setAttribute("aria-current", "true");
            pointName = pointName || String(pointButton.dataset.driverPointName || "").trim();
            var selectedStatus = pointButton.querySelector("[data-driver-point-tile-status]");
            if (selectedStatus) selectedStatus.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="m3 8 3 3 7-7"></path></svg>Текущая';
        }
        var currentPointLabel = current.querySelector("[data-driver-current-point-name]");
        if (currentPointLabel && pointName) currentPointLabel.textContent = pointName;
        var dialLabel = current.querySelector("[data-driver-dial-label]");
        if (dialLabel && pointName) {
            dialLabel.textContent = pointName;
            dialLabel.dataset.driverDialRaw = pointName;
            if (typeof scheduleDriverDialLabelFit === "function") scheduleDriverDialLabelFit();
        }
        setDriverPointSyncState(current, mode || "confirmed");
    }

    /* После подмены разметки с сервера местная проекция неотправленных действий
       обязана лечь поверх заново — иначе снятая с сервера разметка вернула бы
       на круг рейс, разгрузку которого телефон ещё не доставил.

       Полная подмена оболочки заново запускает весь bindDriverMobileShell, а
       значит и эту строку — без удаления прежнего обработчика они копятся на
       window НАВСЕГДА, по одному на каждую полную подмену. Старый обработчик
       держит замыкание с downtimeCard от СВОЕГО, уже отсоединённого узла —
       он навсегда пуст (простой начался уже ПОСЛЕ того как этот узел устарел).
       syncDriverDowntimeTimerFromCard в этом устаревшем замыкании видела
       пустую карточку, ждала 700 мс (подтверждение "пустой карточки", #113)
       и гасила НАСТОЯЩИЙ, ещё идущий простой через общий window.driverDowntimeClock
       — при этом видимая (живая) карточка оставалась нетронутой, потому что
       запись шла в отсоединённый узел-призрак. Отсюда и "встал на N секунд,
       потом сам ожил" на бою (27.09.2026, v362): портил не видимый DOM, а
       общее состояние, и через один нормальный цикл живой обработчик сам
       чинил его назад. Держим ровно один обработчик — от последней подмены. */
    if (window.driverDowntimeRefreshHandler) {
        window.removeEventListener("operational-state-refresh-applied", window.driverDowntimeRefreshHandler);
    }
    window.driverDowntimeRefreshHandler = function () {
        var current = document.querySelector("[data-driver-shell]");
        if (current && driverOfflineEvents.length) applyDriverOfflineProjection(current, driverOfflineEvents);
        syncDriverDowntimeTimerFromCard();
    };
    window.addEventListener("operational-state-refresh-applied", window.driverDowntimeRefreshHandler);

    /* Фоновое обновление раз в ~20 с меняет атрибуты карточки состояния простоя
       через послойную подмену (syncAttributes), но сам JS-таймер (замыкание
       setInterval) заново не привязывается — экран уже «привязан» и повторный
       bindDriverMobileShell выходит по защите на входе функции. Раньше это
       давало замороженный счётчик старой причины после любого стороннего
       изменения простоя (пойман на реальном полевом тесте 26.09.2026):
       переустанавливаем таймер отдельно, сверяя его с реальным содержимым
       карточки на каждое обновление, а не только при собственном касании
       водителя. */
    var driverDowntimeCardBlankTimer = null;
    var driverDowntimeCardBlankConfirmed = false;
    function syncDriverDowntimeTimerFromCard() {
        if (!downtimeCard) return;
        var cardReasonId = String(downtimeCard.dataset.driverActiveReasonId || "");
        var cardDowntimeId = String(downtimeCard.dataset.driverActiveDowntimeId || "");
        var clock = window.driverDowntimeClock;
        var clockReasonId = clock ? String(clock.activeReasonId || "") : "";
        var cardEventId = cardDowntimeId;
        var clockEventId = window.driverDowntimeActiveEventId || "";
        if (cardReasonId === clockReasonId && cardEventId === clockEventId) {
            if (driverDowntimeCardBlankTimer) { window.clearTimeout(driverDowntimeCardBlankTimer); driverDowntimeCardBlankTimer = null; }
            driverDowntimeCardBlankConfirmed = false;
            return;
        }
        if (!cardReasonId && !cardDowntimeId && clock && !driverDowntimeCardBlankConfirmed) {
            /* Пустая карточка, хотя таймер только что показывал активный простой, —
               подозрительно: некоторые обновления фрагмента (реконсайл раз в ~20 с)
               на короткое время теряют состояние простоя в карточке, хотя простой
               реально не закрывался (боевой 27.09.2026, v359) — окантовка гасла и
               мигала, таймер обнулялся на пустом месте. Пустое значение само по себе
               не доказательство закрытия — гасим только если оно ПОДТВЕРДИТСЯ ещё
               раз чуть позже, а не по одному наблюдению. */
            if (!driverDowntimeCardBlankTimer) {
                driverDowntimeCardBlankTimer = window.setTimeout(function () {
                    driverDowntimeCardBlankTimer = null;
                    driverDowntimeCardBlankConfirmed = true;
                    syncDriverDowntimeTimerFromCard();
                    driverDowntimeCardBlankConfirmed = false;
                }, 700);
            }
            return;
        }
        if (driverDowntimeCardBlankTimer) { window.clearTimeout(driverDowntimeCardBlankTimer); driverDowntimeCardBlankTimer = null; }
        window.driverDowntimeActiveEventId = cardEventId;
        if (!cardReasonId) {
            clearDriverActiveDowntime({
                shift_total_seconds: Number(downtimeCard.dataset.driverShiftDowntimeSeconds) || 0
            });
            return;
        }
        var cardReasonButton = shell.querySelector(
            '[data-driver-downtime-reason-button][data-driver-downtime-reason-id="' + cardReasonId + '"]'
        );
        applyDriverActiveDowntime({
            event_id: cardEventId,
            reason_id: cardReasonId,
            workflow: downtimeCard.dataset.driverActiveDowntimeFlow || "",
            started_at: downtimeCard.dataset.driverActiveStartedAt || "",
            elapsed_seconds: Number(downtimeCard.dataset.driverActiveElapsedSeconds) || 0,
            shift_total_seconds: Number(downtimeCard.dataset.driverShiftDowntimeSeconds) || 0,
            calculated_at: downtimeCard.dataset.driverDowntimeCalculatedAt || "",
            status_key: (downtimeCard.className.match(/status-(\w+)/) || [])[1] || "red",
            reason: (cardReasonButton && cardReasonButton.dataset.driverReasonLabel) || "Простой"
        });
    }

    function tripTerminalEventClosesWaitingUnload(events, latestDowntime, activeFlow) {
        if (String(activeFlow || "") !== "waiting_unload") return null;
        var latestDowntimeSequence = latestDowntime ? Number(latestDowntime.sequence || 0) : 0;
        return (events || []).slice().reverse().find(function (event) {
            return (
                (event.event_type === "driver.trip.unloaded" || event.event_type === "driver.trip.manual_completed")
                && ["conflict", "auth_required", "invalid"].indexOf(String(event.state || "pending")) === -1
                && Number(event.sequence || 0) > latestDowntimeSequence
            );
        }) || null;
    }

    /* Круг без рейса: самосвал пустеет и едет за погрузкой. Общая отрисовка для
       разгрузки из очереди и для рейса прошлой смены, снятого с экрана новой
       местной смены (driverDropForeignShiftState). */
    function showDriverTripGone(current, unloadNeedsReview) {
        current.dataset.driverHasOpenTrip = "false";
        current.dataset.driverHasLoadedTrip = "false";
        current.dataset.driverActiveTripId = "";
        current.dataset.driverActiveTripOrigin = "";
        current.dataset.driverActiveTripLoadedAt = "";
        var dial = current.querySelector(".driver-work-dial");
        var button = current.querySelector("[data-driver-hold-button]");
        var dialLabel = current.querySelector("[data-driver-dial-label]");
        var note = current.querySelector(".driver-work-note");
        if (dial) { dial.classList.remove("is-loaded"); dial.classList.add("is-empty"); }
        if (button) {
            button.disabled = true;
            button.classList.remove("is-loaded", "is-pending", "is-holding");
            button.classList.add("is-empty");
        }
        if (dialLabel) {
            /* Подпись обязана пройти подгонку под круг: раньше сюда писали только текст,
               прежний ключ подгонки оставался прежним, и длинная надпись выводилась
               кеглем короткой — она вылезала за круг и обрезалась. */
            /* Куда ехать дальше после разгрузки телефон знает сам, без сервера:
               назначение на экскаватор разгрузкой не снимается, самосвал просто
               пустеет и едет за новой погрузкой к тому же экскаватору. Раньше
               тут держали заглушку «ожидание синхронизации» до ответа сервера —
               на нестабильной связи это давало задержку в минуты (реальный
               полевой тест 26.09.2026). */
            var manualWorkspace = current.querySelector("[data-driver-manual-workspace]");
            // Разгрузка рейса под свободным ковшом — ковш был на один рейс,
            // дальше основной экскаватор (29.09.2026).
            var bucketTrip = manualWorkspace && manualWorkspace.dataset.driverManualAuthorityType === "free_bucket";
            var nextExcavatorLabel = manualWorkspace
                ? String(
                    (bucketTrip
                        ? manualWorkspace.dataset.driverManualPrimaryExcavatorLabel
                        : manualWorkspace.dataset.driverManualExcavatorLabel)
                    || manualWorkspace.dataset.driverManualPrimaryExcavatorLabel
                    || manualWorkspace.dataset.driverManualExcavatorLabel
                    || ""
                )
                : "";
            var unloadDialText = unloadNeedsReview
                ? "НЕ ПОДТВЕРЖДЕНО"
                : (nextExcavatorLabel || "НА ЗАГРУЗКУ");
            dialLabel.textContent = unloadDialText;
            dialLabel.dataset.driverDialRaw = unloadDialText;
            delete dialLabel.dataset.driverDialFitKey;
            if (typeof scheduleDriverDialLabelFit === "function") scheduleDriverDialLabelFit();
        }
        if (note) note.textContent = unloadNeedsReview ? "ПРОВЕРЬТЕ СОБЫТИЕ" : "НА ЗАГРУЗКУ";
        var pointCard = current.querySelector("[data-driver-point-open]");
        if (pointCard) {
            pointCard.disabled = true;
            pointCard.hidden = true;
            pointCard.setAttribute("aria-expanded", "false");
        }
        var sheet = current.querySelector("[data-driver-point-sheet]");
        if (sheet) sheet.hidden = true;
    }

    /* Пересменка без сети (матрица B3a, 30.09.2026): страница нарисована
       сервером для прошлой смены с идущим простоем. После местного закрытия и
       открытия новая смена несла его на себе: кнопки причин молчали («простой
       уже идёт»). Простой заканчивается закрытием смены (правило 8; телефон
       ставит завершение в очередь — driver-local-shift-v1.js), поэтому
       простой, нарисованный сервером для другой смены, снимаем с экрана. Свои
       простои новой смены («local:…») не трогаем. Гружёный рейс остаётся:
       он переходит к следующей смене (решение 13 владельца), его разгружают. */
    function driverShellShowsForeignShift(current) {
        return String(current.dataset.driverShiftId || "") !== String(current.dataset.driverServerShiftId || "");
    }
    window.driverDropForeignShiftState = function (target) {
        var current = target || document.querySelector("[data-driver-shell]");
        if (!current || current !== shell || !driverShellShowsForeignShift(current)) return false;
        var activeDowntimeId = String(downtimeCard && downtimeCard.dataset.driverActiveDowntimeId || "");
        if (!activeDowntimeId || activeDowntimeId.indexOf("local:") === 0) return false;
        clearDriverActiveDowntime();
        document.documentElement.classList.remove("is-driver-downtime-active");
        return true;
    };

    function applyDriverOfflineProjection(current, events) {
        if (!current) return;
        var ordered = (events || []).slice().sort(function (a, b) { return Number(a.sequence) - Number(b.sequence); });
        var activeTripId = String(current.dataset.driverActiveTripId || "");
        var manualTrip = String(current.dataset.driverActiveTripOrigin || "") === "driver_manual";
        var unload = manualTrip ? null : ordered.find(function (event) {
            return event.event_type === "driver.trip.unloaded" && String(event.trip_id || "") === activeTripId;
        });
        if (unload) {
            showDriverTripGone(current, ["conflict", "auth_required", "invalid"].includes(unload.state));
        }
        var pointEvents = ordered.filter(function (event) {
            return event.event_type === "driver.trip.dump_point_changed" && String(event.trip_id || "") === activeTripId;
        });
        if (!unload && pointEvents.length) {
            var latestPoint = pointEvents[pointEvents.length - 1];
            var pointId = String(latestPoint.payload && latestPoint.payload.dump_point_id || "");
            var pointForm = current.querySelector('.driver-unload-tile-form [name="dump_point"][value="' + pointId + '"]');
            var pointButton = pointForm && pointForm.closest("form") && pointForm.closest("form").querySelector(".driver-unload-tile");
            var pointName = pointButton && pointButton.dataset.driverPointName || "";
            var pointMode = ["conflict", "auth_required", "invalid"].includes(latestPoint.state) ? "review" : "local";
            applyDriverPointSelection(current, pointId, pointName, pointMode);
        }
        var latestDowntime = typeof window.selectDriverDowntimeProjection === "function"
            ? window.selectDriverDowntimeProjection(ordered)
            : ordered.slice().reverse().find(function (event) {
                return (event.event_type === "driver.downtime.started" || event.event_type === "driver.downtime.ended")
                    && ["conflict", "auth_required", "invalid"].indexOf(String(event.state || "pending")) === -1;
            });
        var latestDowntimeReasonButton = null;
        var projectedDowntimeFlow = downtimeCard && downtimeCard.dataset.driverActiveDowntimeFlow || "";
        if (latestDowntime && latestDowntime.event_type === "driver.downtime.started") {
            var latestDowntimeReasonId = String(latestDowntime.payload && latestDowntime.payload.reason_id || "");
            latestDowntimeReasonButton = current.querySelector(
                '[data-driver-downtime-reason-button][data-driver-downtime-reason-id="' + latestDowntimeReasonId + '"]'
            );
            projectedDowntimeFlow = latestDowntimeReasonButton && latestDowntimeReasonButton.dataset.driverDowntimeFlow || projectedDowntimeFlow;
        }
        var tripTerminalEvent = tripTerminalEventClosesWaitingUnload(ordered, latestDowntime, projectedDowntimeFlow);
        if (tripTerminalEvent) {
            snapshotDriverDowntimeTimer(Date.parse(tripTerminalEvent.occurred_at || ""));
            clearDriverActiveDowntime({
                shift_total_seconds: downtimeCard && downtimeCard.dataset.driverShiftDowntimeSeconds || 0,
                calculated_at: tripTerminalEvent.occurred_at || ""
            });
        } else if (latestDowntime) {
            if (latestDowntime.event_type === "driver.downtime.ended") {
                clearDriverActiveDowntime({
                    shift_total_seconds: downtimeCard && downtimeCard.dataset.driverShiftDowntimeSeconds || 0
                });
            } else {
                var reasonId = String(latestDowntime.payload && latestDowntime.payload.reason_id || "");
                var reasonButton = latestDowntimeReasonButton || current.querySelector('[data-driver-downtime-reason-button][data-driver-downtime-reason-id="' + reasonId + '"]');
                applyDriverActiveDowntime({
                    active: true,
                    event_id: "local:" + latestDowntime.event_id,
                    reason_id: reasonId,
                    reason: reasonButton && reasonButton.dataset.driverReasonLabel || "Простой",
                    reason_label: reasonButton && reasonButton.dataset.driverReasonLabel || "Простой",
                    workflow: reasonButton && reasonButton.dataset.driverDowntimeFlow || "",
                    status_key: reasonButton && reasonButton.dataset.driverStatusKey || "yellow",
                    started_at: latestDowntime.occurred_at,
                    elapsed_seconds: 0,
                    shift_total_seconds: downtimeCard && downtimeCard.dataset.driverShiftDowntimeSeconds || 0,
                    calculated_at: latestDowntime.occurred_at
                });
            }
        }
        if (window.DriverFreeBucket && typeof window.DriverFreeBucket.renderProjection === "function") {
            window.DriverFreeBucket.renderProjection(current, ordered);
        }
        if (
            window.DriverManualExcavatorWorkspace
            && typeof window.DriverManualExcavatorWorkspace.restoreProjection === "function"
        ) {
            window.DriverManualExcavatorWorkspace.restoreProjection(
                window.driverOfflineOutbox,
                current.querySelector("[data-driver-manual-workspace]")
            ).catch(function () {});
        }
    }

    /* Страница в кэше телефона — та, что была при последней загрузке. Без
       сети после перезапуска она и показывается, поэтому после подтверждений
       сервера и при уходе приложения в фон просим service worker положить
       свежую (users/views.py, REFRESH_AUTHENTICATED_SHELL). Не чаще раза в 20 с. */
    function requestDriverShellCacheRefresh(delayMs) {
        var worker = navigator.serviceWorker && navigator.serviceWorker.controller;
        if (!worker || navigator.onLine === false) return;
        if (window.__driverShellCacheRefreshTimer) return;
        window.__driverShellCacheRefreshTimer = window.setTimeout(function () {
            window.__driverShellCacheRefreshTimer = null;
            var last = Number(window.__driverShellCacheRefreshAt || 0);
            if (Date.now() - last < 20000) return;
            window.__driverShellCacheRefreshAt = Date.now();
            var current = navigator.serviceWorker && navigator.serviceWorker.controller;
            if (current) {
                try { current.postMessage({type: "REFRESH_AUTHENTICATED_SHELL"}); } catch (error) {}
            }
        }, Math.max(0, Number(delayMs) || 0));
    }
    window.driverRequestShellCacheRefresh = requestDriverShellCacheRefresh;
    if (!window.__driverShellCacheRefreshBound) {
        window.__driverShellCacheRefreshBound = true;
        document.addEventListener("visibilitychange", function () {
            if (document.hidden) requestDriverShellCacheRefresh(0);
        });
    }

    function driverOfflineBindings() {
        return {
            context: driverOfflineContext,
            onState: function (state) {
                renderDriverOfflineState(state);
                /* Путёвка ведёт свой журнал смены из тех же событий очереди. */
                if (window.DriverManifestLocal && typeof window.DriverManifestLocal.observe === "function") {
                    try { window.DriverManifestLocal.observe(state && state.events); } catch (error) {}
                }
            },
            onConfirmed: function () {
                var args = arguments;
                var event = args[0] || {};
                if (window.DriverManifestLocal && typeof window.DriverManifestLocal.confirmed === "function") {
                    try { window.DriverManifestLocal.confirmed(event, args[1] || {}); } catch (error) {}
                }
                if (typeof window.driverRequestShellCacheRefresh === "function") window.driverRequestShellCacheRefresh(4000);
                if (window.DriverLocalShift && typeof window.DriverLocalShift.onConfirmed === "function") {
                    window.DriverLocalShift.onConfirmed(event, args[1] || {});
                }
                /* Разгрузку и завершение ручного рейса голос подтверждает сразу, по
                   записи на телефоне (showDriverDialConfirmed). Ответ сервера приходит
                   позже, иногда пачкой — второй голос тогда звучал бы невпопад. */
                if (
                    event.event_type === "driver.trip.loaded"
                    && window.DriverManualExcavatorWorkspace
                    && typeof window.DriverManualExcavatorWorkspace.restoreProjection === "function"
                ) {
                    window.DriverManualExcavatorWorkspace.restoreProjection(window.driverOfflineOutbox).catch(function () {});
                }
                var result = args[1] || {};
                var isDowntime = event.event_type === "driver.downtime.started" || event.event_type === "driver.downtime.ended";
                if (isDowntime) {
                    var serverIds = result.server_ids || {};
                    var downtimeId = String(serverIds.downtime_event_id || serverIds.downtime_id || "");
                    if (downtimeId) {
                        window.driverOwnDowntimeEventIds = (window.driverOwnDowntimeEventIds || []).concat(downtimeId).slice(-50);
                    }
                }
                if (window.AppRealtime && typeof window.AppRealtime.requestReconcile === "function") {
                    // Для простоя версию не передаём: иначе опрос спросит события «после неё»
                    // и не вернёт наш же простой — а он и есть доказательство, что экран
                    // подменять не нужно (см. applyOperationalStateRefresh).
                    if (isDowntime) {
                        window.AppRealtime.requestReconcile("driver_offline_event_confirmed");
                    } else {
                        window.AppRealtime.requestReconcile(
                            "driver_offline_event_confirmed",
                            Number(result.server_version || result.version || 0)
                        );
                    }
                }
            },
            onReview: function (event, result) {
                if (window.DriverManifestLocal && typeof window.DriverManifestLocal.review === "function") {
                    try { window.DriverManifestLocal.review(event, result || {}); } catch (error) {}
                }
                showDriverToast(result.message || "Действие не подтверждено сервером. Обновите экран; если состояние неверное — сообщите диспетчеру.");
                if (
                    event
                    && (event.event_type === "driver.downtime.started" || event.event_type === "driver.downtime.ended")
                    && window.AppRealtime
                    && typeof window.AppRealtime.requestReconcile === "function"
                ) {
                    window.AppRealtime.requestReconcile(
                        "driver_downtime_review",
                        Number(result.server_version || result.version || 0)
                    );
                }
            }
        };
    }

    function createDriverOfflineRuntime() {
        if (typeof window.createDriverOfflineOutbox !== "function") {
            return {
                enqueue: function () { return Promise.reject(new Error("offline_runtime_unavailable")); },
                flush: function () { return Promise.resolve([]); },
                pending: function () { return Promise.resolve([]); },
                publish: function () { return Promise.resolve([]); },
                getServerMapping: function () { return Promise.resolve(null); },
                getDowntimeProjectionReceipt: function () { return Promise.resolve(null); },
                getManualTripProjectionReceipt: function () { return Promise.resolve(null); }
            };
        }
        if (window.driverOfflineOutbox && window.driverOfflineOutboxAccessId === shell.dataset.driverAccessId) {
            var existingBindings = driverOfflineBindings();
            window.driverOfflineOutbox.setBindings(existingBindings).then(function () {
                return window.driverOfflineOutbox.resumeAuthRequired(driverOfflineContext().authGeneration);
            }).then(function () {
                return window.driverOfflineOutbox.flush();
            }).catch(function () {});
            return window.driverOfflineOutbox;
        }
        window.driverOfflineOutboxAccessId = shell.dataset.driverAccessId;
        var csrf = document.querySelector('meta[name="csrf-token"]');
        window.driverOfflineOutbox = window.createDriverOfflineOutbox({
            accessId: shell.dataset.driverAccessId,
            indexedDB: window.indexedDB,
            localStorage: window.localStorage,
            context: driverOfflineContext,
            send: function (batch) {
                return fetch("/offline-events/sync/", {
                    method: "POST",
                    credentials: "same-origin",
                    cache: "no-store",
                    headers: {
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "X-CSRFToken": csrf ? csrf.content : "",
                        "X-Requested-With": "XMLHttpRequest"
                    },
                    body: JSON.stringify(batch)
                }).then(function (response) {
                    return response.text().then(function (body) {
                        var payload = {};
                        try { payload = JSON.parse(body || "{}"); } catch (error) {}
                        if (response.status >= 500) throw new Error("server_unavailable");
                        var needsAuthentication = typeof window.isDriverSyncAuthResponse === "function"
                            && window.isDriverSyncAuthResponse(response, body, window.location.href);
                        if (needsAuthentication) {
                            return {results: batch.events.map(function (event) {
                                return {event_id: event.event_id, status: "auth_required", code: "auth_required", message: "Требуется повторный вход."};
                            })};
                        }
                        if (!response.ok && !Array.isArray(payload.results)) throw new Error("sync_rejected");
                        return payload;
                    });
                });
            },
            onState: driverOfflineBindings().onState,
            onConfirmed: driverOfflineBindings().onConfirmed,
            onReview: driverOfflineBindings().onReview
        });
        window.driverOfflineOutbox.initialize().catch(function () {
            var current = document.querySelector("[data-driver-shell]");
            if (current) {
                current.dataset.driverSyncState = "storage-error";
                var connection = current.querySelector(".driver-online");
                if (connection) connection.setAttribute("aria-label", "Локальное сохранение недоступно; действие не выполнено");
            }
            showDriverToast("Не удалось открыть защищённое хранилище. Действия без связи недоступны.");
        });
        return window.driverOfflineOutbox;
    }

    var driverOfflineOutbox = createDriverOfflineRuntime();
    // Проекция смены шла раньше привязки экрана — чужое состояние снимаем здесь.
    window.driverDropForeignShiftState(shell);
    /* Замок контракта не должен включаться из-за отсутствия сети (владелец,
       30.09.2026). Оболочка из кэша той же версии, что service worker, — её
       скрипты и воркер сверяются на месте; сервер лишь подтверждает, что не
       ушёл на новую версию. Раньше замок ждал его ответа: без сети — до 12 с
       «грейса», на слабом сигнале — до таймаута опроса, и всё это время кнопки
       не работали. Теперь ответ сервера не ждём: пришёл и не совпал — замок и
       обновление включатся штатно (acceptServerContract). */
    (function releaseServerContractWait() {
        var guard = window.AppPwaContractGuard;
        if (!guard || typeof guard.getState !== "function" || typeof guard.markServerUnavailable !== "function") return;
        var contract = guard.getState();
        if (contract && !contract.server && !contract.serverUnavailable) guard.markServerUnavailable();
    })();
    /* Оболочка теперь приходит из кэша сразу (service worker, 30.09.2026) и
       может быть старше сервера: другая сессия после повторного входа (очередь
       ждёт новый auth-generation), иное состояние смены или рейса. Один раз за
       загрузку страницы со связью сверяемся с сервером фрагментом: совпал со
       снимком — экран не трогается, не совпал — обновляется, и очередь
       возобновляется уже с текущим входом. */
    if (
        !window.__driverCachedShellVerified
        && navigator.onLine !== false
        && typeof window.applyOperationalStateRefresh === "function"
    ) {
        window.__driverCachedShellVerified = true;
        var verifyAttempts = 0;
        var verifyCachedShell = function () {
            verifyAttempts += 1;
            Promise.resolve(window.applyOperationalStateRefresh({
                version: Number(document.body.dataset.operationalStateVersion || 0),
                reason: "driver_cached_shell_start"
            })).then(function (result) {
                /* Экран занят (ввод, скрытая вкладка) — сверка откладывается,
                   а не пропадает: повторяем, пока не получится. */
                if (result && result.deferred && verifyAttempts < 12) {
                    window.setTimeout(verifyCachedShell, 5000);
                }
                var outbox = window.driverOfflineOutbox;
                var current = document.querySelector("[data-driver-shell]");
                if (!outbox || !current || typeof outbox.resumeAuthRequired !== "function") return null;
                return outbox.resumeAuthRequired(String(current.dataset.driverAuthGeneration || "")).then(function () {
                    return outbox.flush();
                });
            }).catch(function () {});
        };
        window.setTimeout(verifyCachedShell, 400);
    }
    function restoreDriverConfirmedDowntime(outbox) {
        var context = driverOfflineContext();
        if (!downtimeCard || !outbox || typeof outbox.getDowntimeProjectionReceipt !== "function") {
            return Promise.resolve(false);
        }
        return outbox.getDowntimeProjectionReceipt(context.shiftId, context.equipmentId).then(function (receipt) {
            if (!receipt) return false;
            var receiptAt = Date.parse(receipt.confirmed_at || receipt.occurred_at || "");
            var shellAt = Date.parse(downtimeCard.dataset.driverDowntimeCalculatedAt || "");
            if (!Number.isFinite(receiptAt) || (Number.isFinite(shellAt) && receiptAt <= shellAt)) {
                return false;
            }
            if (receipt.event_type === "driver.downtime.ended") {
                var closedProjection = receipt.projection || {};
                clearDriverActiveDowntime({
                    shift_total_seconds: closedProjection.shift_total_seconds || downtimeCard.dataset.driverShiftDowntimeSeconds || 0,
                    reason_totals: closedProjection.reason_totals || null,
                    calculated_at: receipt.confirmed_at || receipt.occurred_at
                });
                return true;
            }
            if (receipt.event_type !== "driver.downtime.started") return false;
            var reasonId = String(receipt.payload && receipt.payload.reason_id || "");
            var reasonButton = shell.querySelector('[data-driver-downtime-reason-button][data-driver-downtime-reason-id="' + reasonId + '"]');
            var serverIds = receipt.server_ids || {};
            var activeProjection = receipt.projection || {};
            return applyDriverActiveDowntime({
                active: true,
                event_id: String(serverIds.downtime_event_id || serverIds.downtime_id || "confirmed:" + receipt.event_id),
                reason_id: reasonId,
                reason: reasonButton && reasonButton.dataset.driverReasonLabel || "Простой",
                reason_label: reasonButton && reasonButton.dataset.driverReasonLabel || "Простой",
                workflow: reasonButton && reasonButton.dataset.driverDowntimeFlow || "",
                status_key: reasonButton && reasonButton.dataset.driverStatusKey || "yellow",
                started_at: receipt.occurred_at,
                elapsed_seconds: activeProjection.active_elapsed_seconds || 0,
                shift_total_seconds: activeProjection.shift_total_seconds || downtimeCard.dataset.driverShiftDowntimeSeconds || 0,
                reason_totals: activeProjection.reason_totals || null,
                calculated_at: receipt.confirmed_at || receipt.occurred_at
            });
        });
    }
    restoreDriverConfirmedDowntime(driverOfflineOutbox).catch(function () {});
    if (
        window.DriverManualExcavatorWorkspace
        && typeof window.DriverManualExcavatorWorkspace.restoreProjection === "function"
    ) {
        window.DriverManualExcavatorWorkspace.restoreProjection(driverOfflineOutbox).catch(function () {});
    }
    if (window.DriverFreeBucket && typeof window.DriverFreeBucket.bind === "function") {
        window.DriverFreeBucket.bind({shell: shell, outbox: driverOfflineOutbox});
    }

    function formatDriverDowntimeDuration(seconds) {
        seconds = Math.max(0, Math.floor(Number(seconds) || 0));
        var hours = Math.floor(seconds / 3600);
        var minutes = Math.floor((seconds % 3600) / 60);
        var rest = seconds % 60;
        return [hours, minutes, rest].map(function (part) {
            return String(part).padStart(2, "0");
        }).join(":");
    }

    function clearDriverDowntimeTimer() {
        if (window.driverDowntimeTimerId) {
            window.clearInterval(window.driverDowntimeTimerId);
            window.driverDowntimeTimerId = null;
        }
        window.driverDowntimeClock = null;
    }

    function renderDriverReasonDuration(button, totalSeconds, isActive) {
        if (!button) return;
        var duration = button.querySelector("[data-driver-reason-duration]");
        if (!duration) return;
        var seconds = Math.max(0, Math.floor(Number(totalSeconds) || 0));
        var isVisible = seconds > 0 || !!isActive;
        duration.hidden = !isVisible;
        button.classList.toggle("is-used", isVisible);
        if (isVisible) {
            duration.textContent = formatDriverDowntimeDuration(seconds);
        }
        if (button.getAttribute("aria-disabled") !== "true") {
            var reasonLabel = button.dataset.driverReasonLabel || button.dataset.driverReason || "Причина простоя";
            var actionLabel = isActive ? "Активный простой" : "Начать простой";
            button.setAttribute(
                "aria-label",
                actionLabel + ": " + reasonLabel + (isVisible ? ". За смену: " + formatDriverDowntimeDuration(seconds) : "")
            );
        }
    }

    function syncDriverReasonTotals(payload, skipReasonId) {
        payload = payload || {};
        var reasonTotals = payload.reason_totals;
        var activeReasonId = payload.active ? String(payload.reason_id || "") : "";
        downtimeReasonButtons.forEach(function (button) {
            var reasonId = String(button.dataset.driverDowntimeReasonId || "");
            if (skipReasonId && reasonId === String(skipReasonId)) {
                /* Эта кнопка тикает своим отсчётом (now - started_at) — снимок
                   сервера её не трогает, чтобы не сбить чистую функцию времени
                   посторонним значением между двумя тиками. */
                return;
            }
            if (
                reasonTotals
                && typeof reasonTotals === "object"
                && Object.prototype.hasOwnProperty.call(reasonTotals, reasonId)
            ) {
                button.dataset.driverReasonSeconds = String(
                    Math.max(0, Math.floor(Number(reasonTotals[reasonId]) || 0))
                );
            }
            renderDriverReasonDuration(
                button,
                button.dataset.driverReasonSeconds,
                !!activeReasonId && reasonId === activeReasonId
            );
        });
    }

    /* "local:<uuid>" становится серверным числовым ID в момент синхронизации
       офлайн-записи простоя (driver-offline-outbox-v2.js пишет алиас в
       window.driverDowntimeIdAliases синхронно, до того как удалить
       подтверждённую запись из очереди). Раз алиас известен — оба вида ID
       сводятся к одному и тому же серверному значению. */
    function driverDowntimeCanonicalEventId(eventId) {
        var raw = String(eventId || "");
        var stripped = raw.indexOf("local:") === 0 ? raw.slice(6) : raw;
        var aliases = window.driverDowntimeIdAliases || {};
        return String(aliases[stripped] || stripped);
    }

    /* Признак «это тот же самый простой» — причина + время начала, округлённое
       до секунды. ЗАПАСНОЙ признак, а не основной: started_at, который прислал
       телефон СВОИМ (возможно, сбитым) временем в момент нажатия, и started_at,
       который вернул сервер (уже с поправкой на расхождение часов —
       device_clock_ahead/behind), могут отличаться на минуты и даже на
       полчаса на телефоне со сбитыми часами (боевой Xiaomi, координатор
       27.09.2026). Используется только когда серверный алиас ещё не известен —
       то есть строго ДО первой синхронизации, когда обе стороны читают
       started_at с одних и тех же (пока не скорректированных) часов телефона
       и расхождения ещё нет. */
    function driverDowntimeIdentityKey(reasonId, startedAt) {
        var startedAtMs = Date.parse(startedAt || "");
        if (!Number.isFinite(startedAtMs)) return "";
        return String(reasonId || "") + "|" + Math.floor(startedAtMs / 1000);
    }

    /* Простои, закрытые ЭТИМ телефоном (клик «завершить» или honest server
       truth), не должны воскресать из более старого локального снимка/
       фрагмента — под нагрузкой (много техники меняет состояние в ту же
       секунду) экран перерисовывается заметно чаще, и устаревшая запись
       «простой начат», которая ещё не успела уступить место записи
       «завершён» в локальном списке офлайн-событий, повторно применялась как
       активная — мигала окантовка, простой «оживал» на долю секунды между
       обновлениями (боевой 27.09.2026, v359). Список короткий и живёт только
       в памяти вкладки — переживать перезагрузку страницы ему не нужно:
       после перезагрузки экран целиком строится с нуля из честного
       серверного снимка. Список живёт на window, а не в замыкании этой
       функции: полная подмена оболочки (driverMorphShell не справился,
       oldShell.replaceWith(freshShell)) заново запускает bindDriverMobileShell
       на свежем узле — локальный var потерял бы память о закрытых простоях
       ровно в момент, когда она нужнее всего. */
    function driverDowntimeClosedKeys(reasonId, startedAt, eventId) {
        var keys = [];
        var canonical = eventId ? driverDowntimeCanonicalEventId(eventId) : "";
        if (canonical) keys.push("id:" + canonical);
        var identityKey = driverDowntimeIdentityKey(reasonId, startedAt);
        if (identityKey) keys.push("at:" + identityKey);
        return keys;
    }
    function markDriverDowntimeInstanceClosed(reasonId, startedAt, eventId) {
        var keys = driverDowntimeClosedKeys(reasonId, startedAt, eventId);
        if (!keys.length) return;
        var list = (window.driverDowntimeClosedInstances || []).filter(function (item) { return keys.indexOf(item) < 0; });
        list = list.concat(keys);
        while (list.length > 16) list.shift();
        window.driverDowntimeClosedInstances = list;
    }
    function driverDowntimeInstanceIsClosed(reasonId, startedAt, eventId) {
        var keys = driverDowntimeClosedKeys(reasonId, startedAt, eventId);
        if (!keys.length) return false;
        var list = window.driverDowntimeClosedInstances || [];
        return keys.some(function (key) { return list.indexOf(key) >= 0; });
    }

    function startDriverDowntimeTimer(payload) {
        payload = payload || {};
        var activeReasonId = String(payload.reason_id || "");
        var eventId = String(payload.event_id || "");
        var canonicalEventId = driverDowntimeCanonicalEventId(eventId);
        var identityKey = driverDowntimeIdentityKey(activeReasonId, payload.started_at);
        var existing = window.driverDowntimeClock;
        /* Канонический ID у СУЩЕСТВУЮЩЕГО таймера пересчитывается заново, а не
           берётся из кэша на объекте часов: алиас "local:<uuid>" → серверный
           ID мог появиться уже ПОСЛЕ того, как этот таймер запустился — кэш
           навсегда остался бы со старым (нерастворённым) значением и никогда
           не совпал бы с новым payload, у которого алиас уже известен. */
        var existingCanonicalEventId = existing ? driverDowntimeCanonicalEventId(existing.eventId) : "";
        var sameInstance = existing && existing.activeReasonId === activeReasonId && (
            /* canonicalEventId — основной признак: "local:<uuid>" и серверный
               числовой ID сводятся к одному значению через алиас, который
               driver-offline-outbox-v2.js пишет синхронно в момент
               подтверждения. identityKey (причина + started_at) — запасной,
               на случай, если алиас ещё не известен (до первой синхронизации,
               пока обе стороны читают одни и те же, ещё не скорректированные
               часы телефона); started_at, который сервер мог уже
               скорректировать при сбитых часах устройства, не должен решать
               в одиночку (координатор, 27.09.2026). */
            (canonicalEventId && existingCanonicalEventId === canonicalEventId)
            || (identityKey && existing.identityKey === identityKey)
        );
        if (sameInstance) {
            /* Тот же самый простой — не перезапускаем отсчёт вообще: ни точку
               отсчёта, ни интервал. Отображение — чистая функция времени
               (now - started_at), у неё нет накопленного состояния, которое
               можно было бы "обновить" или сбить повторным вызовом. Раньше
               именно накопление (baseShiftSeconds/syncedAtMs, пересчитываемые
               при каждом перезапуске) и давало рывки/откаты таймера на каждой
               фоновой сверке фрагмента (координатор, 27.09.2026, v360: "тупит,
               пошёл, снова остановился, откатился назад"). Освежаем только
               неактивные причины — у активной идёт tick() от той же точки
               отсчёта. */
            syncDriverReasonTotals(payload, activeReasonId);
            return;
        }
        clearDriverDowntimeTimer();
        /* Отсчёт — чистая функция ОДНОЙ неподвижной точки: started_at (уже
           скорректированный сервером под часы телефона; для локального
           старта — собственное время нажатия). Никакого "накопленного"
           elapsed и "времени последней синхронизации" не храним — сам факт
           их существования и был источником рывков/откатов: каждый повторный
           вызов (даже безобидный, для той же причины) пересчитывал базу
           заново от чуть другого мгновения. Здесь эта точка вычисляется
           один-единственный раз — при СМЕНЕ простоя, не при каждой сверке. */
        /* shift_total_seconds и reason_totals[reason] от сервера уже ВКЛЮЧАЮТ
           текущий (ещё идущий) отрезок простоя по состоянию на calculated_at
           (driver_shift_downtime_seconds_by_reason считает открытое событие
           до "сейчас"). Вычитая elapsed_seconds того же снимка, получаем
           фиксированную базу "всё, что накопилось ДО этого отрезка" — и
           дальше просто прибавляем к ней тикающие now-started_at секунды. */
        var reportedShiftTotal = Math.max(0, Math.floor(Number(payload.shift_total_seconds) || 0));
        var reportedElapsed = Math.max(0, Math.floor(Number(payload.elapsed_seconds) || 0));
        var startedAtMs = Date.parse(payload.started_at || "");
        if (!Number.isFinite(startedAtMs)) {
            /* Нет started_at (устаревший вызов без этого поля) — считаем, что
               простой начался elapsed_seconds назад от текущего момента,
               чтобы отсчёт сразу продолжился с уже накопленного значения,
               а не с нуля. */
            startedAtMs = Date.now() - reportedElapsed * 1000;
        }
        var activeReasonButtonAtStart = shell.querySelector(
            '[data-driver-downtime-reason-button][data-driver-downtime-reason-id="' + activeReasonId + '"]'
        );
        var reportedReasonTotal = activeReasonButtonAtStart
            ? Math.max(0, Math.floor(Number(activeReasonButtonAtStart.dataset.driverReasonSeconds) || 0))
            : 0;
        var clock = {
            activeReasonId: activeReasonId,
            eventId: eventId,
            identityKey: identityKey,
            startedAtMs: startedAtMs,
            priorShiftSeconds: Math.max(0, reportedShiftTotal - reportedElapsed),
            priorReasonSeconds: Math.max(0, reportedReasonTotal - reportedElapsed)
        };
        window.driverDowntimeClock = clock;
        syncDriverReasonTotals(payload, activeReasonId);
        /* tick() НЕ держит ссылки на downtimeDuration/downtimeReasonButtons из
           замыкания этого вызова: при полной подмене <main data-driver-shell>
           (оболочка отстала от структуры для послойного обновления — см.
           driverMorphShell) bindDriverMobileShell перепривязывается на НОВОМ
           узле, а «тот же простой» (sameInstance выше) намеренно не
           перезапускает интервал — он продолжает жить на window. Раньше это
           означало, что тикающий интервал писал в уже отсоединённые от
           документа узлы СТАРОГО замыкания: видимый текст замирал и менялся
           только со следующей полной подменой, принёсшей свежее серверное
           значение (боевой 27.09.2026, v361: "2:56" → через 20 с сразу
           "3:18"). Каждый тик ищет живые узлы заново — тогда подмена оболочки
           между тиками не имеет значения. */
        function tick() {
            var liveShell = document.querySelector("[data-driver-shell]");
            var liveDuration = liveShell && liveShell.querySelector("[data-driver-active-duration]");
            var liveReasonButtons = liveShell ? liveShell.querySelectorAll("[data-driver-downtime-reason-button]") : [];
            var liveSeconds = Math.max(0, Math.floor((Date.now() - clock.startedAtMs) / 1000));
            if (liveDuration) {
                liveDuration.textContent = formatDriverDowntimeDuration(clock.priorShiftSeconds + liveSeconds);
            }
            Array.prototype.forEach.call(liveReasonButtons, function (button) {
                var reasonId = String(button.dataset.driverDowntimeReasonId || "");
                if (reasonId === activeReasonId) {
                    renderDriverReasonDuration(button, clock.priorReasonSeconds + liveSeconds, true);
                }
            });
        }
        tick();
        window.driverDowntimeTimerId = window.setInterval(tick, 1000);
    }

    function snapshotDriverDowntimeTimer(atMs) {
        var clock = window.driverDowntimeClock;
        if (!clock || !downtimeCard) {
            return Math.max(0, Math.floor(Number(downtimeCard && downtimeCard.dataset.driverShiftDowntimeSeconds) || 0));
        }
        atMs = Number.isFinite(Number(atMs)) ? Number(atMs) : Date.now();
        /* Чистое чтение чистой функции времени — снимок ничего не накапливает
           и не двигает точку отсчёта, поэтому его можно звать сколько угодно
           раз подряд (например, дважды при отправке действия) без риска
           сдвинуть ещё идущий таймер. */
        var liveSeconds = Math.max(0, Math.floor((atMs - clock.startedAtMs) / 1000));
        var shiftTotalSeconds = clock.priorShiftSeconds + liveSeconds;
        var activeReasonButton = shell.querySelector(
            '[data-driver-downtime-reason-button][data-driver-downtime-reason-id="' + clock.activeReasonId + '"]'
        );
        if (activeReasonButton) {
            var reasonTotalSeconds = clock.priorReasonSeconds + liveSeconds;
            activeReasonButton.dataset.driverReasonSeconds = String(reasonTotalSeconds);
            renderDriverReasonDuration(activeReasonButton, reasonTotalSeconds, true);
        }
        downtimeCard.dataset.driverActiveElapsedSeconds = String(liveSeconds);
        downtimeCard.dataset.driverShiftDowntimeSeconds = String(shiftTotalSeconds);
        if (downtimeDuration) {
            downtimeDuration.textContent = formatDriverDowntimeDuration(shiftTotalSeconds);
        }
        return shiftTotalSeconds;
    }

    function driverDowntimeProjectionSnapshot() {
        var reasonTotals = {};
        downtimeReasonButtons.forEach(function (button) {
            var reasonId = String(button.dataset.driverDowntimeReasonId || "");
            if (reasonId) {
                reasonTotals[reasonId] = Math.max(0, Math.floor(Number(button.dataset.driverReasonSeconds) || 0));
            }
        });
        return {
            active_elapsed_seconds: Math.max(0, Math.floor(Number(downtimeCard && downtimeCard.dataset.driverActiveElapsedSeconds) || 0)),
            shift_total_seconds: Math.max(0, Math.floor(Number(downtimeCard && downtimeCard.dataset.driverShiftDowntimeSeconds) || 0)),
            reason_totals: reasonTotals
        };
    }

    function setDriverDowntimeStatusClass(statusKey) {
        if (!downtimeCard) {
            return;
        }
        ["status-gray", "status-yellow", "status-green", "status-blue", "status-orange", "status-red"].forEach(function (className) {
            downtimeCard.classList.remove(className);
        });
        downtimeCard.classList.add("status-" + (statusKey || "yellow"));
    }

    function applyDriverWaitingMode(payload) {
        var flow = String((payload && payload.workflow) || "");
        var isLoadingWait = flow === "waiting_loading";
        var isUnloadingWait = flow === "waiting_unload";
        var isWaiting = isLoadingWait || isUnloadingWait;
        if (holdForm) {
            /* Разгрузка всегда удержанием со шкалой — и в ожидании разгрузки тоже:
               одно касание срабатывало только при отпускании, без заполнения круга. */
            holdForm.dataset.driverUnloadOneTap = "false";
        }
        if (workDial) {
            workDial.classList.toggle("is-waiting-operation", isWaiting);
            workDial.classList.toggle("is-waiting-loading", isLoadingWait);
            workDial.classList.toggle("is-waiting-unload", isUnloadingWait);
        }
        if (workDialControl) {
            workDialControl.classList.toggle("is-waiting-operation", isWaiting);
            workDialControl.classList.toggle("is-waiting-loading", isLoadingWait);
            workDialControl.classList.toggle("is-waiting-unload", isUnloadingWait);
        }
        var note = workDialControl ? workDialControl.querySelector(".driver-work-note") : null;
        if (note) {
            note.textContent = isWaiting
                ? String(payload.reason_label || payload.reason || "Ожидание").toLocaleUpperCase("ru-RU")
                : (holdForm ? "ТОЧКА РАЗГРУЗКИ" : "НА ЗАГРУЗКУ");
        }
        return isWaiting;
    }

    function applyDriverActiveDowntime(payload) {
        if (!downtimeCard || !payload || !payload.event_id) {
            return false;
        }
        if (driverDowntimeInstanceIsClosed(payload.reason_id, payload.started_at, payload.event_id)) {
            /* Этот телефон уже закрыл именно этот простой (та же причина и
               started_at) — источник, вызвавший этот payload (устаревший
               локальный список офлайн-событий или запоздавший серверный
               снимок), просто ещё не узнал об этом. Не воскрешаем. */
            if (downtimeCard.classList.contains("is-active") || downtimeCard.dataset.driverActiveReasonId) {
                /* Карточку уже успели вернуть в активное состояние НАПРЯМУЮ,
                   в обход этой функции — driver-shift-refresh-v1.js
                   (ownDowntimeOnly) копирует весь набор data-driver-active-*
                   атрибутов и весь class карточки прямо с фрагмента, не
                   спрашивая applyDriverActiveDowntime/clearDriverActiveDowntime.
                   Если во фрагменте оказался более старый (запоздавший)
                   снимок — окантовка/мигание успевали вернуться ДО того, как
                   эта проверка вообще срабатывала. Раз воскрешать нельзя —
                   приводим карточку обратно к неактивной, а не просто
                   отказываемся её трогать (боевой 27.09.2026, v359). */
                clearDriverActiveDowntime({shift_total_seconds: downtimeCard.dataset.driverShiftDowntimeSeconds});
            }
            return false;
        }
        downtimeCard.dataset.driverActiveDowntimeId = payload.event_id;
        downtimeCard.dataset.driverActiveReasonId = String(payload.reason_id || "");
        downtimeCard.dataset.driverActiveDowntimeFlow = payload.workflow || "";
        downtimeCard.dataset.driverActiveStartedAt = payload.started_at || "";
        downtimeCard.dataset.driverActiveElapsedSeconds = String(payload.elapsed_seconds || 0);
        downtimeCard.dataset.driverShiftDowntimeSeconds = String(payload.shift_total_seconds || 0);
        downtimeCard.dataset.driverDowntimeCalculatedAt = payload.calculated_at || "";
        downtimeCard.classList.add("is-active");
        window.driverDowntimeActiveEventId = String(payload.event_id || "");
        setDriverDowntimeStatusClass(payload.status_key || "red");
        if (downtimeTitle) downtimeTitle.textContent = "Активный простой";
        if (downtimeReason) downtimeReason.textContent = payload.reason || "";
        startDriverDowntimeTimer(payload);
        if (downtimeClose) {
            downtimeClose.disabled = false;
            downtimeClose.classList.remove("is-disabled");
            downtimeClose.removeAttribute("aria-disabled");
        }
        return applyDriverWaitingMode(payload);
    }

    function clearDriverActiveDowntime(payload) {
        if (downtimeCard && downtimeCard.dataset.driverActiveReasonId) {
            markDriverDowntimeInstanceClosed(
                downtimeCard.dataset.driverActiveReasonId,
                downtimeCard.dataset.driverActiveStartedAt,
                downtimeCard.dataset.driverActiveDowntimeId
            );
        }
        clearDriverDowntimeTimer();
        window.driverDowntimeActiveEventId = "";
        if (downtimeTitle) downtimeTitle.textContent = "Простоя нет";
        if (downtimeReason) downtimeReason.textContent = "Выберите причину для начала";
        var shiftTotalSeconds = Math.max(0, Number(payload && payload.shift_total_seconds) || Number(downtimeCard && downtimeCard.dataset.driverShiftDowntimeSeconds) || 0);
        if (downtimeDuration) downtimeDuration.textContent = (payload && payload.shift_total_label) || formatDriverDowntimeDuration(shiftTotalSeconds);
        if (downtimeCard) {
            downtimeCard.dataset.driverActiveDowntimeId = "";
            downtimeCard.dataset.driverActiveReasonId = "";
            downtimeCard.dataset.driverActiveDowntimeFlow = "";
            downtimeCard.dataset.driverActiveStartedAt = "";
            downtimeCard.dataset.driverActiveElapsedSeconds = String((payload && payload.elapsed_seconds) || 0);
            downtimeCard.dataset.driverShiftDowntimeSeconds = String(shiftTotalSeconds);
            downtimeCard.dataset.driverDowntimeCalculatedAt = String((payload && payload.calculated_at) || downtimeCard.dataset.driverDowntimeCalculatedAt || "");
            downtimeCard.classList.remove("is-active");
            setDriverDowntimeStatusClass("yellow");
        }
        syncDriverReasonTotals(Object.assign({}, payload || {}, { active: false }));
        if (downtimeClose) {
            downtimeClose.disabled = true;
            downtimeClose.classList.add("is-disabled");
            downtimeClose.setAttribute("aria-disabled", "true");
        }
        downtimeReasonButtons.forEach(function (button) {
            button.classList.remove("is-selected");
        });
        applyDriverWaitingMode({ workflow: "" });
    }

    function postDriverDowntimeAction(payload) {
        payload = payload || {};
        var isClose = payload.action === "close";
        // Простой, который закрывает погрузка/разгрузка, — временем самого рейса.
        var occurredAt = payload.occurred_at ? String(payload.occurred_at) : new Date().toISOString();
        snapshotDriverDowntimeTimer(Date.parse(occurredAt));
        var projectionSnapshot = driverDowntimeProjectionSnapshot();
        if (!isClose) {
            projectionSnapshot.active_elapsed_seconds = 0;
        }
        var context = driverOfflineContext();
        if (!context.shiftId || !context.equipmentId) {
            return Promise.reject(new Error("Нет подтверждённой смены или самосвала для сохранения простоя."));
        }
        if (!isClose && Number(payload.reason_id) <= 0) {
            return Promise.reject(new Error("Не выбрана причина простоя."));
        }
        return driverOfflineOutbox.pending().then(function (events) {
            /* Смена, открытая на телефоне без связи, пока без серверного ID: её
               события несут local_shift_id, а после подтверждения новые — уже
               серверный ID. Простой той же смены — любой из двух ключей. */
            var localShiftState = window.DriverLocalShift && typeof window.DriverLocalShift.state === "function"
                ? window.DriverLocalShift.state(shell)
                : null;
            var shiftKeys = [String(context.shiftId || "")];
            if (localShiftState && localShiftState.local_shift_id) shiftKeys.push(String(localShiftState.local_shift_id));
            if (localShiftState && localShiftState.server_shift_id) shiftKeys.push(String(localShiftState.server_shift_id));
            function isSameDowntimeContext(event) {
                return shiftKeys.indexOf(String(event && (event.shift_id || event.local_shift_id) || "")) >= 0
                    && Number(event && event.equipment_id) === Number(context.equipmentId);
            }
            var latestPendingDowntime = events.slice().reverse().find(function (event) {
                return (event.event_type === "driver.downtime.started" || event.event_type === "driver.downtime.ended")
                    && event.state === "pending"
                    && isSameDowntimeContext(event);
            });
            var unresolvedStart = events.slice().reverse().find(function (event) {
                return event.event_type === "driver.downtime.started"
                    && event.state === "pending"
                    && isSameDowntimeContext(event);
            });
            var reasonButton = !isClose
                ? shell.querySelector('[data-driver-downtime-reason-button][data-driver-downtime-reason-id="' + String(payload.reason_id || "") + '"]')
                : null;
            var activeDowntimeId = String(downtimeCard && downtimeCard.dataset.driverActiveDowntimeId || "");
            var localStartId = unresolvedStart ? unresolvedStart.event_id
                : (activeDowntimeId.indexOf("local:") === 0 ? activeDowntimeId.slice(6) : "");
            var mappingPromise = isClose && localStartId && !unresolvedStart
                ? driverOfflineOutbox.getServerMapping(localStartId)
                : Promise.resolve(null);
            return mappingPromise.then(function (mapping) {
                var mappedServerId = Number(mapping && (mapping.downtime_event_id || mapping.downtime_id)) || null;
                var directServerId = activeDowntimeId.indexOf("local:") === 0 ? null : Number(activeDowntimeId) || null;
                /* Раньше здесь отказывали в закрытии, если начало простоя ещё не
                   подтверждено сервером, — водитель жал «завершить» и получал
                   отказ на нестабильной связи. Телефон решает сам и сразу: событие
                   закрытия ставится в очередь без ссылки, если ссылки ещё нет,
                   сервер связывает его с началом простоя, когда оно синхронизуется
                   (см. _retry('downtime_reference_pending', ...) в offline_sync.py). */
                if (isClose) {
                    if (typeof window.createDriverDowntimeEndEvent !== "function") {
                        throw new Error("offline_runtime_unavailable");
                    }
                    return driverOfflineOutbox.enqueue(window.createDriverDowntimeEndEvent({
                        eventId: payload.client_action_id,
                        occurredAt: occurredAt,
                        pendingStartId: unresolvedStart ? unresolvedStart.event_id : null,
                        serverId: mappedServerId || directServerId,
                        contextSnapshot: {downtime_projection: projectionSnapshot}
                    }));
                }
                return driverOfflineOutbox.enqueue({
                    event_id: payload.client_action_id,
                    event_type: "driver.downtime.started",
                    occurred_at: occurredAt,
                    depends_on: latestPendingDowntime ? [latestPendingDowntime.event_id] : [],
                    context_snapshot: {downtime_projection: projectionSnapshot},
                    payload: {reason_id: Number(payload.reason_id)}
                });
            }).then(function (event) {
                driverOfflineOutbox.flush().catch(function () {});
                if (isClose) {
                    return {
                        ok: true,
                        active: false,
                        closed: true,
                        elapsed_seconds: downtimeCard && downtimeCard.dataset.driverActiveElapsedSeconds || 0,
                        shift_total_seconds: downtimeCard && downtimeCard.dataset.driverShiftDowntimeSeconds || 0
                    };
                }
                return {
                    ok: true,
                    active: true,
                    event_id: "local:" + event.event_id,
                    reason_id: payload.reason_id,
                    reason: reasonButton && reasonButton.dataset.driverReasonLabel || "Простой",
                    reason_label: reasonButton && reasonButton.dataset.driverReasonLabel || "Простой",
                    workflow: reasonButton && reasonButton.dataset.driverDowntimeFlow || "",
                    status_key: reasonButton && reasonButton.dataset.driverStatusKey || "yellow",
                    started_at: occurredAt,
                    elapsed_seconds: 0,
                    shift_total_seconds: downtimeCard && downtimeCard.dataset.driverShiftDowntimeSeconds || 0,
                    calculated_at: occurredAt
                };
            });
        });
    }

    /* Простой гаснет рейсом сам — на телефоне, без сервера (бой 02.10.2026,
       Infinix без сети): «ожидание погрузки» шло после ручной погрузки и даже
       после разгрузки, снять можно было только рукой; с сетью его гасил сервер.
       Погрузка закрывает любой открытый простой (задание v376, п. 4), разгрузка —
       только ожидание разгрузки. Закрытие ставится в очередь РАНЬШЕ события
       рейса и тем же временем, так что серверу закрывать уже нечего. */
    window.driverCloseDowntimeForTrip = function (kind, occurredAt) {
        var activeId = String(downtimeCard && downtimeCard.dataset.driverActiveDowntimeId || "");
        var flow = String(downtimeCard && downtimeCard.dataset.driverActiveDowntimeFlow || "");
        if (!activeId) return Promise.resolve(null);
        if (kind === "unload" && flow !== "waiting_unload") return Promise.resolve(null);
        var startedAt = Date.parse(downtimeCard.dataset.driverActiveStartedAt || "");
        var at = String(occurredAt || new Date().toISOString());
        // Простой, начатый уже после этой отметки, рейс не закрывает.
        if (Number.isFinite(startedAt) && startedAt > Date.parse(at)) return Promise.resolve(null);
        return postDriverDowntimeAction({
            action: "close",
            client_action_id: generateClientActionId("driver-downtime-close"),
            occurred_at: at
        }).then(function (payload) {
            clearDriverActiveDowntime(payload);
            return payload;
        }).catch(function () {
            // Рейс важнее: не записали закрытие — погрузку это не держит.
            return null;
        });
    };

    function registerDriverDowntimeAction(button, onComplete) {
        if (!button || typeof onComplete !== "function") return;
        var pending = false;
        button.addEventListener("click", function (event) {
            event.preventDefault();
            // Правила «гружёный/пустой/точка» решает телефон по своему рейсу
            // (driver-downtime-drum-v1.js); серверная разметка — только без них.
            var localRefusal = typeof window.driverDowntimeLocalRefusal === "function"
                ? window.driverDowntimeLocalRefusal(button)
                : null;
            if (localRefusal !== null ? localRefusal : button.getAttribute("aria-disabled") === "true") {
                showDriverToast(localRefusal || button.dataset.driverUnavailableMessage || "Действие недоступно");
                return;
            }
            if (
                button.disabled
                || pending
                || driverRoleIsReadonly()
            ) return;
            pending = true;
            button.classList.add("is-pending");
            var actionResult;
            try {
                actionResult = onComplete();
            } catch (error) {
                actionResult = Promise.reject(error);
            }
            Promise.resolve(actionResult).catch(function (error) {
                showDriverToast(error && error.message ? error.message : "Действие не выполнено");
            }).finally(function () {
                pending = false;
                button.classList.remove("is-pending");
            });
        });
    }

    var downtimeRefresh = shell.querySelector("[data-driver-downtime-refresh]");
    if (downtimeRefresh) {
        downtimeRefresh.addEventListener("click", function () { window.location.reload(); });
    }

    downtimeReasonButtons.forEach(function (button) {
        registerDriverDowntimeAction(button, function () {
            if (button.disabled) return;
            if (
                downtimeCard
                && downtimeCard.dataset.driverActiveDowntimeId
                && String(downtimeCard.dataset.driverActiveReasonId || "") === String(button.dataset.driverDowntimeReasonId || "")
            ) {
                return;
            }
            downtimeReasonButtons.forEach(function (item) {
                item.classList.remove("is-selected");
            });
            button.classList.add("is-selected");
            button.disabled = true;
            return postDriverDowntimeAction({
                action: "start",
                reason_id: button.dataset.driverDowntimeReasonId,
                client_action_id: generateClientActionId("driver-downtime")
            }).then(function (payload) {
                playDriverVoice("action_ok", "voice_downtime_started");
                if (applyDriverActiveDowntime(payload)) {
                    openDriverTab("work");
                }
                if (window.AppRealtime && typeof window.AppRealtime.wake === "function") {
                    window.AppRealtime.wake("driver_downtime_saved");
                }
            }).catch(function (error) {
                playDriverVoice("action_error", "voice_action_failed");
                showDriverToast(error.message || "Простой не сохранен");
                button.classList.remove("is-selected");
            }).finally(function () {
                button.disabled = false;
            });
        });
    });

    if (downtimeClose) {
        registerDriverDowntimeAction(downtimeClose, function () {
            if (downtimeClose.disabled || downtimeClose.getAttribute("aria-disabled") === "true") {
                return;
            }
            if (!downtimeCard || !downtimeCard.dataset.driverActiveDowntimeId) {
                clearDriverActiveDowntime();
                return;
            }
            downtimeClose.disabled = true;
            downtimeClose.classList.add("is-pending");
            return postDriverDowntimeAction({
                action: "close",
                client_action_id: generateClientActionId("driver-downtime-close")
            }).then(function (payload) {
                playDriverVoice("action_ok", "voice_downtime_finished");
                clearDriverActiveDowntime(payload);
                if (window.AppRealtime && typeof window.AppRealtime.wake === "function") {
                    window.AppRealtime.wake("driver_downtime_closed");
                }
            }).catch(function (error) {
                playDriverVoice("action_error", "voice_action_failed");
                showDriverToast(error.message || "Простой не завершен");
                downtimeClose.disabled = false;
                downtimeClose.classList.remove("is-disabled");
                downtimeClose.removeAttribute("aria-disabled");
            }).finally(function () {
                downtimeClose.classList.remove("is-pending");
            });
        });
    }

    if (downtimeCard && downtimeCard.dataset.driverActiveDowntimeId && downtimeCard.dataset.driverActiveStartedAt) {
        window.driverDowntimeActiveEventId = downtimeCard.dataset.driverActiveDowntimeId;
        startDriverDowntimeTimer({
            active: true,
            event_id: downtimeCard.dataset.driverActiveDowntimeId,
            reason_id: downtimeCard.dataset.driverActiveReasonId,
            started_at: downtimeCard.dataset.driverActiveStartedAt,
            elapsed_seconds: downtimeCard.dataset.driverActiveElapsedSeconds,
            shift_total_seconds: downtimeCard.dataset.driverShiftDowntimeSeconds,
            calculated_at: downtimeCard.dataset.driverDowntimeCalculatedAt
        });
    } else if (downtimeDuration) {
        downtimeDuration.textContent = formatDriverDowntimeDuration(downtimeCard && downtimeCard.dataset.driverShiftDowntimeSeconds);
        syncDriverReasonTotals({ active: false });
        clearDriverDowntimeTimer();
    }

    var unloadHoldGuard = null;
    var unloadSubmissionPending = false;
    var unloadRecoveryStorage = null;
    try {
        unloadRecoveryStorage = window.sessionStorage;
    } catch (error) {}
    var unloadRecovery = window.createDriverUnloadRecovery({
        storage: unloadRecoveryStorage,
        tripId: holdForm ? holdForm.dataset.driverTripId : "",
        input: holdForm ? holdForm.querySelector("[data-driver-client-action]") : null,
        generateActionId: function () {
            return generateClientActionId("trip-unloaded");
        },
        onRecover: function () {
            unloadSubmissionPending = false;
            if (holdForm) {
                delete holdForm.dataset.driverUnloadSubmitting;
            }
            if (unloadHoldGuard) {
                unloadHoldGuard.cancel();
            }
            if (holdButton && !driverRoleIsReadonly()) {
                holdButton.disabled = false;
            }
        }
    });
    window.driverUnloadRecovery = unloadRecovery;

    if (holdForm && holdButton) {
        var dialLabel = holdButton.querySelector("[data-driver-dial-label]");
        var readyDialLabel = dialLabel
            ? (dialLabel.dataset.driverDialRaw || dialLabel.textContent.trim().replace(/\s+/g, " "))
            : "";
        function submitDriverUnloadOnce() {
            /* Ручной рейс на круге (барабан точек, driver-point-drum-v1.js): удержание или
               одно касание завершает его через очередь ручного режима; круг гаснет сам,
               когда движок ручного рейса сообщит о завершении. */
            if (holdButton.dataset.driverManualDial === "true") {
                if (holdButton.disabled || driverRoleIsReadonly() || !window.DriverPointDrum) return false;
                var started = window.DriverPointDrum.completeFromDial(showDriverDialConfirmed);
                /* Возвращаем true — иначе кольцо сбросилось бы и заглушило длинный
                   виброотклик завершения. Подпись круга не трогаем: промежуточной
                   «ОТПРАВКИ» больше нет (владелец, 30.09.2026) — телефон решает сам и
                   сразу, круг идёт от удержания прямо к «РАЗГРУЖЕНО» и следующему
                   экскаватору; is-pending остаётся только замком от повторного нажатия. */
                if (started) {
                    holdButton.classList.remove("is-loaded", "is-holding");
                    holdButton.classList.add("is-pending");
                    return true;
                }
                return false;
            }
            if (
                unloadSubmissionPending
                || holdForm.dataset.driverUnloadSubmitting === "true"
                || holdButton.disabled
                || driverRoleIsReadonly()
            ) {
                return false;
            }
            if (!unloadRecovery.ensureActionId()) {
                showDriverToast("Не удалось подготовить разгрузку. Повторите нажатие");
                return false;
            }
            unloadSubmissionPending = true;
            holdForm.dataset.driverUnloadSubmitting = "true";
            holdForm.dataset.holdComplete = "true";
            holdButton.classList.remove("is-loaded", "is-holding");
            // Без «ОТПРАВКИ» на круге (владелец, 30.09.2026): подпись сменит проекция.
            holdButton.classList.add("is-pending");
            holdButton.disabled = true;
            var actionId = holdForm.querySelector("[data-driver-client-action]").value;
            var unloadTripId = String(holdForm.dataset.driverTripId || "");
            var unloadContext = driverOfflineContext();
            if (!unloadTripId || shell.dataset.driverHasLoadedTrip !== "true" || !unloadContext.shiftId || !unloadContext.equipmentId) {
                unloadRecovery.recover({type: "local_guard_failed"});
                holdButton.classList.remove("is-pending");
                showDriverToast("Нет подтверждённого загруженного рейса для разгрузки.");
                return false;
            }
            // Ожидание разгрузки гасит сама разгрузка — тем же временем и раньше в очереди.
            var unloadAt = new Date().toISOString();
            var closeWait = typeof window.driverCloseDowntimeForTrip === "function"
                ? window.driverCloseDowntimeForTrip("unload", unloadAt)
                : Promise.resolve(null);
            closeWait.then(function () {
                return driverOfflineOutbox.pending();
            }).then(function (events) {
                var pendingPoint = events.slice().reverse().find(function (event) {
                    return event.event_type === "driver.trip.dump_point_changed"
                        && String(event.trip_id || "") === unloadTripId;
                });
                return driverOfflineOutbox.enqueue({
                    event_id: actionId,
                    event_type: "driver.trip.unloaded",
                    occurred_at: unloadAt,
                    trip_id: unloadTripId,
                    depends_on: pendingPoint ? [pendingPoint.event_id] : [],
                    payload: {trip_id: Number(unloadTripId)}
                });
            }).then(function (savedEvent) {
                unloadRecovery.recover({type: "queued"});
                /* Разгрузка записана на телефоне — это и есть факт (телефон решает
                   сам). Круг сначала показывает «засчитано», и только потом
                   проекция переводит его в следующее состояние: проекция ниже
                   выждет конец показа (см. showDriverDialConfirmed). */
                showDriverDialConfirmed();
                applyDriverOfflineProjection(shell, driverOfflineEvents);
                showDriverToast("Разгрузка сохранена на телефоне.");
                /* Delivery is best-effort. A flush error must never turn a successful
                   durable enqueue into a false "save failed" message or restore the trip. */
                try {
                    var flushPromise = driverOfflineOutbox.flush();
                    if (flushPromise && typeof flushPromise.catch === "function") {
                        flushPromise.catch(function () {});
                    }
                } catch (flushError) {}
            }).catch(function () {
                unloadRecovery.recover({type: "storage_failed"});
                holdButton.classList.remove("is-pending");
                showDriverToast("Не удалось сохранить разгрузку на телефоне. Повторите действие.");
            });
            return true;
        }
        /* Кольцо удержания набирается секциями между делениями (12 штук за holdMs).
           Каждая секция — короткий отклик, заполненное кольцо — длинный. Отклики идут
           по таймеру, а не по кадрам: ни одного лишнего пересчёта во время удержания.
           Если в системных настройках телефона выключен виброотклик при касании,
           Android глушит эти вызовы (в dumpsys они видны со scale 0). */
        var HOLD_SEGMENTS = 12;
        var holdSegmentTimer = null;
        function driverVibrate(pattern) {
            /* Через общий уровень виброотклика (driver-haptics-v1.js): водитель
               выбирает силу на вкладке «Смена», длительности масштабируются там. */
            if (typeof window.driverHaptic === "function") { window.driverHaptic(pattern); return; }
            if (!window.navigator || typeof window.navigator.vibrate !== "function") return;
            try { window.navigator.vibrate(pattern); } catch (error) {}
        }
        function stopHoldSegmentFeedback() {
            if (holdSegmentTimer !== null) {
                window.clearInterval(holdSegmentTimer);
                holdSegmentTimer = null;
            }
        }
        function startHoldSegmentFeedback(totalMs) {
            stopHoldSegmentFeedback();
            var fired = 0;
            holdSegmentTimer = window.setInterval(function () {
                fired += 1;
                // Последнюю границу не отбиваем: там срабатывает длинный отклик завершения.
                if (fired >= HOLD_SEGMENTS) { stopHoldSegmentFeedback(); return; }
                driverVibrate(24);
            }, totalMs / HOLD_SEGMENTS);
        }
        /* «Засчитано»: как только разгрузка (или завершение ручного рейса) записана
           на телефоне, поверх круга ~5 с лежит отдельный слой — полное зелёное
           кольцо со вспышкой, галочка, которая прорисовывается, «РАЗГРУЖЕНО» и
           мягкий ореол; звучит голос. Под слоем экран живёт как обычно: проекция,
           очередь, опрос и подмена фрагмента ничего не ждут, и к концу показа
           следующее состояние («ЭКС-1 / НА ЗАГРУЗКУ») уже готово — слой просто
           гаснет. Касание по экрану закрывает показ досрочно. Раньше проекция
           перестраивала круг в тот же миг, и водитель, у которого во время
           удержания экран закрыт рукой, не понимал, засчитался ли рейс
           (владелец, 28.09.2026).
           Слой лежит на body, а не внутри круга: сверка после разгрузки приходит
           через 1–2 с и заменяет или послойно перестраивает оболочку целиком —
           слой внутри неё исчез бы посреди показа. Геометрию берём с самой
           кнопки круга при показе; сама кнопка и сердцевина не меняются ни на
           пиксель. Голос звучит здесь, по факту записи на телефоне, а не по
           ответу сервера: ответ мог прийти через секунды или минуты, когда
           водитель уже уехал. */
        var DRIVER_DIAL_CONFIRM_MS = 5000;
        var DRIVER_DIAL_CONFIRM_FADE_MS = 450;
        var driverDialConfirmTimer = null;
        var driverDialConfirmFadeTimer = null;
        function driverDialConfirmLayer() {
            var layer = document.querySelector("[data-driver-work-confirm]");
            if (layer) return layer;
            layer = document.createElement("div");
            layer.className = "driver-work-confirm";
            layer.setAttribute("data-driver-work-confirm", "");
            layer.setAttribute("aria-hidden", "true");
            /* Только сердцевина: ни кольца удержания, ни свечения снаружи неё
               (владелец, 28.09.2026) — кольцо после срабатывания само возвращается
               в исходный вид. */
            layer.innerHTML = ''
                + '<span class="driver-work-confirm-halo"></span>'
                + '<svg class="driver-work-confirm-check" viewBox="0 0 100 100" aria-hidden="true">'
                +   '<path d="M26 53 L44 70 L75 34" pathLength="100"></path>'
                + '</svg>'
                + '<b class="driver-work-confirm-text">РАЗГРУЖЕНО</b>';
            document.body.appendChild(layer);
            return layer;
        }
        function placeDriverDialConfirmLayer() {
            var layer = document.querySelector("[data-driver-work-confirm]");
            var button = document.querySelector("[data-driver-hold-button]");
            var core = button && button.querySelector(".driver-work-dial-core");
            if (!layer || !core || !layer.classList.contains("is-showing")) return;
            var rect = core.getBoundingClientRect();
            if (!(rect.width > 0)) return;
            layer.style.left = rect.left.toFixed(1) + "px";
            layer.style.top = rect.top.toFixed(1) + "px";
            layer.style.width = rect.width.toFixed(1) + "px";
            layer.style.height = rect.height.toFixed(1) + "px";
        }
        function hideDriverDialConfirmed(fade) {
            var layer = document.querySelector("[data-driver-work-confirm]");
            window.clearTimeout(driverDialConfirmTimer);
            window.clearTimeout(driverDialConfirmFadeTimer);
            driverDialConfirmTimer = null;
            driverDialConfirmFadeTimer = null;
            document.removeEventListener("pointerdown", dismissDriverDialConfirmed, true);
            window.removeEventListener("resize", placeDriverDialConfirmLayer);
            if (!layer || !layer.classList.contains("is-showing")) return;
            if (!fade) {
                layer.classList.remove("is-showing", "is-leaving");
                return;
            }
            layer.classList.add("is-leaving");
            driverDialConfirmFadeTimer = window.setTimeout(function () {
                driverDialConfirmFadeTimer = null;
                layer.classList.remove("is-showing", "is-leaving");
            }, DRIVER_DIAL_CONFIRM_FADE_MS);
        }
        // Слой не ловит касаний (pointer-events: none): касание доходит до экрана как обычно.
        function dismissDriverDialConfirmed() { hideDriverDialConfirmed(true); }
        function showDriverDialConfirmed() {
            var layer = driverDialConfirmLayer();
            hideDriverDialConfirmed(false);
            // Повторный показ подряд: сброс классов и принудительная раскладка перезапускают анимации.
            void layer.offsetWidth;
            layer.classList.add("is-showing");
            placeDriverDialConfirmLayer();
            window.driverDialConfirmShownAt = Date.now();
            playDriverVoice("action_ok", "voice_trip_finished");
            document.addEventListener("pointerdown", dismissDriverDialConfirmed, true);
            window.addEventListener("resize", placeDriverDialConfirmLayer);
            driverDialConfirmTimer = window.setTimeout(function () {
                driverDialConfirmTimer = null;
                hideDriverDialConfirmed(true);
            }, DRIVER_DIAL_CONFIRM_MS - DRIVER_DIAL_CONFIRM_FADE_MS);
        }
        window.showDriverDialConfirmed = showDriverDialConfirmed;
        /* Удержание завершено: сброс удержания после этого (восстановление
           формы, отмена) не должен глушить длинный отклик завершения. */
        var unloadHoldCompleted = false;
        /* Разгрузка по удержанию записана: подписью круга дальше владеет
           проекция (следующий экскаватор), а не подпись с момента привязки
           экрана. Иначе отпускание пальца возвращало «ККД» поверх «ЭКС-1»
           (матрица без сети, 30.09.2026). */
        var unloadHoldSubmitted = false;
        unloadHoldGuard = window.createDriverRoleHoldGuard({
            /* Разгрузка повторяется десятки раз за смену: полсекунды — достаточно,
               чтобы случайное касание не отправило рейс, и не утомляет за смену.
               Кольцо в CSS (driver-shift-v1.css, driver-hold-right/left) набирается
               ровно за это же время: две половины по 250 мс. */
            holdMs: 500,
            onStart: function () {
                unloadHoldCompleted = false;
                unloadHoldSubmitted = false;
                holdButton.classList.add("is-holding");
                startHoldSegmentFeedback(500);
            },
            onReset: function () {
                stopHoldSegmentFeedback();
                if (!unloadHoldCompleted) driverVibrate(0);
                delete holdForm.dataset.holdComplete;
                holdButton.classList.remove("is-holding", "is-pending");
                // Ручной рейс мог завершиться до отпускания пальца — пустой круг не «загружаем».
                if (!holdButton.disabled) holdButton.classList.add("is-loaded");
                // После обычного отпускания подпись и так исходная — подгонка текста
                // (замеры ширины в цикле) на слабом телефоне стоила заметного кадра.
                var resetLabel = holdButton.dataset.driverManualDialLabel || readyDialLabel;
                if (!unloadHoldSubmitted && dialLabel && resetLabel && (dialLabel.dataset.driverDialRaw || dialLabel.textContent.trim().replace(/\s+/g, " ")) !== resetLabel) {
                    renderDriverDialLabel(dialLabel, resetLabel);
                    scheduleDriverDialLabelFit();
                }
            },
            onComplete: function () {
                stopHoldSegmentFeedback();
                unloadHoldCompleted = true;
                driverVibrate(160);   // кольцо заполнено
                if (!submitDriverUnloadOnce()) {
                    unloadHoldGuard.cancel();
                    return;
                }
                unloadHoldSubmitted = true;
            }
        });
        window.driverUnloadHoldGuard = unloadHoldGuard;
        window.driverUnloadGesture = window.bindDriverUnloadGesture({
            form: holdForm,
            button: holdButton,
            holdGuard: unloadHoldGuard,
            canTrigger: function () {
                return !unloadSubmissionPending && !driverRoleIsReadonly();
            },
            onOneTap: function () {
                return submitDriverUnloadOnce();
            }
        });
    }

    var pointSheet = shell.querySelector("[data-driver-point-sheet]");
    var pointOpen = shell.querySelector("[data-driver-point-open]");
    var pointSheetReturnFocus = null;
    function setPointSheet(open) {
        if (!pointSheet) {
            return;
        }
        pointSheet.hidden = !open;
        shell.classList.toggle("is-point-sheet-open", open);
        if (pointOpen) pointOpen.setAttribute("aria-expanded", open ? "true" : "false");
        if (open) {
            pointSheetReturnFocus = document.activeElement;
            window.requestAnimationFrame(function () {
                var focusTarget = pointSheet.querySelector(".driver-unload-tile.is-current, [data-driver-point-close]");
                if (focusTarget) focusTarget.focus();
            });
        } else if (pointSheetReturnFocus && typeof pointSheetReturnFocus.focus === "function") {
            pointSheetReturnFocus.focus();
            pointSheetReturnFocus = null;
        }
    }
    if (pointOpen && pointSheet) {
        pointOpen.addEventListener("click", function () {
            setPointSheet(true);
        });
        pointOpen.addEventListener("keydown", function (event) {
            if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                setPointSheet(true);
            }
        });
        pointSheet.querySelectorAll("[data-driver-point-close]").forEach(function (button) {
            button.addEventListener("click", function () {
                setPointSheet(false);
            });
        });
        pointSheet.addEventListener("click", function (event) {
            if (event.target === pointSheet) setPointSheet(false);
        });
        pointSheet.addEventListener("keydown", function (event) {
            if (event.key === "Escape") {
                event.preventDefault();
                setPointSheet(false);
            }
        });
        pointSheet.querySelectorAll("form").forEach(function (form) {
            form.dataset.driverOfflineManaged = "true";
            form.addEventListener("submit", function (event) {
                event.preventDefault();
                event.stopImmediatePropagation();
                if (form.dataset.driverOfflinePending === "true") return;
                var action = form.querySelector("[data-driver-client-action]");
                var point = form.querySelector('[name="dump_point"]');
                var button = form.querySelector(".driver-unload-tile");
                var tripId = String(shell.dataset.driverActiveTripId || "");
                var pointId = Number(point && point.value);
                var pointName = button && button.dataset.driverPointName || "";
                if (!tripId || !pointId || shell.dataset.driverHasOpenTrip !== "true") {
                    showDriverToast("Нет подтверждённого рейса или точки разгрузки.");
                    return;
                }
                if (String(pointId) === String(shell.dataset.driverActualDumpPointId || "")) {
                    setPointSheet(false);
                    showDriverToast("Эта точка уже выбрана.");
                    return;
                }
                form.dataset.driverOfflinePending = "true";
                driverOfflineOutbox.pending().then(function (events) {
                    if (typeof window.createDriverPointChangeEvent !== "function") {
                        throw new Error("offline_runtime_unavailable");
                    }
                    var change = window.createDriverPointChangeEvent({
                        tripId: tripId,
                        pointId: pointId,
                        currentPointId: shell.dataset.driverActualDumpPointId,
                        events: events
                    });
                    if (action) action.value = change.event_id;
                    return driverOfflineOutbox.enqueue(change);
                }).then(function () {
                    applyDriverPointSelection(shell, pointId, pointName, "local");
                    setPointSheet(false);
                    showDriverToast("Точка разгрузки сохранена на телефоне.");
                    return driverOfflineOutbox.flush();
                }).catch(function () {
                    showDriverToast("Не удалось сохранить точку на телефоне. Повторите действие.");
                }).finally(function () {
                    form.dataset.driverOfflinePending = "false";
                });
            });
        });
    }

    /* Открытие/закрытие смены, принятие назначения и смена
       точки разгрузки раньше перезагружали весь WebView. Серверные
       POST-обработчики не меняем: забираем их готовый HTML и заменяем
       только рабочий shell. Обычная отправка остаётся аварийным fallback. */
    shell.querySelectorAll("form[data-driver-in-place]").forEach(function (form) {
        if (form.dataset.driverOfflineManaged === "true") return;
        if (form.dataset.driverInPlaceBound === "true") return;
        form.dataset.driverInPlaceBound = "true";
        form.addEventListener("submit", function (event) {
            if (event.defaultPrevented || form.dataset.driverInPlacePending === "true") {
                return;
            }
            event.preventDefault();
            form.dataset.driverInPlacePending = "true";
            var actionInput = form.querySelector("[data-driver-client-action], [name='client_action_id']");
            if (actionInput && !actionInput.value) {
                actionInput.value = generateClientActionId(form.dataset.driverInPlace || "driver-action");
            }
            var localShift = window.DriverLocalShift;
            var shiftKind = form.dataset.driverInPlace;
            /* Смена открывается и закрывается на телефоне сразу, событием очереди;
               сервер догонит, когда появится связь (владелец, 30.09.2026). */
            var submitPromise = localShift && (shiftKind === "shift-open" || shiftKind === "shift-close")
                ? (shiftKind === "shift-open" ? localShift.open(form) : localShift.close(form)).then(function () {
                    var tabButton = document.querySelector(
                        '[data-driver-tab-open="' + (shiftKind === "shift-open" ? "work" : "shift") + '"]'
                    );
                    if (tabButton) tabButton.click();
                    if (typeof playDriverVoice === "function") {
                        if (shiftKind === "shift-open") playDriverVoice("shift_start", "voice_shift_opened");
                        else playDriverVoice("shift_end", "voice_shift_closed");
                    }
                    return true;
                })
                : shiftKind === "shift-close"
                    ? window.DriverShiftCloseOutbox.submit(form)
                    : shiftKind === "assignment"
                        ? window.driverAcceptAssignmentLocally(form)
                        : window.submitDriverFormInPlace(form, {fallbackToNavigation: false});
            Promise.resolve(submitPromise).then(function (applied) {
                if (!applied && form.isConnected) {
                    form.dataset.driverInPlacePending = "false";
                    form.dataset.driverShiftOpeningPending = "false";
                    var openButton = form.querySelector("[data-driver-shift-open-button]");
                    if (openButton) {
                        openButton.disabled = false;
                        openButton.classList.remove("is-pending");
                        openButton.textContent = "Начать смену";
                    }
                    var closeButton = form.querySelector("[data-driver-shift-close-button]");
                    if (closeButton) {
                        closeButton.disabled = false;
                        closeButton.classList.remove("is-pending");
                        var closeLabel = closeButton.querySelector("[data-mobile-shift-label]");
                        if (closeLabel) closeLabel.textContent = "Закрыть смену";
                    }
                }
            }).catch(function (error) {
                if (form.isConnected) {
                    form.dataset.driverInPlacePending = "false";
                    /* Открытие не записалось — форма снова в руках водителя: без
                       этого признак отправки оставался и держал экран (opening_form). */
                    form.dataset.driverShiftOpeningPending = "false";
                    var failedOpenButton = form.querySelector("[data-driver-shift-open-button]");
                    if (failedOpenButton) {
                        failedOpenButton.disabled = false;
                        failedOpenButton.classList.remove("is-pending");
                        var failedOpenLabel = failedOpenButton.querySelector("[data-mobile-shift-label]");
                        if (failedOpenLabel) failedOpenLabel.textContent = "Начать смену";
                    }
                    var closeButton = form.querySelector("[data-driver-shift-close-button]");
                    if (closeButton) {
                        closeButton.disabled = false;
                        closeButton.classList.remove("is-pending");
                        var closeLabel = closeButton.querySelector("[data-mobile-shift-label]");
                        if (closeLabel) closeLabel.textContent = "Закрыть смену";
                    }
                }
                if (typeof window.showDriverToast === "function") {
                    window.showDriverToast(error && error.message ? error.message : "Не удалось сохранить действие.");
                }
            });
        });
    });
    window.DriverShiftCloseOutbox.restore(
        shell.querySelector("[data-driver-shift-close-form]")
    );

    (function initDriverPwaUpdates() {
        if (!("serviceWorker" in navigator)) {
            return;
        }
        var runtime = window.__driverPwaUpdateRuntime;
        var currentShellVersion = runtime && runtime.currentShellVersion
            ? runtime.currentShellVersion
            : shell.dataset.driverPwaVersion || "";
        var updateModal = document.querySelector("[data-driver-pwa-update-modal]");
        var updateBadge = document.querySelector("[data-driver-pwa-update-badge]");
        var updateTarget = document.querySelector("[data-driver-pwa-update-nav-target]");
        var statusNode = document.querySelector("[data-driver-pwa-update-status]");
        var currentVersionNode = document.querySelector("[data-driver-pwa-current-version]");
        var newVersionNode = document.querySelector("[data-driver-pwa-new-version]");
        var applyButton = document.querySelector("[data-driver-pwa-update-apply]");
        var laterButton = document.querySelector("[data-driver-pwa-update-later]");

        function formatVersion(version) {
            var match = String(version || "").match(/driver-mobile-shell-v(\d+)/);
            return match ? "v" + match[1] : String(version || "v1");
        }
        function versionNumber(version) {
            var match = String(version || "").match(/driver-mobile-shell-v(\d+)/);
            return match ? parseInt(match[1], 10) : 0;
        }
        function setBadge(visible) {
            if (updateBadge) {
                updateBadge.hidden = !visible;
            }
            if (updateTarget) {
                updateTarget.classList.toggle("has-update", visible);
            }
        }
        function setStatus(text) {
            if (statusNode) {
                statusNode.textContent = text;
            }
        }
        function showUpdate(nextVersion) {
            setBadge(true);
            if (currentVersionNode) {
                currentVersionNode.textContent = formatVersion(currentShellVersion);
            }
            if (newVersionNode) {
                newVersionNode.textContent = formatVersion(nextVersion);
            }
            setStatus("Можно установить новую версию экрана водителя.");
        }
        function revealModal() {
            if (updateModal) {
                updateModal.hidden = false;
            }
        }
        function hideModal() {
            if (updateModal) {
                updateModal.hidden = true;
            }
        }

        function releaseVerifiedPageFromStaleWorkerLock(detail) {
            var guard = window.AppPwaContractGuard;
            var body = document.body;
            var server = detail && detail.server;
            var worker = detail && detail.serviceWorker;
            var expectedContractVersion = body && String(body.dataset.appContractVersion || "");
            var expectedShellVersion = body && String(body.dataset.appShellVersion || "");
            var expectedRoleCode = body && String(body.dataset.appRoleCode || "");
            var workerMatches = worker
                && worker.appContractVersion === expectedContractVersion
                && worker.shellVersion === expectedShellVersion
                && worker.roleCode === expectedRoleCode;
            if (
                !guard
                || typeof guard.acceptServiceWorkerVersion !== "function"
                || !detail
                || !detail.locked
                || workerMatches
                || !expectedContractVersion
                || expectedShellVersion !== String(currentShellVersion || "")
                || expectedRoleCode !== "driver"
                || detail.javascriptVersion !== expectedContractVersion
                || !server
                || server.appContractVersion !== expectedContractVersion
                || server.shellVersion !== expectedShellVersion
                || server.roleCode !== expectedRoleCode
            ) {
                return false;
            }
            guard.acceptServiceWorkerVersion({
                appContractVersion: expectedContractVersion,
                shellVersion: expectedShellVersion,
                roleCode: expectedRoleCode
            });
            return true;
        }

        if (!runtime) {
            runtime = {
                registration: null,
                registrationPromise: null,
                waitingWorker: null,
                activationRequestedWorker: null,
                currentShellVersion: currentShellVersion,
                renderUpdate: null,
                clearUpdate: null,
                applyPromise: null,
                controllerRecoveryTimer: 0
            };
            runtime.requestWorkerVersion = function (worker) {
                if (!worker || !worker.postMessage || !window.MessageChannel) {
                    return Promise.resolve("");
                }
                return new Promise(function (resolve) {
                    var channel = new MessageChannel();
                    var timeout = window.setTimeout(function () {
                        resolve("");
                    }, 1200);
                    channel.port1.onmessage = function (event) {
                        window.clearTimeout(timeout);
                        resolve(event.data && event.data.version ? event.data.version : "");
                    };
                    worker.postMessage({type: "GET_VERSION"}, [channel.port2]);
                });
            };
            runtime.scheduleControllerRecovery = function (targetVersion, delay) {
                if (!targetVersion || runtime.controllerRecoveryTimer) {
                    return;
                }
                runtime.controllerRecoveryTimer = window.setTimeout(function () {
                    runtime.controllerRecoveryTimer = 0;
                    runtime.requestWorkerVersion(navigator.serviceWorker.controller).then(function (controllerVersion) {
                        if (String(controllerVersion || "") === String(targetVersion || "")) {
                            return;
                        }
                        var guard = window.AppPwaContractGuard;
                        if (
                            guard
                            && typeof guard.hasUnsafeWorkInProgress === "function"
                            && guard.hasUnsafeWorkInProgress()
                        ) {
                            return;
                        }
                        var reloadKey = "driver-pwa-controller-reload:" + String(targetVersion || "");
                        try {
                            if (window.sessionStorage.getItem(reloadKey) === "1") {
                                return;
                            }
                            window.sessionStorage.setItem(reloadKey, "1");
                        } catch (error) {
                            return;
                        }
                        window.location.reload();
                    });
                }, Number(delay) >= 0 ? Number(delay) : 1200);
            };
            runtime.activateMatchingWaitingWorker = function (registration, worker, waitingVersion) {
                if (!worker || String(waitingVersion || "") !== String(runtime.currentShellVersion || "")) {
                    return false;
                }
                if (registration && registration.waiting && registration.waiting !== worker) {
                    return false;
                }
                var guard = window.AppPwaContractGuard;
                var guardState = guard && typeof guard.getState === "function"
                    ? guard.getState()
                    : null;
                if (!guardState || !guardState.locked) {
                    return false;
                }
                if (
                    guard
                    && typeof guard.hasUnsafeWorkInProgress === "function"
                    && guard.hasUnsafeWorkInProgress()
                ) {
                    return false;
                }
                if (runtime.activationRequestedWorker === worker) {
                    return true;
                }
                runtime.activationRequestedWorker = worker;
                runtime.waitingWorker = null;
                setBadge(false);
                hideModal();
                worker.postMessage({type: "SKIP_WAITING"});
                runtime.scheduleControllerRecovery(waitingVersion, 1200);
                return true;
            };
            runtime.renderWaitingUpdate = function (registration, worker) {
                var waitingWorker = worker || (registration && registration.waiting);
                if (!waitingWorker) return Promise.resolve();
                if (
                    (registration && registration.waiting !== waitingWorker)
                    || runtime.activationRequestedWorker === waitingWorker
                ) {
                    return Promise.resolve();
                }
                var activeWorker = (registration && registration.active)
                    || navigator.serviceWorker.controller;
                return Promise.all([
                    runtime.requestWorkerVersion(activeWorker),
                    runtime.requestWorkerVersion(waitingWorker)
                ]).then(function (versions) {
                    if (
                        (registration && registration.waiting !== waitingWorker)
                        || runtime.activationRequestedWorker === waitingWorker
                    ) {
                        return;
                    }
                    if (runtime.activateMatchingWaitingWorker(
                        registration,
                        waitingWorker,
                        versions[1]
                    )) {
                        return;
                    }
                    if (runtime.renderUpdate) {
                        runtime.renderUpdate(versions[1], versions[0]);
                    }
                });
            };
            runtime.watchRegistration = function (registration) {
                if (!registration || registration.__driverPwaRuntimeBound) return;
                registration.__driverPwaRuntimeBound = true;
                if (registration.waiting) {
                    runtime.waitingWorker = registration.waiting;
                    runtime.renderWaitingUpdate(registration, registration.waiting);
                }
                registration.addEventListener("updatefound", function () {
                    var worker = registration.installing;
                    if (!worker || !worker.addEventListener) return;
                    worker.addEventListener("statechange", function () {
                        if (worker.state !== "installed" || !navigator.serviceWorker.controller) return;
                        runtime.waitingWorker = registration.waiting || worker;
                        runtime.renderWaitingUpdate(registration, runtime.waitingWorker);
                    });
                });
            };
            runtime.ensureRegistration = function () {
                if (runtime.registration) return Promise.resolve(runtime.registration);
                if (runtime.registrationPromise) return runtime.registrationPromise;
                var guard = window.AppPwaContractGuard;
                var source = guard && typeof guard.getRegistration === "function"
                    ? guard.getRegistration()
                    : navigator.serviceWorker.getRegistration(shell.dataset.driverSwScope);
                runtime.registrationPromise = Promise.resolve(source).then(function (registration) {
                    runtime.registration = registration || null;
                    runtime.watchRegistration(runtime.registration);
                    return runtime.registration;
                }).catch(function () {
                    return null;
                });
                return runtime.registrationPromise;
            };
            runtime.requestManualUpdate = function () {
                if (runtime.applyPromise) return runtime.applyPromise;
                var guard = window.AppPwaContractGuard;
                var update = guard && typeof guard.requestManualUpdate === "function"
                    ? guard.requestManualUpdate()
                    : runtime.ensureRegistration().then(function (registration) {
                        if (!registration || !registration.update) {
                            return {status: "unavailable", registration: registration || null};
                        }
                        return Promise.resolve(registration.update()).then(function () {
                            return {
                                status: registration.waiting ? "update-ready" : "current",
                                registration: registration
                            };
                        });
                    });
                runtime.applyPromise = Promise.resolve(update).then(function (result) {
                    var registration = result && result.registration
                        ? result.registration
                        : runtime.registration;
                    var worker = registration
                        ? registration.waiting
                        : runtime.waitingWorker;
                    if (worker && runtime.activationRequestedWorker !== worker) {
                        runtime.activationRequestedWorker = worker;
                        runtime.waitingWorker = null;
                        setBadge(false);
                        hideModal();
                        worker.postMessage({type: "SKIP_WAITING"});
                        return result;
                    }
                    if (result && result.status === "unavailable") {
                        setStatus(
                            "Служба обновления недоступна. Закройте и снова откройте приложение."
                        );
                    } else if (result && result.status === "error") {
                        setStatus("Не удалось проверить обновление. Попробуйте еще раз.");
                    } else {
                        setStatus("Установлена актуальная версия.");
                    }
                    return result;
                }).finally(function () {
                    runtime.applyPromise = null;
                });
                return runtime.applyPromise;
            };
            /* Переход на новую версию оболочки (матрица без сети A5, 30.09.2026).
               Раньше метка версии во фрагменте перезагружала страницу через 1,5 с —
               раньше, чем ставился новый service worker: старый отдавал старую
               страницу, и она оставалась под замком «service-worker-mismatch».
               А разовая перезагрузка после смены воркера откладывалась, пока
               страница скрыта или занята, и больше не повторялась — «Позже» и
               сворачивание приложения оставляли его замороженным. Теперь экран
               ждёт, пока base.html (он один ведёт обновление воркера) сообщит о
               новом воркере (app-pwa-contract-state), и перезагружается, только
               когда контроллер уже новый; скрытая страница — при возврате,
               занятая вводом — чуть позже. */
            runtime.followTarget = "";
            runtime.followShellVersion = function (targetVersion) {
                targetVersion = String(targetVersion || "");
                if (!targetVersion || versionNumber(targetVersion) <= versionNumber(runtime.currentShellVersion)) {
                    return false;
                }
                if (versionNumber(targetVersion) > versionNumber(runtime.followTarget)) {
                    runtime.followTarget = targetVersion;
                }
                if (!runtime.followBound) {
                    runtime.followBound = true;
                    document.addEventListener("visibilitychange", function () {
                        if (!document.hidden) runtime.checkFollow();
                    });
                }
                runtime.checkFollow();
                return true;
            };
            runtime.checkFollow = function () {
                var target = runtime.followTarget;
                if (!target) return;
                runtime.requestWorkerVersion(navigator.serviceWorker.controller).then(function (controllerVersion) {
                    if (versionNumber(controllerVersion) >= versionNumber(target)) {
                        runtime.reloadIntoWorker(target);
                        return;
                    }
                    var registration = runtime.registration;
                    var waiting = registration && registration.waiting;
                    if (!waiting) return;
                    runtime.requestWorkerVersion(waiting).then(function (waitingVersion) {
                        if (versionNumber(waitingVersion) >= versionNumber(target) && runtime.activationRequestedWorker !== waiting) {
                            runtime.activationRequestedWorker = waiting;
                            waiting.postMessage({type: "SKIP_WAITING"});
                        }
                    });
                });
            };
            runtime.reloadIntoWorker = function (target) {
                if (document.hidden || runtime.followReloading) return;
                var current = document.querySelector("[data-driver-shell]");
                if (current && typeof isDriverOperationalRefreshUnsafe === "function" && isDriverOperationalRefreshUnsafe(current)) {
                    var reason = String(window.driverRefreshBusyReason || "");
                    // Жёсткие причины — водитель вводит показания: ждём, не теряя ввод.
                    if (/^focus:|^opening_form$|^close_form$/.test(reason)) {
                        window.setTimeout(runtime.checkFollow, 5000);
                        return;
                    }
                }
                var key = "driver-shell-follow-reload:" + target;
                var record = {count: 0, at: 0};
                try { record = JSON.parse(window.sessionStorage.getItem(key) || "null") || record; } catch (error) {}
                if (record.count >= 3 && Date.now() - Number(record.at || 0) < 10 * 60 * 1000) return;
                try {
                    window.sessionStorage.setItem(key, JSON.stringify({count: Number(record.count || 0) + 1, at: Date.now()}));
                } catch (error) {}
                runtime.followReloading = true;
                window.location.reload();
            };
            window.addEventListener("app-pwa-contract-state", function (event) {
                var detail = event && event.detail ? event.detail : {};
                if (releaseVerifiedPageFromStaleWorkerLock(detail)) {
                    return;
                }
                var workerVersion = detail.serviceWorker && detail.serviceWorker.shellVersion;
                if (workerVersion && versionNumber(workerVersion) > versionNumber(runtime.currentShellVersion)) {
                    runtime.followShellVersion(workerVersion);
                }
                var serverVersion = detail.server && detail.server.shellVersion;
                if (
                    serverVersion
                    && versionNumber(serverVersion) > versionNumber(runtime.currentShellVersion)
                ) {
                    if (runtime.renderUpdate) runtime.renderUpdate(serverVersion);
                    runtime.followShellVersion(serverVersion);
                } else if (detail.ready && runtime.clearUpdate) {
                    runtime.clearUpdate();
                    runtime.scheduleControllerRecovery(runtime.currentShellVersion, 150);
                }
            });
            window.__driverPwaUpdateRuntime = runtime;
        }

        runtime.renderUpdate = function (nextVersion, baselineVersion) {
            var next = versionNumber(nextVersion);
            var loaded = versionNumber(currentShellVersion);
            var baseline = versionNumber(baselineVersion || currentShellVersion);
            if (
                next > baseline
                && (!baselineVersion || next >= loaded)
            ) {
                showUpdate(nextVersion);
            }
        };
        runtime.clearUpdate = function () {
            setBadge(false);
        };
        runtime.ensureRegistration().then(function (registration) {
            var guardState = window.AppPwaContractGuard
                && typeof window.AppPwaContractGuard.getState === "function"
                ? window.AppPwaContractGuard.getState()
                : null;
            var serverVersion = guardState && guardState.server
                ? guardState.server.shellVersion
                : "";
            if (versionNumber(serverVersion) > versionNumber(currentShellVersion)) {
                showUpdate(serverVersion);
            } else if (registration && registration.waiting) {
                runtime.waitingWorker = registration.waiting;
                runtime.renderWaitingUpdate(registration, registration.waiting);
            }
            if (applyButton && applyButton.dataset.driverPwaUpdateBound !== "true") {
                applyButton.dataset.driverPwaUpdateBound = "true";
                applyButton.addEventListener("click", function () {
                    setStatus("Проверяем и устанавливаем обновление...");
                    runtime.requestManualUpdate();
                });
            }
        }).catch(function () {
            setBadge(false);
        });

        if (laterButton && laterButton.dataset.driverPwaUpdateBound !== "true") {
            laterButton.dataset.driverPwaUpdateBound = "true";
            laterButton.addEventListener("click", hideModal);
        }
        if (updateTarget && updateTarget.dataset.driverPwaUpdateBound !== "true") {
            updateTarget.dataset.driverPwaUpdateBound = "true";
            updateTarget.addEventListener("click", function () {
                if (updateBadge && !updateBadge.hidden) {
                    revealModal();
                }
            }, true);
        }
    })();
};
document.addEventListener("DOMContentLoaded", window.bindDriverMobileShell);
