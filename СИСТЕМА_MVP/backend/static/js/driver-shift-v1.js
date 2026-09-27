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
    function bindAssignmentCountdown() {
        var button = shell.querySelector("[data-driver-assignment-deadline]");
        var output = button ? button.querySelector("[data-driver-assignment-countdown]") : null;
        if (!button || !output) {
            return;
        }
        var deadline = Date.parse(button.dataset.driverAssignmentDeadline || "");
        if (!Number.isFinite(deadline)) {
            return;
        }
        function renderCountdown() {
            if (!button.isConnected) {
                window.clearInterval(timerId);
                return;
            }
            var remaining = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
            var minutes = Math.floor(remaining / 60);
            var seconds = remaining % 60;
            output.textContent = String(minutes).padStart(2, "0") + ":" + String(seconds).padStart(2, "0");
            if (remaining > 0 || button.dataset.driverDeadlineReached === "true") {
                return;
            }
            button.dataset.driverDeadlineReached = "true";
            button.disabled = true;
            window.clearInterval(timerId);
            if (window.AppRealtime && typeof window.AppRealtime.wake === "function") {
                window.AppRealtime.wake("assignment-deadline");
            } else {
                window.setTimeout(function () {
                    if (button.isConnected) {
                        window.location.reload();
                    }
                }, 1200);
            }
        }
        var timerId = window.setInterval(renderCountdown, 1000);
        renderCountdown();
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

    function applyDriverOfflineProjection(current, events) {
        if (!current) return;
        var ordered = (events || []).slice().sort(function (a, b) { return Number(a.sequence) - Number(b.sequence); });
        var activeTripId = String(current.dataset.driverActiveTripId || "");
        var manualTrip = String(current.dataset.driverActiveTripOrigin || "") === "driver_manual";
        var unload = manualTrip ? null : ordered.find(function (event) {
            return event.event_type === "driver.trip.unloaded" && String(event.trip_id || "") === activeTripId;
        });
        if (unload) {
            var unloadNeedsReview = ["conflict", "auth_required", "invalid"].includes(unload.state);
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
                var nextExcavatorLabel = manualWorkspace
                    ? String(
                        manualWorkspace.dataset.driverManualExcavatorLabel
                        || manualWorkspace.dataset.driverManualPrimaryExcavatorLabel
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

    function driverOfflineBindings() {
        return {
            context: driverOfflineContext,
            onState: renderDriverOfflineState,
            onConfirmed: function () {
                var args = arguments;
                var event = args[0] || {};
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
        var occurredAt = new Date().toISOString();
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
            function isSameDowntimeContext(event) {
                return Number(event && event.shift_id) === Number(context.shiftId)
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

    function registerDriverDowntimeAction(button, onComplete) {
        if (!button || typeof onComplete !== "function") return;
        var pending = false;
        button.addEventListener("click", function (event) {
            event.preventDefault();
            if (button.getAttribute("aria-disabled") === "true") {
                showDriverToast(button.dataset.driverUnavailableMessage || "Действие недоступно");
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
                /* Как у обычного рейса: круг сразу показывает отправку. Возвращаем true —
                   иначе кольцо сбросилось бы и заглушило длинный виброотклик завершения. */
                if (started) {
                    holdButton.classList.remove("is-loaded", "is-holding");
                    holdButton.classList.add("is-pending");
                    if (dialLabel) {
                        renderDriverDialLabel(dialLabel, holdButton.dataset.driverPendingLabel || "ОТПРАВКА");
                        scheduleDriverDialLabelFit();
                    }
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
            holdButton.classList.add("is-pending");
            if (dialLabel) {
                renderDriverDialLabel(
                    dialLabel,
                    holdButton.dataset.driverPendingLabel || "ОТПРАВКА"
                );
                scheduleDriverDialLabelFit();
            }
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
            driverOfflineOutbox.pending().then(function (events) {
                var pendingPoint = events.slice().reverse().find(function (event) {
                    return event.event_type === "driver.trip.dump_point_changed"
                        && String(event.trip_id || "") === unloadTripId;
                });
                return driverOfflineOutbox.enqueue({
                    event_id: actionId,
                    event_type: "driver.trip.unloaded",
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
        unloadHoldGuard = window.createDriverRoleHoldGuard({
            /* Разгрузка повторяется десятки раз за смену: полсекунды — достаточно,
               чтобы случайное касание не отправило рейс, и не утомляет за смену.
               Кольцо в CSS (driver-shift-v1.css, driver-hold-right/left) набирается
               ровно за это же время: две половины по 250 мс. */
            holdMs: 500,
            onStart: function () {
                unloadHoldCompleted = false;
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
                if (dialLabel && resetLabel && (dialLabel.dataset.driverDialRaw || dialLabel.textContent.trim().replace(/\s+/g, " ")) !== resetLabel) {
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
                }
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
            var submitPromise = form.dataset.driverInPlace === "shift-close"
                ? window.DriverShiftCloseOutbox.submit(form)
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
            window.addEventListener("app-pwa-contract-state", function (event) {
                var detail = event && event.detail ? event.detail : {};
                if (releaseVerifiedPageFromStaleWorkerLock(detail)) {
                    return;
                }
                var serverVersion = detail.server && detail.server.shellVersion;
                if (
                    serverVersion
                    && versionNumber(serverVersion) > versionNumber(runtime.currentShellVersion)
                ) {
                    if (runtime.renderUpdate) runtime.renderUpdate(serverVersion);
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
