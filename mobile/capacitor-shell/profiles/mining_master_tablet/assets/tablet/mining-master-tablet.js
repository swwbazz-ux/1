/* APK-only presentation adapter. All operations stay in the existing role handlers. */
(function (global) {
    "use strict";
    var VERSION = "1.1.1";
    var SUPPORTED_SHELLS = ["dispatcher-desktop-shell-v166"];
    var RELATED_PATHS = [
        "/dispatcher/reports/", "/dispatcher/shift-log/", "/dispatcher/management/",
        "/dispatcher/mining-volumes/", "/dispatcher/transport/", "/dispatcher/downtimes/",
        "/dispatcher/shift-reports/trucks/", "/dispatcher/shift-reports/excavation/",
        "/dispatcher/shift-reports/hourly/", "/dispatcher/shift-reports/hourly-grid/",
        "/reports/customer-daily/", "/reports/volume/", "/reports/templates/", "/reports/downtimes/"
    ];
    var SOURCE = "[data-dispatcher-drag][data-equipment-id]";
    var MODALS = ".app-confirm-modal:not([hidden]), .dispatcher-notice-modal:not([hidden]), [data-gd-equipment-detail]:not([hidden]), [data-mm-shift-plan-detail]:not([hidden])";

    function allowedLocation(location) {
        return location.protocol === "https:" && location.hostname === "dispatcher.driverform.ru" &&
            (!location.port || location.port === "443") &&
            location.pathname.replace(/\/+$/, "") === "/dispatcher/control";
    }
    function allowedRelatedLocation(location) {
        return location.protocol === "https:" && location.hostname === "dispatcher.driverform.ru" &&
            (!location.port || location.port === "443") && RELATED_PATHS.indexOf(location.pathname) >= 0;
    }
    function snapshot(source, board) {
        return {
            source: source,
            board: board,
            kind: source.dataset.dispatcherDrag,
            id: source.dataset.equipmentId,
            assignment: source.dataset.haulAssignmentStateId || "",
            zone: source.dataset.placementZone || "",
            assignedZone: source.dataset.assignedZone || "",
            complexTruck: source.dataset.complexTruck || "",
            consumed: false
        };
    }
    function canDrop(selection, target, context) {
        if (!selection || selection.consumed || !context.compatible || !context.shiftOpen || context.observer || context.modalOpen) return false;
        var source = selection.source;
        if (!source || !source.isConnected || !target || !target.isConnected || !selection.board || !selection.board.isConnected) return false;
        if (context.board !== selection.board || !selection.board.contains(source) || !selection.board.contains(target)) return false;
        if (source.dataset.dragBound !== "true" || target.dataset.dispatcherDropBound !== "true") return false;
        if (source.classList.contains("is-placeholder") || source.classList.contains("is-assigned")) return false;
        if (source.dataset.equipmentId !== selection.id || source.dataset.dispatcherDrag !== selection.kind ||
            (source.dataset.haulAssignmentStateId || "") !== selection.assignment ||
            (source.dataset.placementZone || "") !== selection.zone ||
            (source.dataset.assignedZone || "") !== selection.assignedZone ||
            (source.dataset.complexTruck || "") !== selection.complexTruck) return false;
        var destination = target.dataset.dispatcherDrop;
        if (selection.kind === "excavator") return destination === "complex";
        if (selection.kind === "complex") return destination === "excavator-garage" || destination === "truck-garage";
        if (selection.kind !== "truck") return false;
        if (destination === "truck-garage") return source.dataset.complexTruck === "true";
        if (destination !== "complex" || !target.dataset.equipmentId) return false;
        return source.closest("[data-dispatcher-drop='complex']") !== target;
    }
    function dispatchExistingDrop(selection, target, context, constructors) {
        if (!canDrop(selection, target, context)) return false;
        var transfer = new constructors.DataTransfer();
        var source = selection.source;
        selection.consumed = true;
        var started = false;
        try {
            var start = new constructors.DragEvent("dragstart", { bubbles: true, cancelable: true, dataTransfer: transfer });
            source.dispatchEvent(start);
            started = true;
            if (start.defaultPrevented || !source.classList.contains("dispatcher-dragging")) return false;
            target.dispatchEvent(new constructors.DragEvent("dragover", { bubbles: true, cancelable: true, dataTransfer: transfer }));
            target.dispatchEvent(new constructors.DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer: transfer }));
            return true;
        } finally {
            if (started) source.dispatchEvent(new constructors.DragEvent("dragend", { bubbles: true, cancelable: true, dataTransfer: transfer }));
        }
    }

    /* Pure adapter exports let tests execute real server DnD handlers without HTTP. */
    if (typeof module !== "undefined" && module.exports) {
        module.exports = { allowedLocation: allowedLocation, allowedRelatedLocation: allowedRelatedLocation, snapshot: snapshot, canDrop: canDrop, dispatchExistingDrop: dispatchExistingDrop };
        return;
    }
    if (global.CopperMiningMasterTablet) {
        global.CopperMiningMasterTablet.init();
        return;
    }
    var doc = global.document, board = null, selection = null, editMode = false, compatible = false;
    var bound = false, queued = false, busyUntil = 0, message = "";
    function setText(node, value) { if (node && node.textContent !== value) node.textContent = value; }
    /* Attribute no-ops still notify DOM observers. In particular, writing an
       unchanged disabled value wakes role-readonly, which republishes access
       attributes and can otherwise feed straight back into reconcile. */
    function setClass(node, name, enabled) {
        if (node.classList.contains(name) !== enabled) node.classList.toggle(name, enabled);
    }
    function setAttribute(node, name, value) {
        if (value === null) { if (node.hasAttribute(name)) node.removeAttribute(name); }
        else if (node.getAttribute(name) !== value) node.setAttribute(name, value);
    }
    function setBoolean(node, name, value) { if (node[name] !== value) node[name] = value; }
    function setStyle(node, name, value) {
        if (node.style.getPropertyValue(name) !== value) node.style.setProperty(name, value);
    }
    function button(label, action) {
        var node = doc.createElement("button"); node.type = "button"; node.dataset.crMmAction = action; node.textContent = label; return node;
    }
    function isCompatible() {
        if (!allowedLocation(global.location) || !doc.body.classList.contains("dispatcher-control-screen") || doc.body.classList.contains("mining-master-mobile-screen")) return false;
        if (doc.body.dataset.appRoleCode !== "dispatcher" || SUPPORTED_SHELLS.indexOf(doc.body.dataset.appShellVersion) < 0) return false;
        var shell = doc.querySelector(".dispatcher-shell");
        if (!shell || !doc.querySelector(".dispatcher-board .dispatcher-zone-grid") || !doc.querySelector(".dispatcher-nav a[href]")) return false;
        try {
            var move = new URL(shell.dataset.dispatcherMoveUrl, global.location.href), assign = new URL(shell.dataset.dispatcherAssignUrl, global.location.href);
            if (move.origin !== global.location.origin || assign.origin !== global.location.origin ||
                move.pathname !== "/dispatcher/control/excavator/move/" || assign.pathname !== "/dispatcher/control/truck/assign/") return false;
        } catch (_) { return false; }
        return typeof global.DataTransfer === "function" && typeof global.DragEvent === "function";
    }
    function context() {
        return { compatible: compatible && isCompatible(), board: doc.querySelector(".dispatcher-board"),
            shiftOpen: !!board && board.dataset.dispatcherShiftOpen === "true" && !board.classList.contains("is-readonly") && doc.body.dataset.roleAccessActive !== "false",
            observer: (doc.body.dataset.observerMode === "true" || doc.body.classList.contains("is-observer-mode")) && doc.body.dataset.observerControl !== "true",
            modalOpen: !!doc.querySelector(MODALS) };
    }
    function clearSelection(text, endMode) {
        selection = null; if (endMode) editMode = false; if (text !== undefined) message = text;
        doc.querySelectorAll(".cr-mm-selected, .cr-mm-target-available").forEach(function (node) {
            node.classList.remove("cr-mm-selected", "cr-mm-target-available"); setAttribute(node, "data-cr-mm-target-label", null);
        });
        updateToolbar();
    }
    function updateTargets() {
        if (!board) return;
        var state = context();
        board.querySelectorAll("[data-dispatcher-drop]").forEach(function (zone) {
            var available = canDrop(selection, zone, state); setClass(zone, "cr-mm-target-available", available);
            setAttribute(zone, "data-cr-mm-target-label", available ? (zone.dataset.dispatcherDrop === "complex" ? "Назначить сюда" :
                zone.dataset.dispatcherDrop === "excavator-garage" ? "В резерв экскаваторов" : "В резерв самосвалов") : null);
        });
    }
    function ensureToolbar() {
        if (doc.getElementById("cr-mm-toolbar")) return;
        var toolbar = doc.createElement("section"); toolbar.id = "cr-mm-toolbar"; toolbar.setAttribute("aria-label", "Управление планшетом");
        var search = doc.createElement("input"); search.type = "search"; search.maxLength = 32;
        search.placeholder = "Номер техники"; search.setAttribute("aria-label", "Поиск техники по номеру"); search.dataset.crMmSearch = "true";
        toolbar.appendChild(search);
        var hint = doc.createElement("span"); hint.dataset.crMmHint = "true"; hint.setAttribute("role", "status"); hint.setAttribute("aria-live", "polite"); toolbar.appendChild(hint);
        toolbar.appendChild(button("Перестановка", "mode")); toolbar.appendChild(button("Карточка", "detail")); toolbar.appendChild(button("Отмена", "cancel"));
        doc.body.appendChild(toolbar);
    }
    function updateToolbar() {
        var toolbar = doc.getElementById("cr-mm-toolbar"); if (!toolbar) return;
        var state = context(), mode = toolbar.querySelector("[data-cr-mm-action='mode']");
        setBoolean(mode, "disabled", !state.compatible || !state.shiftOpen || state.observer);
        setAttribute(mode, "aria-pressed", editMode ? "true" : "false"); setText(mode, editMode ? "Готово" : "Перестановка");
        setBoolean(toolbar.querySelector("[data-cr-mm-action='detail']"), "hidden", !selection);
        setBoolean(toolbar.querySelector("[data-cr-mm-action='cancel']"), "hidden", !selection);
        var fallback = editMode ? "Выберите технику, затем комплекс или резерв" : state.shiftOpen && !state.observer ?
            "Нажатие — карточка техники. Для переноса включите «Перестановка»." : "Просмотр · для управления техникой нужна открытая смена";
        setText(toolbar.querySelector("[data-cr-mm-hint]"), message || fallback);
        setClass(doc.documentElement, "cr-mm-edit-mode", editMode);
    }
    function select(source) {
        var state = context();
        if (!editMode || !source || !state.compatible || !state.shiftOpen || state.observer || state.modalOpen || Date.now() < busyUntil) return;
        if (source.dataset.dragBound !== "true" || source.classList.contains("is-placeholder") || source.classList.contains("is-assigned")) return;
        if (selection && selection.source === source) { clearSelection(""); return; }
        clearSelection(""); selection = snapshot(source, board); source.classList.add("cr-mm-selected");
        var name = source.dataset.equipmentName || source.dataset.equipmentId;
        message = (selection.kind === "truck" ? "Самосвал " : selection.kind === "complex" ? "Комплекс " : "Экскаватор ") + name + " · нажмите выделенное место назначения";
        updateTargets(); updateToolbar();
    }
    function performDrop(target) {
        if (Date.now() < busyUntil) return;
        var state = context();
        if (!canDrop(selection, target, state)) { clearSelection("Состояние изменилось. Выберите технику ещё раз.", true); return; }
        busyUntil = Date.now() + 1200;
        try {
            var done = dispatchExistingDrop(selection, target, state, global);
            clearSelection(done ? "Действие передано приложению. Проверьте результат на пульте." : "Действие не выполнено. Проверьте состояние смены.", true);
        } catch (_) { clearSelection("Не удалось выполнить действие. Выберите технику ещё раз.", true); }
    }
    function fitCanvas() {
        if (!compatible) return;
        var canvas = doc.querySelector("[data-dispatcher-canvas]"); if (!canvas) return;
        var height = Math.max(240, global.innerHeight - 64), designHeight = 1000;
        /* The keyboard reduces working height, not the readable size of every
           control. Keep the usual tablet scale and let the existing grids scroll. */
        var referenceHeight = global.innerHeight < 640 ? 736 : height;
        var width = Math.max(1400, Math.round(designHeight * global.innerWidth / referenceHeight));
        var scale = Math.min(global.innerWidth / width, referenceHeight / designHeight);
        var canvasHeight = Math.min(designHeight, height / scale);
        setAttribute(canvas, "data-dispatcher-canvas", "on");
        setStyle(canvas, "--dispatcher-canvas-w", width + "px"); setStyle(canvas, "--dispatcher-canvas-h", canvasHeight + "px");
        setStyle(canvas, "--dispatcher-canvas-scale", String(scale)); setStyle(canvas, "--gd-vw", width / 100 + "px"); setStyle(canvas, "--gd-vh", designHeight / 100 + "px");
        setStyle(doc.documentElement, "--cr-mm-scale", String(scale));
        setStyle(doc.documentElement, "--cr-mm-canvas-center", height / 2 + "px");
    }
    function updateRackControls() {
        if (!board) return;
        board.querySelectorAll(".complex-truck-more").forEach(function (more) {
            var rack = more.closest(".complex-assigned-trucks");
            var expanded = !!rack && rack.classList.contains("cr-mm-expanded-rack");
            setAttribute(more, "role", "button"); setAttribute(more, "tabindex", "0");
            setAttribute(more, "aria-expanded", expanded ? "true" : "false");
            setAttribute(more, "aria-label", expanded ? "Свернуть список самосвалов" : "Показать все самосвалы комплекса");
        });
    }
    function collapseRacks() {
        doc.querySelectorAll(".cr-mm-expanded-rack").forEach(function (rack) { rack.classList.remove("cr-mm-expanded-rack"); });
        updateRackControls();
    }
    function updateSearch(scroll) {
        if (!board) return;
        var input = doc.querySelector("[data-cr-mm-search]"), needle = input ? input.value.trim().toLocaleLowerCase("ru") : "", first = null;
        board.querySelectorAll(SOURCE).forEach(function (node) {
            var hit = !!needle && (node.dataset.equipmentName || "").toLocaleLowerCase("ru").indexOf(needle) !== -1;
            setClass(node, "cr-mm-search-hit", hit); if (hit && !first) first = node;
            if (scroll && hit && node.hidden) {
                var rack = node.closest(".complex-assigned-trucks");
                if (rack) setClass(rack, "cr-mm-expanded-rack", true);
            }
        });
        updateRackControls();
        if (scroll && first) first.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
    function reconcile() {
        queued = false; if (!allowedLocation(global.location)) return;
        compatible = isCompatible();
        if (!compatible) {
            clearSelection("", true); collapseRacks(); setClass(doc.documentElement, "cr-mm-tablet", false);
            var toolbar = doc.getElementById("cr-mm-toolbar"); if (toolbar) toolbar.remove();
            if (!doc.getElementById("cr-mm-compatibility-warning")) {
                var warning = doc.createElement("div"); warning.id = "cr-mm-compatibility-warning"; warning.setAttribute("role", "alert");
                warning.textContent = "Планшетный режим требует обновления приложения. Отображается обычный диспетчерский пульт."; doc.body.prepend(warning);
            }
            return;
        }
        var warning = doc.getElementById("cr-mm-compatibility-warning"); if (warning) warning.remove();
        var next = doc.querySelector(".dispatcher-board");
        if (board !== next) { var editing = editMode; board = next; collapseRacks(); clearSelection(editing ? "Пульт обновился. При необходимости включите перестановку снова." : "", true); }
        else if (selection && (!selection.source.isConnected || (selection.source.dataset.haulAssignmentStateId || "") !== selection.assignment || !context().shiftOpen)) clearSelection("Состояние изменилось.", true);
        setClass(doc.documentElement, "cr-mm-tablet", true);
        ensureToolbar(); updateToolbar(); updateTargets(); updateSearch(false); fitCanvas();
    }
    function schedule() { if (!queued) { queued = true; global.requestAnimationFrame(reconcile); } }
    function bind() {
        if (bound) return; bound = true;
        doc.addEventListener("click", function (event) {
            if (!compatible || !event.target.closest) return;
            var more = event.target.closest(".complex-truck-more");
            if (more && board && board.contains(more) && !context().modalOpen) {
                event.preventDefault(); event.stopPropagation();
                var rack = more.closest(".complex-assigned-trucks");
                if (rack) { rack.classList.toggle("cr-mm-expanded-rack"); updateRackControls(); }
                return;
            }
            var control = event.target.closest("[data-cr-mm-action]");
            if (control && !control.disabled) {
                event.preventDefault(); event.stopPropagation();
                var action = control.dataset.crMmAction;
                if (action === "mode") { var state = context(); if (!state.shiftOpen || state.observer || state.modalOpen) return; clearSelection(""); editMode = !editMode; updateToolbar(); }
                else if (action === "cancel") clearSelection("", true);
                else if (action === "detail" && selection) { var node = selection.source; clearSelection("", true); if (node.isConnected) node.click(); }
                return;
            }
            if (!editMode || !board || !board.contains(event.target) || context().modalOpen || event.target.closest("button, a, input, select, textarea, form")) return;
            var target = event.target.closest("[data-dispatcher-drop]"), source = event.target.closest(SOURCE);
            if (selection && target && canDrop(selection, target, context())) { event.preventDefault(); event.stopPropagation(); performDrop(target); }
            else if (source) { event.preventDefault(); event.stopPropagation(); select(source); }
        }, true);
        doc.addEventListener("input", function (event) { if (event.target.matches("[data-cr-mm-search]")) updateSearch(true); });
        doc.addEventListener("keydown", function (event) {
            if ((event.key === "Enter" || event.key === " ") && event.target.matches(".complex-truck-more[role='button']")) { event.preventDefault(); event.stopPropagation(); event.target.click(); }
            if (event.key === "Escape") { if (editMode) clearSelection("", true); collapseRacks(); }
        }, true);
        doc.addEventListener("visibilitychange", function () { if (doc.hidden) { clearSelection("", true); collapseRacks(); } });
        global.addEventListener("pagehide", function () { clearSelection("", true); collapseRacks(); });
        global.addEventListener("resize", fitCanvas);
        if (global.visualViewport) global.visualViewport.addEventListener("resize", fitCanvas);
        var observer = new MutationObserver(function (records) {
            if (records.some(function (record) {
                var node = record.target.nodeType === 1 ? record.target : record.target.parentElement;
                if (node && node.closest("#cr-mm-toolbar, #cr-mm-compatibility-warning")) return false;
                if (record.type === "attributes") return record.oldValue !== record.target.getAttribute(record.attributeName);
                return Array.prototype.some.call(record.addedNodes, function (child) { return child.nodeType === 1 && child.id !== "cr-mm-toolbar" && child.id !== "cr-mm-compatibility-warning"; }) ||
                    Array.prototype.some.call(record.removedNodes, function (child) { return child === board || selection && (child === selection.source || child.contains && child.contains(selection.source)); });
            })) schedule();
        });
        observer.observe(doc.body, { childList: true, subtree: true, attributes: true, attributeOldValue: true, attributeFilter: ["data-dispatcher-shift-open", "data-role-access-active", "data-observer-mode", "data-observer-control", "data-app-role-code", "data-app-shell-version", "data-drag-bound", "data-dispatcher-drop-bound", "data-haul-assignment-state-id"] });
    }
    function init() {
        if (!allowedLocation(global.location) && !allowedRelatedLocation(global.location)) return;
        if (!doc.body || doc.readyState === "loading") { doc.addEventListener("DOMContentLoaded", init, { once: true }); return; }
        if (allowedRelatedLocation(global.location)) {
            /* Report pages use the real viewport: Android clips the scrolling shell
               when it is nested inside the desktop transform canvas. No board actions. */
            var pageReady = doc.body.dataset.appRoleCode === "dispatcher" &&
                SUPPORTED_SHELLS.indexOf(doc.body.dataset.appShellVersion) >= 0 &&
                doc.body.classList.contains("dispatcher-control-screen") &&
                !!doc.querySelector(global.location.pathname === "/reports/templates/"
                    ? "body > .dispatcher-analytics-shell.management-linked-report > .dispatcher-analytics-board"
                    : "[data-dispatcher-canvas] > .dispatcher-analytics-shell > .dispatcher-analytics-board");
            setClass(doc.documentElement, "cr-mm-tablet-page", pageReady);
            return;
        }
        bind(); reconcile();
    }
    global.CopperMiningMasterTablet = { version: VERSION, init: init, cancel: function () { clearSelection("", true); } };
    init();
})(typeof window !== "undefined" ? window : globalThis);
