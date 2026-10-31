/* APK-only presentation adapter. All operations stay in the existing role handlers. */
(function (global) {
    "use strict";
    var VERSION = "1.2.1";
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

    /* A single closing panel retains the screen until its transition completes.
       Activity only rearms timers; it never republishes unchanged DOM state. */
    function createPanelState(options) {
        var active = null, phase = "closed", desired = null, paused = false, dead = false, idle = null, motion = null;
        function stopIdle() { if (idle !== null) options.clearTimer(idle); idle = null; }
        function publish() { options.render({ active: active, phase: phase }); }
        function arm() {
            stopIdle();
            if (dead || paused || phase !== "open") return;
            idle = options.setTimer(function () {
                idle = null;
                if (options.blocked && options.blocked()) { setPaused(true); return; }
                request(null);
            }, 10000);
        }
        function request(side) {
            if (dead) return;
            desired = side || null;
            if (phase === "closing") return;
            if (active === desired) { arm(); return; }
            stopIdle();
            if (active) {
                phase = "closing"; publish();
                motion = options.setTimer(function () {
                    motion = null; active = null; phase = "closed"; publish();
                    if (desired) { active = desired; phase = "open"; publish(); arm(); }
                }, 200);
            } else if (desired) { active = desired; phase = "open"; publish(); arm(); }
        }
        function setPaused(value) { if (paused === value || dead) return; paused = value; if (paused) stopIdle(); else arm(); }
        return { request: request, activity: arm, pause: setPaused,
            state: function () { return { active: active, phase: phase, paused: paused }; },
            destroy: function () { dead = true; stopIdle(); if (motion !== null) options.clearTimer(motion); motion = null; } };
    }

    /* Pure adapter exports let tests execute real server DnD handlers without HTTP. */
    if (typeof module !== "undefined" && module.exports) {
        module.exports = { allowedLocation: allowedLocation, allowedRelatedLocation: allowedRelatedLocation, snapshot: snapshot, canDrop: canDrop, dispatchExistingDrop: dispatchExistingDrop, createPanelState: createPanelState };
        return;
    }
    if (global.CopperMiningMasterTablet) {
        global.CopperMiningMasterTablet.init();
        return;
    }
    var doc = global.document, board = null, selection = null, editMode = false, compatible = false;
    var bound = false, queued = false, busyUntil = 0, message = "", edgePanels = null;
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
    function createEdgePanels(owner) {
        var canvas = owner.closest("[data-dispatcher-canvas]");
        var nodes = { top: owner.querySelector(":scope > .dispatcher-topbar"), bottom: doc.getElementById("cr-mm-toolbar"),
            left: owner.querySelector(":scope > .dispatcher-left"), right: owner.querySelector(":scope > .dispatcher-right") };
        if (!canvas || Object.keys(nodes).some(function (side) { return !nodes[side]; })) return null;
        var root = doc.documentElement, handles = {}, edges = {}, originals = {}, listeners = [], pointers = new Set();
        var dead = false, gesture = null, suppressClickUntil = 0, pendingReveal = null, pendingManual = null, pendingLayout = false;
        var nativeDrag = null, dragEpoch = 0, dragStartTimer = null;
        var fullHeight = global.innerHeight, fullWidth = global.innerWidth, layoutKey = "", frame = null, settle = null;
        var tileFrame = null, tileSample = null, pendingTiles = false;
        var backdrop = doc.createElement("div"); backdrop.className = "cr-mm-panel-backdrop"; backdrop.hidden = true; owner.appendChild(backdrop);
        var labels = { top: "верхнюю панель", bottom: "нижнюю панель", left: "левую панель", right: "правую панель" };
        function listen(node, type, callback, capture) { node.addEventListener(type, callback, capture); listeners.push(function () { node.removeEventListener(type, callback, capture); }); }
        function editing() { return !!doc.activeElement && doc.activeElement.matches("input:not([type=button]):not([type=submit]):not([type=checkbox]):not([type=radio]), textarea, select, [contenteditable]:not([contenteditable=false])"); }
        function dialogOpen() {
            return Array.prototype.some.call(doc.querySelectorAll(MODALS + ", dialog[open], [aria-modal=true]"), function (node) {
                return node.getClientRects().length && global.getComputedStyle(node).visibility !== "hidden";
            });
        }
        function keyboardOpen() {
            return global.innerWidth === fullWidth && (global.innerHeight < fullHeight - 120 ||
                global.visualViewport && global.visualViewport.height < global.innerHeight - 120);
        }
        function blocked() { return doc.hidden || pointers.size > 0 || !!nativeDrag || editing() || dialogOpen() || keyboardOpen(); }
        function scrollRevealed() {
            if (!pendingReveal || !pendingReveal.node.isConnected || !owner.contains(pendingReveal.node)) { pendingReveal = null; return; }
            var state = machine.state();
            if (!pendingReveal.side && state.phase === "closed" || pendingReveal.side === state.active && state.phase === "open") {
                pendingReveal.node.scrollIntoView({ block: "nearest", inline: "nearest" }); pendingReveal = null;
            }
        }
        function render(state) {
            setAttribute(root, "data-cr-panel-open", state.active || "");
            Object.keys(nodes).forEach(function (side) {
                var nodeState = state.active === side ? state.phase : "closed";
                setAttribute(nodes[side], "data-cr-panel-state", nodeState);
                setBoolean(nodes[side], "inert", nodeState !== "open");
                setAttribute(nodes[side], "aria-hidden", nodeState === "open" ? "false" : "true");
                setAttribute(handles[side], "aria-expanded", nodeState === "open" ? "true" : "false");
                setAttribute(handles[side], "aria-label", (nodeState === "open" ? "Скрыть " : "Показать ") + labels[side]);
                setBoolean(edges[side], "hidden", nodeState !== "closed");
            });
            setBoolean(backdrop, "hidden", !state.active);
            if (pendingReveal && (state.phase === "open" || !pendingReveal.side && state.phase === "closed")) {
                global.requestAnimationFrame(function () { if (!dead) scrollRevealed(); });
            }
        }
        var machine = createPanelState({ render: render, blocked: blocked,
            setTimer: function (callback, delay) { return global.setTimeout(callback, delay); }, clearTimer: function (id) { global.clearTimeout(id); } });
        /* The desktop canvas also responds to window/visualViewport resize.
           Its variables remain server-owned. The APK uses separate dimensions,
           so even a same-size resize cannot briefly shrink the live truck racks. */
        var observedCanvas = null, serverCanvasMode = null;
        function keepCanvasMode() {
            if (dead) return;
            var mode = canvas.getAttribute("data-dispatcher-canvas");
            if (canvasObserver.takeRecords().length || mode !== "on") serverCanvasMode = mode;
            if (mode !== "on") {
                setAttribute(canvas, "data-dispatcher-canvas", "on");
                /* Consume only our own mode write, so it cannot replace the
                   last server mode that must be restored on fail-closed exit. */
                canvasObserver.takeRecords();
            }
        }
        var canvasObserver = new MutationObserver(function () { serverCanvasMode = canvas.getAttribute("data-dispatcher-canvas"); keepCanvasMode(); });
        function watchCanvas() {
            if (observedCanvas !== canvas) {
                canvasObserver.disconnect();
                if (observedCanvas) setAttribute(observedCanvas, "data-dispatcher-canvas", serverCanvasMode);
                observedCanvas = canvas; serverCanvasMode = canvas.getAttribute("data-dispatcher-canvas");
                canvasObserver.observe(canvas, { attributes: true, attributeFilter: ["data-dispatcher-canvas"] });
            }
            keepCanvasMode();
        }
        function syncTileSize() {
            tileFrame = null;
            if (dead) return;
            if (blocked()) { pendingTiles = true; return; }
            pendingTiles = false;
            var sample = owner.querySelector(".dispatcher-complex-card .complex-truck-tile:not([hidden])");
            if (sample !== tileSample) {
                if (tileResize) tileResize.disconnect(); tileSample = sample;
                if (sample && tileResize) tileResize.observe(sample);
            }
            /* offset sizes exclude hover/drag transforms and include the same
               border box used by the real rack layout. Never write into a rack. */
            var width = sample && sample.offsetWidth, height = sample && sample.offsetHeight;
            if (!width || !height) {
                var rack = owner.querySelector(".complex-assigned-trucks"), styles = rack && global.getComputedStyle(rack);
                width = styles && parseFloat(styles.getPropertyValue("--complex-truck-w")) || 88;
                height = styles && parseFloat(styles.getPropertyValue("--complex-truck-h")) || 63;
            }
            setStyle(owner, "--cr-panel-tile-w", width + "px"); setStyle(owner, "--cr-panel-tile-h", height + "px");
            setStyle(root, "--cr-panel-side-edge", (width + 22) * (Number(root.style.getPropertyValue("--cr-mm-scale")) || 1) + "px");
        }
        function scheduleTiles() { if (!dead && tileFrame === null) tileFrame = global.requestAnimationFrame(syncTileSize); }
        var tileResize = typeof global.ResizeObserver === "function" ? new global.ResizeObserver(scheduleTiles) : null;
        var tileObserver = new MutationObserver(scheduleTiles);
        function watchTiles() {
            tileObserver.disconnect(); if (tileResize) tileResize.disconnect(); tileSample = null;
            var grid = owner.querySelector(".dispatcher-zone-grid");
            if (grid) tileObserver.observe(grid, { childList: true, subtree: true, attributes: true, attributeFilter: ["style", "hidden"] });
            if (tileFrame !== null) global.cancelAnimationFrame(tileFrame);
            syncTileSize();
        }
        function layout(force) {
            if (dead) return;
            keepCanvasMode();
            /* Android's transient system bars and IME change innerHeight during
               a gesture. At a fixed landscape width keep the largest viewport
               already seen; shrinking it must not rearrange the working field. */
            var height = global.innerWidth === fullWidth ? Math.max(fullHeight, global.innerHeight) : global.innerHeight;
            var key = global.innerWidth + "x" + height;
            if (!force && layoutKey === key) { pendingLayout = false; return; }
            if (blocked()) { pendingLayout = true; return; }
            layoutKey = key; pendingLayout = false;
            fullWidth = global.innerWidth; fullHeight = height;
            /* Preserve readable tablet scale, but give the field the whole
               viewport permanently. Opening a panel only changes its transform.
               Do not remove the panel class to measure the old desktop grid:
               the server's ResizeObserver would recalculate every truck rack. */
            var referenceHeight = height < 640 ? 736 : Math.max(240, height - 64);
            var width = Math.max(1400, Math.round(1000 * global.innerWidth / referenceHeight));
            var scale = Math.min(global.innerWidth / width, referenceHeight / 1000);
            setStyle(root, "--cr-mm-scale", String(scale));
            setStyle(root, "--cr-panel-canvas-w", width + "px");
            setStyle(root, "--cr-panel-canvas-h", height / scale + "px");
            setStyle(root, "--cr-panel-canvas-vw", width / 100 + "px");
            setStyle(root, "--cr-panel-top-edge", "126px");
            setStyle(root, "--cr-mm-canvas-center", height / 2 + "px");
            setClass(root, "cr-mm-panels", true); setAttribute(root, "data-cr-panels-ready", "true"); scheduleTiles();
        }
        function refresh() {
            if (dead) return;
            setAttribute(root, "data-cr-panel-modal", dialogOpen() ? "true" : "false");
            var paused = blocked(); machine.pause(paused);
            if (!paused) {
                if (pendingLayout) layout(false);
                if (pendingTiles) scheduleTiles();
                if (pendingManual) { var wanted = pendingManual.side; pendingManual = null; machine.request(wanted); }
                else if (pendingReveal) { machine.request(pendingReveal.side); scrollRevealed(); }
            }
        }
        function manualRequest(side) {
            pendingReveal = null; pendingManual = { side: side };
            if (editing()) doc.activeElement.blur();
            refresh();
        }
        function sideFor(node) { return nodes.left.contains(node) ? "left" : nodes.right.contains(node) ? "right" : null; }
        function finishNativeDrag() {
            dragEpoch += 1;
            if (dragStartTimer !== null) global.clearTimeout(dragStartTimer); dragStartTimer = null;
            if (!nativeDrag) return;
            nativeDrag = null; setAttribute(root, "data-cr-panel-native-drag", null);
            machine.request(null); refresh();
        }
        Object.keys(nodes).forEach(function (side) {
            var copiedPresentation = nodes[side].hasAttribute("data-cr-panel");
            originals[side] = { aria: copiedPresentation ? null : nodes[side].getAttribute("aria-hidden"), inert: copiedPresentation ? false : nodes[side].inert };
            setAttribute(nodes[side], "data-cr-panel", side);
            var handle = doc.createElement("button"); handle.type = "button"; handle.className = "cr-mm-panel-handle";
            handle.dataset.crPanelSide = side; handle.setAttribute("data-role-readonly-safe", "");
            var edge = doc.createElement("div"); edge.className = "cr-mm-panel-edge"; edge.dataset.crPanelSide = side; edge.setAttribute("aria-hidden", "true");
            handles[side] = handle; edges[side] = edge; doc.body.appendChild(edge); doc.body.appendChild(handle);
        });
        render(machine.state()); watchCanvas(); layout(true); watchTiles();
        function stop(event) { event.preventDefault(); event.stopImmediatePropagation(); }
        function click(event) {
            var handle = event.target.closest && event.target.closest(".cr-mm-panel-handle");
            if (handle) {
                stop(event);
                if (Date.now() < suppressClickUntil || dialogOpen()) return;
                var side = handle.dataset.crPanelSide, state = machine.state();
                manualRequest(state.active === side && state.phase === "open" ? null : side);
            } else if (event.target === backdrop) { stop(event); if (!dialogOpen()) manualRequest(null); }
        }
        function inward(side, dx, dy) { return side === "left" ? dx : side === "right" ? -dx : side === "top" ? dy : -dy; }
        listen(doc, "pointerdown", function (event) {
            pointers.add(event.pointerId); machine.pause(true);
            var start = event.target.closest && event.target.closest(".cr-mm-panel-handle, .cr-mm-panel-edge");
            if (start && event.isPrimary && event.button === 0 && !dialogOpen()) {
                var side = start.dataset.crPanelSide;
                gesture = { id: event.pointerId, start: start, side: side, x: event.clientX, y: event.clientY,
                    closing: machine.state().active === side, locked: false };
            }
        }, true);
        listen(doc, "pointermove", function (event) {
            if (!gesture || gesture.id !== event.pointerId) return;
            var dx = event.clientX - gesture.x, dy = event.clientY - gesture.y;
            var horizontal = gesture.side === "left" || gesture.side === "right", main = Math.abs(horizontal ? dx : dy), cross = Math.abs(horizontal ? dy : dx);
            if (!gesture.locked && cross > 12 && cross >= main) { gesture = null; return; }
            if (!gesture.locked && main > 12 && main > cross * 1.5) {
                gesture.locked = true; try { gesture.start.setPointerCapture(event.pointerId); } catch (_) { /* Cancelled pointer. */ }
            }
            if (gesture && gesture.locked) event.preventDefault();
        }, { passive: false });
        listen(doc, "pointerup", function (event) {
            if (gesture && gesture.id === event.pointerId) {
                var g = gesture; gesture = null;
                if (g.locked) {
                    var distance = inward(g.side, event.clientX - g.x, event.clientY - g.y);
                    suppressClickUntil = Date.now() + 450;
                    if (!g.closing && distance >= 40) manualRequest(g.side);
                    else if (g.closing && distance <= -40) manualRequest(null);
                    event.preventDefault();
                }
            }
            pointers.delete(event.pointerId); refresh(); machine.activity();
        }, { passive: false });
        listen(doc, "pointercancel", function (event) { gesture = null; pointers.delete(event.pointerId); refresh(); machine.activity(); }, true);
        /* Presentation bridge for the server's real HTML5 drag session. It
           exposes an existing drop target; it never dispatches a command/drop.
           A new task observes the result of the original dragstart handler
           (native dispatch may run microtasks between capture and target).
           Assigned trucks stop propagation; synchronous tap DnD ends first. */
        listen(doc, "dragstart", function (event) {
            var source = event.target.closest && event.target.closest(SOURCE), epoch = ++dragEpoch;
            if (!source || !owner.contains(source)) return;
            if (dragStartTimer !== null) global.clearTimeout(dragStartTimer);
            dragStartTimer = global.setTimeout(function () {
                dragStartTimer = null;
                if (dead || epoch !== dragEpoch || event.defaultPrevented || !source.isConnected || !source.classList.contains("dispatcher-dragging")) return;
                var state = context(); if (!state.compatible || !state.shiftOpen || state.observer || state.modalOpen) return;
                nativeDrag = snapshot(source, owner); pendingReveal = null; pendingManual = null;
                setAttribute(root, "data-cr-panel-native-drag", "true"); machine.pause(true);
            }, 0);
        }, true);
        function exposeNativeTarget(event) {
            if (!nativeDrag) return;
            if (!nativeDrag.source.isConnected || !nativeDrag.source.classList.contains("dispatcher-dragging")) { finishNativeDrag(); return; }
            var side = event.clientX <= 40 ? "left" : event.clientX >= global.innerWidth - 40 ? "right" : null;
            if (side) {
                var target = nodes[side].querySelector("[data-dispatcher-drop]");
                if (canDrop(nativeDrag, target, context())) { event.preventDefault(); machine.request(side); }
            } else {
                var sourceSide = sideFor(nativeDrag.source), current = machine.state();
                if (sourceSide && current.active === sourceSide && !nodes[sourceSide].contains(event.target)) machine.request(null);
            }
        }
        listen(doc, "dragenter", exposeNativeTarget, true); listen(doc, "dragover", exposeNativeTarget, true);
        ["drop", "dragend"].forEach(function (type) { listen(doc, type, function () {
            dragEpoch += 1; Promise.resolve().then(function () { if (!dead) finishNativeDrag(); });
        }, true); });
        listen(doc, "click", click, true);
        function activity(event) {
            var active = machine.state().active;
            if (active && nodes[active].contains(event.target)) machine.activity();
            refresh();
        }
        ["click", "input", "change", "wheel", "scroll"].forEach(function (type) { listen(doc, type, activity, true); });
        listen(doc, "focusin", refresh, true);
        listen(doc, "focusout", function () { global.clearTimeout(settle); settle = global.setTimeout(refresh, 0); }, true);
        listen(doc, "keydown", function (event) {
            if (nativeDrag && event.key === "Escape") return;
            if (event.key === "Escape" && machine.state().active && !dialogOpen()) {
                stop(event); var side = machine.state().active; manualRequest(null); handles[side].focus({ preventScroll: true });
            } else if (event.key === "Enter" && event.target.matches("[data-cr-mm-search]")) {
                event.preventDefault(); updateSearch(true); event.target.blur(); refresh();
            } else activity(event);
        }, true);
        function resize() { pendingLayout = true; if (frame !== null) return; frame = global.requestAnimationFrame(function () { frame = null; refresh(); }); }
        listen(global, "resize", resize); if (global.visualViewport) listen(global.visualViewport, "resize", resize);
        listen(doc, "visibilitychange", function () { if (doc.hidden) { pointers.clear(); gesture = null; pendingReveal = null; finishNativeDrag(); machine.request(null); } refresh(); });
        var observer = new MutationObserver(refresh);
        observer.observe(doc.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["hidden", "open", "aria-hidden", "class"] });
        return {
            owner: owner, matches: function () { return owner.isConnected && nodes.top === owner.querySelector(":scope > .dispatcher-topbar") && nodes.left === owner.querySelector(":scope > .dispatcher-left") && nodes.right === owner.querySelector(":scope > .dispatcher-right") && nodes.bottom === doc.getElementById("cr-mm-toolbar"); },
            adopt: function (next) {
                var nextCanvas = next && next.closest("[data-dispatcher-canvas]");
                var replacements = next && { top: next.querySelector(":scope > .dispatcher-topbar"), left: next.querySelector(":scope > .dispatcher-left"), right: next.querySelector(":scope > .dispatcher-right") };
                if (!nextCanvas || nodes.bottom !== doc.getElementById("cr-mm-toolbar") || Object.keys(replacements).some(function (side) { return !replacements[side]; })) return false;
                ["tile-w", "tile-h"].forEach(function (suffix) {
                    setStyle(next, "--cr-panel-" + suffix, owner.style.getPropertyValue("--cr-panel-" + suffix));
                });
                Object.keys(replacements).forEach(function (side) {
                    setAttribute(nodes[side], "data-cr-panel", null); setAttribute(nodes[side], "data-cr-panel-state", null);
                    setAttribute(nodes[side], "aria-hidden", originals[side].aria); setBoolean(nodes[side], "inert", originals[side].inert);
                    nodes[side] = replacements[side];
                    var copied = nodes[side].hasAttribute("data-cr-panel");
                    originals[side] = { aria: copied ? null : nodes[side].getAttribute("aria-hidden"), inert: copied ? false : nodes[side].inert };
                    setAttribute(nodes[side], "data-cr-panel", side);
                });
                var keepInput = machine.state().active === "bottom" && (editing() || keyboardOpen());
                finishNativeDrag();
                owner = next; this.owner = next; canvas = nextCanvas; pendingReveal = null;
                next.querySelectorAll(".cr-mm-panel-backdrop").forEach(function (node) { if (node !== backdrop) node.remove(); });
                next.appendChild(backdrop);
                watchCanvas(); watchTiles(); render(machine.state());
                if (!keepInput) machine.request(null);
                refresh(); return true;
            },
            layout: function () { layout(false); },
            reveal: function (node) { pendingReveal = node ? { node: node, side: sideFor(node) } : null; refresh(); },
            selected: function (node) { if (sideFor(node)) { pendingReveal = null; machine.request(null); } },
            completeAction: function () { if (machine.state().active === "bottom") { pendingReveal = null; machine.request(null); } },
            escape: function () { return !!machine.state().active; },
            destroy: function () {
                if (dead) return; keepCanvasMode(); dead = true; machine.destroy(); observer.disconnect(); canvasObserver.disconnect(); tileObserver.disconnect(); if (tileResize) tileResize.disconnect(); listeners.forEach(function (off) { off(); });
                setAttribute(canvas, "data-dispatcher-canvas", serverCanvasMode);
                global.clearTimeout(settle); if (frame !== null) global.cancelAnimationFrame(frame); backdrop.remove();
                if (tileFrame !== null) global.cancelAnimationFrame(tileFrame);
                Object.keys(nodes).forEach(function (side) {
                    handles[side].remove(); edges[side].remove(); setAttribute(nodes[side], "data-cr-panel", null); setAttribute(nodes[side], "data-cr-panel-state", null);
                    setAttribute(nodes[side], "aria-hidden", originals[side].aria); setBoolean(nodes[side], "inert", originals[side].inert);
                });
                setClass(root, "cr-mm-panels", false); setAttribute(root, "data-cr-panel-open", null); setAttribute(root, "data-cr-panels-ready", null); setAttribute(root, "data-cr-panel-modal", null);
                nativeDrag = null; dragEpoch += 1; if (dragStartTimer !== null) global.clearTimeout(dragStartTimer); setAttribute(root, "data-cr-panel-native-drag", null);
                ["tile-w", "tile-h"].forEach(function (suffix) { owner.style.removeProperty("--cr-panel-" + suffix); });
                ["top-edge", "side-edge", "canvas-w", "canvas-h", "canvas-vw"].forEach(function (suffix) { root.style.removeProperty("--cr-panel-" + suffix); });
            }
        };
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
        if (edgePanels) edgePanels.selected(source);
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
    function fitCanvas() { if (edgePanels) edgePanels.layout(); else fitCanvasBase(); }
    function fitCanvasBase() {
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
        if (scroll && edgePanels) edgePanels.reveal(first);
        else if (scroll && first) first.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
    function reconcile() {
        queued = false; if (!allowedLocation(global.location)) return;
        compatible = isCompatible();
        if (!compatible) {
            if (edgePanels) { edgePanels.destroy(); edgePanels = null; }
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
        if (edgePanels && (edgePanels.owner !== next || !edgePanels.matches()) && !edgePanels.adopt(next)) { edgePanels.destroy(); edgePanels = null; }
        if (board !== next) { var editing = editMode; board = next; collapseRacks(); clearSelection(editing ? "Пульт обновился. При необходимости включите перестановку снова." : "", true); }
        else if (selection && (!selection.source.isConnected || (selection.source.dataset.haulAssignmentStateId || "") !== selection.assignment || !context().shiftOpen)) clearSelection("Состояние изменилось.", true);
        setClass(doc.documentElement, "cr-mm-tablet", true);
        ensureToolbar(); updateToolbar(); updateTargets(); updateSearch(false);
        if (!edgePanels) edgePanels = createEdgePanels(board);
        fitCanvas();
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
                if (edgePanels) edgePanels.completeAction();
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
            if (event.key === "Escape" && !(edgePanels && edgePanels.escape())) { if (editMode) clearSelection("", true); collapseRacks(); }
        }, true);
        doc.addEventListener("visibilitychange", function () { if (doc.hidden) { clearSelection("", true); collapseRacks(); } });
        global.addEventListener("pagehide", function () { clearSelection("", true); collapseRacks(); if (edgePanels) { edgePanels.destroy(); edgePanels = null; } });
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
            if (edgePanels) { edgePanels.destroy(); edgePanels = null; }
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
