(function (root) {
    "use strict";

    var currentWorkspace = null;
    var currentController = null;
    var currentTripTimer = null;
    var tripTimerInterval = null;

    function demoResultForTarget(name) {
        return "Демонстрация: выбрана точка «" + String(name || "") + "». Рейс не создан.";
    }

    function formatElapsedTime(totalSeconds) {
        var safeSeconds = Math.max(0, Math.floor(Number(totalSeconds) || 0));
        var hours = Math.floor(safeSeconds / 3600);
        var minutes = Math.floor((safeSeconds % 3600) / 60);
        var seconds = safeSeconds % 60;
        return [hours, minutes, seconds].map(function (value) {
            return String(value).padStart(2, "0");
        }).join(":");
    }

    function tripTimerLabel(pointName) {
        return "В ПУТИ · " + String(pointName || "ТОЧКА НЕ УКАЗАНА").trim();
    }

    function renderTripTimer(workspace, nowValue) {
        if (!workspace) return null;
        var timer = workspace.querySelector("[data-driver-manual-trip-timer]");
        if (!timer) return null;
        var label = timer.querySelector("[data-driver-manual-trip-timer-label]");
        var value = timer.querySelector("[data-driver-manual-trip-timer-value]");
        if (!currentTripTimer) {
            timer.classList.remove("is-active");
            timer.dataset.driverManualTimerActive = "false";
            delete timer.dataset.driverManualTimerStartedAt;
            delete timer.dataset.driverManualTimerPointName;
            if (label) label.textContent = "ОЖИДАЕТ ОТПРАВКИ";
            if (value) value.textContent = "00:00:00";
            timer.setAttribute("aria-label", "Таймер ожидает отправки в точку разгрузки");
            return {active: false, elapsedSeconds: 0, formatted: "00:00:00"};
        }
        currentTripTimer.workspace = workspace;
        var now = Number(nowValue);
        if (!Number.isFinite(now)) now = Date.now();
        var elapsedSeconds = Math.max(0, Math.floor((now - currentTripTimer.startedAt) / 1000));
        var formatted = formatElapsedTime(elapsedSeconds);
        var copy = tripTimerLabel(currentTripTimer.pointName);
        timer.classList.add("is-active");
        timer.dataset.driverManualTimerActive = "true";
        timer.dataset.driverManualTimerStartedAt = String(currentTripTimer.startedAt);
        timer.dataset.driverManualTimerPointName = currentTripTimer.pointName;
        if (label) label.textContent = copy;
        if (value) value.textContent = formatted;
        timer.setAttribute("aria-label", copy + ", прошло " + formatted);
        return {active: true, elapsedSeconds: elapsedSeconds, formatted: formatted, pointName: currentTripTimer.pointName};
    }

    function ensureTripTimerTick() {
        if (tripTimerInterval || !currentTripTimer || typeof root.setInterval !== "function") return;
        tripTimerInterval = root.setInterval(function () {
            if (!currentTripTimer) return;
            renderTripTimer(currentTripTimer.workspace || currentWorkspace);
        }, 1000);
    }

    function startTripTimer(workspace, pointName, startedAt) {
        var safeStartedAt = Number(startedAt);
        if (!Number.isFinite(safeStartedAt)) safeStartedAt = Date.now();
        currentTripTimer = {
            workspace: workspace || currentWorkspace,
            pointName: String(pointName || "").trim(),
            startedAt: safeStartedAt
        };
        renderTripTimer(currentTripTimer.workspace, safeStartedAt);
        ensureTripTimerTick();
        return currentTripTimer;
    }

    function pointModeForShell(shell) {
        return shell && shell.dataset && shell.dataset.driverHasOpenTrip === "true" &&
            String(shell.dataset.driverActiveTripId || "") ? "current" : "next";
    }

    function pointActionCopy(mode) {
        return mode === "current"
            ? {label: "ТОЧКА РАЗГРУЗКИ", hint: "Изменить текущую", aria: "Изменить точку разгрузки текущего рейса"}
            : {label: "ТОЧКА РАЗГРУЗКИ", hint: "Для следующего рейса", aria: "Выбрать точку разгрузки для следующего рейса"};
    }

    function updatePointAction(workspace) {
        if (!workspace) return null;
        var shell = workspace.closest("[data-driver-shell]");
        var action = workspace.querySelector("[data-driver-manual-point-open]");
        if (!action) return null;
        var mode = pointModeForShell(shell);
        var copy = pointActionCopy(mode);
        var label = action.querySelector("[data-driver-manual-point-label]");
        var hint = action.querySelector("[data-driver-manual-point-hint]");
        if (label) label.textContent = copy.label;
        if (hint) hint.textContent = copy.hint;
        action.setAttribute("aria-label", copy.aria);
        action.dataset.driverManualPointMode = mode;
        return mode;
    }

    function dumpNameSizeClass(name) {
        var length = String(name || "").trim().length;
        if (length > 18) return "is-name-long";
        if (length > 10) return "is-name-medium";
        return "is-name-short";
    }

    function applyDumpNameSize(target, name) {
        if (!target || !target.classList) return target;
        target.classList.remove("is-name-short", "is-name-medium", "is-name-long");
        target.classList.add(dumpNameSizeClass(name));
        return target;
    }

    function createManualDumpTarget(doc, pointId, pointName, prototype) {
        var target = prototype ? prototype.cloneNode(true) : doc.createElement("button");
        if (!prototype) {
            target.type = "button";
            target.className = "eo-unload-card eo-dashboard-unload-card driver-manual-workspace__dump-card status-yellow";
            target.innerHTML = '<span class="eo-dashboard-unload-top"><strong></strong><small aria-label="Рейсов: 0">0</small></span>';
        }
        target.classList.remove("is-last-dump", "status-green", "status-red");
        target.classList.add("status-yellow", "is-driver-manual-one-off");
        applyDumpNameSize(target, pointName);
        target.removeAttribute("aria-current");
        target.dataset.eoDumpTarget = String(pointId);
        target.dataset.eoDumpName = String(pointName || "");
        target.dataset.eoDumpDistance = "";
        target.dataset.eoHasPendingTrucks = "false";
        target.dataset.driverManualDumpTarget = "";
        target.dataset.driverManualOneOff = "true";
        target.dataset.driverManualCompletedCount = "0";
        target.dataset.driverManualLastSent = "false";
        target.setAttribute("aria-label", String(pointName || "") + ": рейсов 0");
        var title = target.querySelector(".eo-dashboard-unload-top strong");
        var count = target.querySelector(".eo-dashboard-unload-top small");
        if (title) title.textContent = String(pointName || "");
        if (count) count.textContent = "0";
        return target;
    }

    function selectManualPoint(workspace, pointId, pointName) {
        if (!workspace || !pointId) return null;
        var grid = workspace.querySelector(".eo-dashboard-unload-grid");
        if (!grid) return null;
        var selector = '[data-driver-manual-dump-target][data-eo-dump-target="' + String(pointId) + '"]';
        var target = grid.querySelector(selector);
        if (!target) {
            var prototype = grid.querySelector("[data-driver-manual-dump-target]");
            target = createManualDumpTarget(workspace.ownerDocument || root.document, pointId, pointName, prototype);
            grid.appendChild(target);
            Array.from(grid.classList).forEach(function (name) {
                if (/^is-count-\d+$/.test(name)) grid.classList.remove(name);
            });
            grid.classList.add("is-count-" + grid.querySelectorAll("[data-driver-manual-dump-target]").length);
        }
        grid.querySelectorAll("[data-driver-manual-dump-target]").forEach(function (item) {
            item.classList.toggle("is-driver-manual-selected-point", item === target);
            if (item === target) item.setAttribute("aria-current", "true");
            else item.removeAttribute("aria-current");
        });
        var action = workspace.querySelector("[data-driver-manual-point-open]");
        if (action) {
            var hint = action.querySelector("[data-driver-manual-point-hint]");
            if (hint) hint.textContent = String(pointName || "");
            action.dataset.driverManualSelectedPointId = String(pointId);
            action.dataset.driverManualSelectedPointName = String(pointName || "");
        }
        return target;
    }

    function rememberPointSheet(sheet) {
        if (!sheet || sheet.__driverManualPointOriginal) return;
        var head = sheet.querySelector(".driver-unload-head");
        var paragraphs = head ? head.querySelectorAll("p") : [];
        var current = sheet.querySelector(".driver-unload-current");
        sheet.__driverManualPointOriginal = {
            title: head && head.querySelector("h2") ? head.querySelector("h2").textContent : "",
            first: paragraphs[0] ? paragraphs[0].textContent : "",
            second: paragraphs[1] ? paragraphs[1].textContent : "",
            secondHidden: paragraphs[1] ? paragraphs[1].hidden : false,
            currentHidden: current ? current.hidden : false
        };
    }

    function closePointChooser(workspace) {
        workspace = workspace || currentWorkspace || (root.document && root.document.querySelector("[data-driver-manual-workspace]"));
        if (!workspace) return;
        var shell = workspace.closest("[data-driver-shell]");
        var sheet = shell && shell.querySelector("[data-driver-point-sheet]");
        if (!sheet || sheet.dataset.driverManualPointMode !== "next") return;
        var original = sheet.__driverManualPointOriginal;
        var head = sheet.querySelector(".driver-unload-head");
        var paragraphs = head ? head.querySelectorAll("p") : [];
        var title = head && head.querySelector("h2");
        var current = sheet.querySelector(".driver-unload-current");
        if (original) {
            if (title) title.textContent = original.title;
            if (paragraphs[0]) paragraphs[0].textContent = original.first;
            if (paragraphs[1]) {
                paragraphs[1].textContent = original.second;
                paragraphs[1].hidden = original.secondHidden;
            }
            if (current) current.hidden = original.currentHidden;
        }
        sheet.querySelectorAll(".driver-unload-tile").forEach(function (button) {
            if (button.__driverManualWasDisabled !== undefined) {
                button.disabled = button.__driverManualWasDisabled;
                delete button.__driverManualWasDisabled;
            }
        });
        delete sheet.dataset.driverManualPointMode;
        sheet.hidden = true;
        if (shell) shell.classList.remove("is-point-sheet-open");
        var action = workspace.querySelector("[data-driver-manual-point-open]");
        if (action) {
            action.setAttribute("aria-expanded", "false");
            if (typeof action.focus === "function") action.focus({preventScroll: true});
        }
    }

    function openPointChooser(workspace) {
        if (!workspace) return false;
        if (currentController) currentController.cancel();
        var shell = workspace.closest("[data-driver-shell]");
        if (!shell) return false;
        if (pointModeForShell(shell) === "current") {
            var canonical = Array.from(shell.querySelectorAll("[data-driver-point-open]")).find(function (control) {
                return !control.hasAttribute("data-driver-manual-point-open");
            });
            if (!canonical || canonical.disabled) return false;
            canonical.click();
            return true;
        }
        var sheet = shell.querySelector("[data-driver-point-sheet]");
        if (!sheet) return false;
        rememberPointSheet(sheet);
        var head = sheet.querySelector(".driver-unload-head");
        var paragraphs = head ? head.querySelectorAll("p") : [];
        var title = head && head.querySelector("h2");
        var current = sheet.querySelector(".driver-unload-current");
        if (title) title.textContent = "Другая точка разгрузки";
        if (paragraphs[0]) paragraphs[0].textContent = "Выберите разовую точку для следующего ручного рейса.";
        if (paragraphs[1]) paragraphs[1].hidden = true;
        if (current) current.hidden = true;
        sheet.querySelectorAll(".driver-unload-tile").forEach(function (button) {
            button.__driverManualWasDisabled = button.disabled;
            button.disabled = false;
        });
        sheet.dataset.driverManualPointMode = "next";
        sheet.hidden = false;
        shell.classList.add("is-point-sheet-open");
        var action = workspace.querySelector("[data-driver-manual-point-open]");
        if (action) action.setAttribute("aria-expanded", "true");
        root.requestAnimationFrame(function () {
            var focusTarget = sheet.querySelector(".driver-unload-tile, [data-driver-point-close]");
            if (focusTarget) focusTarget.focus();
        });
        return true;
    }

    function closeWorkspace(workspace) {
        workspace = workspace || currentWorkspace || root.document.querySelector("[data-driver-manual-workspace]");
        if (!workspace) return;
        if (currentController) currentController.cancel();
        closePointChooser(workspace);
        var result = workspace.querySelector("[data-driver-manual-result]");
        if (result) {
            root.clearTimeout(result.__driverManualHideTimer);
            result.hidden = true;
        }
        workspace.hidden = true;
        var shell = workspace.closest("[data-driver-shell]");
        if (shell) shell.classList.remove("is-driver-manual-workspace-open");
        root.document.body.classList.remove("excavator-operator-screen");
        root.document.querySelectorAll("[data-driver-manual-open]").forEach(function (control) {
            control.setAttribute("aria-expanded", "false");
        });
    }

    function bindWorkspace(workspace) {
        if (!workspace || !root.ExcavatorDashboardDrag) return null;
        if (currentWorkspace === workspace && currentController) {
            currentController.bindAll();
            renderTripTimer(workspace);
            return currentController;
        }
        if (currentController) currentController.destroy();
        currentWorkspace = workspace;
        var result = workspace.querySelector("[data-driver-manual-result]");
        currentController = root.ExcavatorDashboardDrag.attach({
            shell: workspace,
            sourceSelector: "[data-driver-manual-source]",
            targetSelector: "[data-driver-manual-dump-target]",
            gradientId: "driver-manual-drag-comet-light",
            canDrag: function () { return true; },
            isManual: function () { return false; },
            isInactive: function () { return false; },
            isBlocked: function () { return false; },
            onDrop: function (card, target) {
                startTripTimer(workspace, target.dataset.eoDumpName);
                if (!result) return;
                result.textContent = demoResultForTarget(target.dataset.eoDumpName);
                result.hidden = false;
                root.clearTimeout(result.__driverManualHideTimer);
                result.__driverManualHideTimer = root.setTimeout(function () {
                    result.hidden = true;
                }, 2600);
            },
            haptic: function (pattern, amplitude) {
                if (typeof root.driverHaptic === "function") {
                    root.driverHaptic(pattern, amplitude);
                } else if (root.navigator && typeof root.navigator.vibrate === "function") {
                    try { root.navigator.vibrate(pattern); } catch (error) {}
                }
            }
        });
        renderTripTimer(workspace);
        workspace.__driverManualClose = function () { closeWorkspace(workspace); };
        return currentController;
    }

    function openWorkspace(control) {
        var workspace = root.document.querySelector("[data-driver-manual-workspace]");
        if (!workspace) return;
        bindWorkspace(workspace);
        workspace.hidden = false;
        var shell = workspace.closest("[data-driver-shell]");
        if (shell) shell.classList.add("is-driver-manual-workspace-open");
        root.document.body.classList.add("excavator-operator-screen");
        if (control) control.setAttribute("aria-expanded", "true");
        updatePointAction(workspace);
        renderTripTimer(workspace);
        var source = workspace.querySelector("[data-driver-manual-source]");
        var back = workspace.querySelector("[data-driver-manual-close]");
        if (source || back) (source || back).focus({preventScroll: true});
    }

    function openFreeBucket(workspace) {
        var shell = workspace && workspace.closest("[data-driver-shell]");
        var canonicalTrigger = shell && shell.querySelector('[data-mobile-dial-action="free-bucket"]');
        if (!canonicalTrigger || canonicalTrigger.disabled) return false;
        canonicalTrigger.click();
        return true;
    }

    function bindAll() {
        var workspace = root.document.querySelector("[data-driver-manual-workspace]");
        if (!workspace) return;
        bindWorkspace(workspace);
        var shell = workspace.closest("[data-driver-shell]");
        if (shell && shell.classList.contains("is-driver-manual-workspace-open")) {
            workspace.hidden = false;
            root.document.body.classList.add("excavator-operator-screen");
        }
    }

    if (root.document && !root.__driverManualExcavatorWorkspaceDelegated) {
        root.__driverManualExcavatorWorkspaceDelegated = true;
        root.document.addEventListener("click", function (event) {
            var freeBucket = event.target && event.target.closest
                ? event.target.closest("[data-driver-manual-free-bucket-open]")
                : null;
            if (freeBucket) {
                event.preventDefault();
                event.stopPropagation();
                openFreeBucket(freeBucket.closest("[data-driver-manual-workspace]"));
                return;
            }
            var pointOpen = event.target && event.target.closest
                ? event.target.closest("[data-driver-manual-point-open]")
                : null;
            if (pointOpen) {
                event.preventDefault();
                event.stopPropagation();
                openPointChooser(pointOpen.closest("[data-driver-manual-workspace]"));
                return;
            }
            var pointSheet = event.target && event.target.closest
                ? event.target.closest("[data-driver-point-sheet]")
                : null;
            if (pointSheet && pointSheet.dataset.driverManualPointMode === "next") {
                var pointButton = event.target.closest(".driver-unload-tile");
                if (pointButton) {
                    event.preventDefault();
                    event.stopPropagation();
                    var pointForm = pointButton.closest("form");
                    var pointInput = pointForm && pointForm.querySelector('[name="dump_point"]');
                    var pointWorkspace = root.document.querySelector("[data-driver-manual-workspace]");
                    selectManualPoint(pointWorkspace, pointInput && pointInput.value, pointButton.dataset.driverPointName);
                    closePointChooser(pointWorkspace);
                    return;
                }
                if (event.target.closest("[data-driver-point-close]") || event.target === pointSheet) {
                    event.preventDefault();
                    closePointChooser(root.document.querySelector("[data-driver-manual-workspace]"));
                    return;
                }
            }
            var close = event.target && event.target.closest
                ? event.target.closest("[data-driver-manual-close]")
                : null;
            if (close) {
                event.preventDefault();
                closeWorkspace(close.closest("[data-driver-manual-workspace]"));
                return;
            }
            var tab = event.target && event.target.closest
                ? event.target.closest("[data-driver-tab-open]")
                : null;
            if (tab) {
                closeWorkspace();
                return;
            }
            var open = event.target && event.target.closest
                ? event.target.closest("[data-driver-manual-open]")
                : null;
            if (!open || open.disabled) return;
            event.preventDefault();
            openWorkspace(open);
        });
        if (typeof root.addEventListener === "function") {
            root.addEventListener("operational-state-refresh-applied", bindAll);
            root.addEventListener("blur", function () {
                if (currentController) currentController.cancel();
            });
        }
        root.document.addEventListener("keydown", function (event) {
            if (event.key !== "Escape") return;
            var sheet = root.document.querySelector('[data-driver-point-sheet][data-driver-manual-point-mode="next"]');
            if (!sheet || sheet.hidden) return;
            event.preventDefault();
            closePointChooser(root.document.querySelector("[data-driver-manual-workspace]"));
        });
    }

    root.bindDriverManualExcavatorWorkspace = bindAll;
    root.DriverManualExcavatorWorkspace = {
        bindAll: bindAll,
        open: openWorkspace,
        close: closeWorkspace,
        openFreeBucket: openFreeBucket,
        openPointChooser: openPointChooser,
        closePointChooser: closePointChooser,
        selectManualPoint: selectManualPoint,
        createManualDumpTarget: createManualDumpTarget,
        dumpNameSizeClass: dumpNameSizeClass,
        formatElapsedTime: formatElapsedTime,
        tripTimerLabel: tripTimerLabel,
        renderTripTimer: renderTripTimer,
        startTripTimer: startTripTimer,
        pointModeForShell: pointModeForShell,
        pointActionCopy: pointActionCopy,
        demoResultForTarget: demoResultForTarget
    };
    if (typeof root.document !== "undefined") {
        if (root.document.readyState === "loading") {
            root.document.addEventListener("DOMContentLoaded", bindAll, {once: true});
        } else {
            bindAll();
        }
    }
    if (typeof module !== "undefined" && module.exports) module.exports = root.DriverManualExcavatorWorkspace;
})(typeof window !== "undefined" ? window : globalThis);
