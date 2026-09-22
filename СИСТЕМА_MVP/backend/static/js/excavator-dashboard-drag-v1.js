(function (root) {
    "use strict";

    var pickupAudioContext = null;
    var controllerSequence = 0;

    function reducedMotion() {
        return Boolean(root.matchMedia && root.matchMedia("(prefers-reduced-motion: reduce)").matches);
    }

    function gestureStarted(dx, dy) {
        return Math.abs(dx) > 7 || Math.abs(dy) > 7;
    }

    function preparePickupAudio() {
        var AudioContextClass = root.AudioContext || root.webkitAudioContext;
        if (!AudioContextClass) return null;
        try {
            if (!pickupAudioContext || pickupAudioContext.state === "closed") {
                pickupAudioContext = new AudioContextClass();
            }
            if (pickupAudioContext.state === "suspended") {
                pickupAudioContext.resume().catch(function () {});
            }
            return pickupAudioContext;
        } catch (error) {
            return null;
        }
    }

    function playPickupTone() {
        var context = preparePickupAudio();
        if (!context) return;
        try {
            var startedAt = context.currentTime;
            var duration = .17;
            var oscillator = context.createOscillator();
            var gain = context.createGain();
            oscillator.type = "sine";
            oscillator.frequency.setValueAtTime(620, startedAt);
            oscillator.frequency.exponentialRampToValueAtTime(1120, startedAt + duration);
            gain.gain.setValueAtTime(.0001, startedAt);
            gain.gain.exponentialRampToValueAtTime(.13, startedAt + .018);
            gain.gain.exponentialRampToValueAtTime(.0001, startedAt + duration);
            oscillator.connect(gain);
            gain.connect(context.destination);
            oscillator.start(startedAt);
            oscillator.stop(startedAt + duration);
            oscillator.addEventListener("ended", function () {
                oscillator.disconnect();
                gain.disconnect();
            }, {once: true});
        } catch (error) {}
    }

    function playGrabFeedback(haptic) {
        if (typeof haptic === "function") {
            haptic(70, 220);
        } else if (root.navigator && typeof root.navigator.vibrate === "function") {
            try { root.navigator.vibrate(70); } catch (error) {}
        }
        playPickupTone();
    }

    function createPreview(state) {
        if (!state || !state.card || state.preview || !root.document) return;
        var rect = state.originRect;
        var preview = state.card.cloneNode(true);
        preview.classList.add("is-drag-preview", "truck-drag-preview");
        preview.classList.remove(
            "is-selected", "is-dragging", "is-load-blocked", "is-shift-pending",
            "is-no-driver", "is-inactive", "is-manual-passive",
            "is-transfer-incoming", "is-transfer-outgoing"
        );
        preview.querySelectorAll(".eo-transfer-meta, .eo-transfer-glow").forEach(function (node) {
            node.remove();
        });
        ["eoTransferDirection", "eoTransferId", "eoTransferKind", "eoTransferCreated", "eoTransferDeadline"]
            .forEach(function (key) { delete preview.dataset[key]; });
        preview.removeAttribute("id");
        preview.setAttribute("aria-hidden", "true");
        preview.tabIndex = -1;
        var glint = root.document.createElement("div");
        glint.className = "eo-truck-pickup-glint";
        preview.appendChild(glint);
        preview.style.setProperty("position", "fixed", "important");
        preview.style.left = rect.left + "px";
        preview.style.top = rect.top + "px";
        preview.style.setProperty("width", rect.width + "px", "important");
        preview.style.setProperty("height", rect.height + "px", "important");
        preview.style.setProperty("min-width", "0", "important");
        preview.style.setProperty("min-height", "0", "important");
        preview.style.setProperty("max-width", "none", "important");
        preview.style.setProperty("max-height", "none", "important");
        preview.style.setProperty("aspect-ratio", "auto", "important");
        preview.style.margin = "0";
        preview.style.pointerEvents = "none";
        preview.style.zIndex = "20000";
        preview.style.boxSizing = "border-box";
        preview.style.setProperty("border-radius", root.getComputedStyle(state.card).borderRadius, "important");
        preview.style.transformOrigin = "center center";
        root.document.body.appendChild(preview);
        state.preview = preview;
        if (!reducedMotion()) {
            var flare = root.document.createElement("div");
            flare.className = "eo-truck-pickup-flare";
            flare.setAttribute("aria-hidden", "true");
            flare.style.left = (rect.left - rect.width / 2) + "px";
            flare.style.top = (rect.top - rect.height / 2 - 8) + "px";
            flare.style.width = (rect.width * 2) + "px";
            flare.style.height = (rect.height * 2) + "px";
            root.document.body.appendChild(flare);
            state.pickupFlare = flare;
        }
        movePreview(state, 0, 0);
    }

    function movePreview(state, dx, dy) {
        if (!state || !state.preview) return;
        var reduce = reducedMotion();
        state.preview.style.transform = "translate3d(" + dx + "px, " +
            (dy - (reduce ? 0 : 8)) + "px, 0) scale(" + (reduce ? "1" : "1.18") + ")";
        var rect = state.originRect;
        var centerX = rect.left + rect.width / 2 + dx;
        var centerY = rect.top + rect.height / 2 + dy;
        var hitScale = reduce ? 1 : .9;
        state.previewHitRect = {
            left: centerX - rect.width * hitScale / 2,
            right: centerX + rect.width * hitScale / 2,
            top: centerY - rect.height * hitScale / 2,
            bottom: centerY + rect.height * hitScale / 2
        };
        if (state.comet) {
            state.comet.x = centerX;
            state.comet.y = centerY - 8;
        }
    }

    function createComet(state, gradientId) {
        if (!state || state.comet || !root.requestAnimationFrame || reducedMotion() || !root.document) return;
        function svgNode(name, attributes) {
            var node = root.document.createElementNS("http://www.w3.org/2000/svg", name);
            Object.keys(attributes || {}).forEach(function (key) { node.setAttribute(key, attributes[key]); });
            return node;
        }
        controllerSequence += 1;
        var id = String(gradientId || "eo-drag-comet-light") + "-" + controllerSequence;
        var layer = svgNode("svg", {"class": "eo-truck-comet", "aria-hidden": "true", focusable: "false"});
        var defs = svgNode("defs");
        var gradient = svgNode("linearGradient", {id: id, gradientUnits: "userSpaceOnUse"});
        [["0%", "#17cfff", "0"], ["40%", "#43eaff", ".65"], ["100%", "#efffff", "1"]]
            .forEach(function (stop) {
                gradient.appendChild(svgNode("stop", {
                    offset: stop[0], "stop-color": stop[1], "stop-opacity": stop[2]
                }));
            });
        defs.appendChild(gradient);
        layer.appendChild(defs);
        var paths = [24, 10, 3].map(function (width, index) {
            var path = svgNode("path", {
                fill: "none", stroke: "url(#" + id + ")", "stroke-width": width,
                "stroke-linecap": "round", "stroke-linejoin": "round", opacity: [.22, .55, 1][index]
            });
            if (!index) path.style.filter = "blur(5px)";
            layer.appendChild(path);
            return path;
        });
        var rect = state.originRect;
        var comet = {
            layer: layer, paths: paths, gradient: gradient, frame: null, lastMoved: 0,
            x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 - 8
        };
        comet.lastX = comet.x;
        comet.lastY = comet.y;
        comet.points = [{x: comet.x, y: comet.y}];
        root.document.body.appendChild(layer);
        state.comet = comet;
        function animate(now) {
            if (state.comet !== comet) return;
            var dx = comet.x - comet.lastX;
            var dy = comet.y - comet.lastY;
            var distance = Math.hypot(dx, dy);
            if (distance >= 5) {
                if (comet.points.length === 1) {
                    var reach = (rect.width * Math.abs(dx / distance) + rect.height * Math.abs(dy / distance)) * .59 + 60;
                    comet.points[0] = {
                        x: comet.lastX - dx / distance * reach,
                        y: comet.lastY - dy / distance * reach
                    };
                }
                comet.points.push({x: comet.x, y: comet.y});
                if (comet.points.length > 48) comet.points.shift();
                var remaining = 240;
                for (var i = comet.points.length - 1; i > 0; i -= 1) {
                    var head = comet.points[i];
                    var tail = comet.points[i - 1];
                    var length = Math.hypot(head.x - tail.x, head.y - tail.y);
                    if (length >= remaining) {
                        comet.points[i - 1] = {
                            x: head.x + (tail.x - head.x) * remaining / length,
                            y: head.y + (tail.y - head.y) * remaining / length
                        };
                        comet.points.splice(0, i - 1);
                        break;
                    }
                    remaining -= length;
                }
                var points = comet.points;
                var d = "M " + points[0].x + " " + points[0].y;
                for (var j = 1; j < points.length - 1; j += 1) {
                    d += " Q " + points[j].x + " " + points[j].y + " " +
                        (points[j].x + points[j + 1].x) / 2 + " " + (points[j].y + points[j + 1].y) / 2;
                }
                d += " L " + comet.x + " " + comet.y;
                paths.forEach(function (path) { path.setAttribute("d", d); });
                gradient.setAttribute("x1", points[0].x);
                gradient.setAttribute("y1", points[0].y);
                gradient.setAttribute("x2", comet.x + .01);
                gradient.setAttribute("y2", comet.y + .01);
                comet.lastX = comet.x;
                comet.lastY = comet.y;
                comet.lastMoved = now;
            }
            layer.style.opacity = String(Math.max(0, 1 - (now - comet.lastMoved) / 700));
            comet.frame = root.requestAnimationFrame(animate);
        }
        comet.frame = root.requestAnimationFrame(animate);
    }

    function removePreview(state) {
        if (!state) return;
        if (state.pickupFlare) {
            state.pickupFlare.remove();
            state.pickupFlare = null;
        }
        if (state.comet) {
            root.cancelAnimationFrame(state.comet.frame);
            state.comet.layer.remove();
            state.comet = null;
        }
        if (state.preview) state.preview.remove();
        state.preview = null;
    }

    function findIntersectingTarget(state, shell, selector) {
        if (!state || !state.preview || !state.previewHitRect || !shell) return null;
        var previewRect = state.previewHitRect;
        var previewCenterX = (previewRect.left + previewRect.right) / 2;
        var previewCenterY = (previewRect.top + previewRect.bottom) / 2;
        var bestTarget = null;
        var bestScore = -Infinity;
        shell.querySelectorAll(selector).forEach(function (target) {
            var targetRect = target.getBoundingClientRect();
            var overlapX = Math.min(previewRect.right, targetRect.right) - Math.max(previewRect.left, targetRect.left);
            var overlapY = Math.min(previewRect.bottom, targetRect.bottom) - Math.max(previewRect.top, targetRect.top);
            if (overlapX < -2 || overlapY < -2) return;
            var targetCenterX = (targetRect.left + targetRect.right) / 2;
            var targetCenterY = (targetRect.top + targetRect.bottom) / 2;
            var centerDistance = Math.hypot(previewCenterX - targetCenterX, previewCenterY - targetCenterY);
            var score = Math.max(0, overlapX) * Math.max(0, overlapY) - centerDistance * .01;
            if (score > bestScore) {
                bestScore = score;
                bestTarget = target;
            }
        });
        return bestTarget;
    }

    function attach(options) {
        options = options || {};
        var shell = options.shell;
        if (!shell) return null;
        var sourceSelector = options.sourceSelector || "[data-eo-truck-card]";
        var targetSelector = options.targetSelector || "[data-eo-dump-target]";
        var manualHoldMs = Number(options.manualHoldMs || 290);
        var detailHoldMs = Number(options.detailHoldMs || 580);
        var activeDrag = null;
        var destroyed = false;

        function call(name, fallback) {
            if (typeof options[name] === "function") {
                return options[name].apply(null, Array.prototype.slice.call(arguments, 2));
            }
            return fallback;
        }
        function canDrag(card) { return Boolean(call("canDrag", card.dataset.eoCanLoad === "1", card)); }
        function isManual(card) { return Boolean(call("isManual", card.dataset.eoManualAvailable === "1", card)); }
        function isInactive(card) { return Boolean(call("isInactive", card.dataset.eoTruckInactive === "1", card)); }
        function isBlocked(card) { return Boolean(call("isBlocked", card.classList.contains("is-load-blocked"), card)); }
        function clearDropReady() {
            shell.querySelectorAll(targetSelector + ".is-drop-ready").forEach(function (target) {
                target.classList.remove("is-drop-ready");
            });
            shell.classList.remove("is-truck-drag-active");
        }
        function setTarget(target) {
            shell.classList.toggle("is-truck-drag-active", Boolean(target));
            shell.querySelectorAll(targetSelector).forEach(function (item) {
                item.classList.toggle("is-drop-ready", item === target);
            });
        }
        function specialCancel(state, dx, dy) {
            return Boolean(call("isSpecialCancel", false, state, dx, dy));
        }
        function cancel(pointerId) {
            if (!activeDrag || (pointerId != null && activeDrag.pointerId !== pointerId)) return;
            var state = activeDrag;
            activeDrag = null;
            state.card.classList.remove(
                "is-dragging", "is-picked-up", "is-pickup-waiting", "is-free-bucket-cancel-armed"
            );
            state.card.style.transform = "";
            removePreview(state);
            clearDropReady();
        }
        function update(event) {
            if (!activeDrag || event.pointerId !== activeDrag.pointerId) return;
            if (activeDrag.longPressOpened || activeDrag.waitingForHold) return;
            var dx = event.clientX - activeDrag.startX;
            var dy = event.clientY - activeDrag.startY;
            if (!activeDrag.started && !gestureStarted(dx, dy)) return;
            event.preventDefault();
            if (!activeDrag.started) {
                activeDrag.started = true;
                activeDrag.card.classList.add("is-dragging");
                createPreview(activeDrag);
                createComet(activeDrag, options.gradientId || "eo-drag-comet-light");
            }
            movePreview(activeDrag, dx, dy);
            var cancelling = specialCancel(activeDrag, dx, dy);
            activeDrag.card.classList.toggle("is-free-bucket-cancel-armed", cancelling);
            if (activeDrag.preview) activeDrag.preview.classList.toggle("is-free-bucket-cancel-armed", cancelling);
            if (cancelling) {
                clearDropReady();
                activeDrag.target = null;
                return;
            }
            var target = findIntersectingTarget(activeDrag, shell, targetSelector);
            if (target && target !== activeDrag.target) {
                call("onTargetChange", undefined, activeDrag.card, target, activeDrag);
            }
            setTarget(target);
            activeDrag.target = target;
        }
        function finish(event) {
            if (!activeDrag || event.pointerId !== activeDrag.pointerId) return;
            var state = activeDrag;
            activeDrag = null;
            if (state.card.releasePointerCapture) {
                try { state.card.releasePointerCapture(state.pointerId); } catch (error) {}
            }
            var dx = Number.isFinite(event.clientX) ? event.clientX - state.startX : 0;
            var dy = Number.isFinite(event.clientY) ? event.clientY - state.startY : 0;
            var cancelling = specialCancel(state, dx, dy);
            state.card.classList.remove("is-dragging", "is-free-bucket-cancel-armed");
            state.card.style.transform = "";
            removePreview(state);
            clearDropReady();
            if (state.longPressOpened) {
                event.preventDefault();
                return;
            }
            if (!state.started) {
                state.card.classList.remove("is-picked-up", "is-pickup-waiting");
                return;
            }
            event.preventDefault();
            state.card.dataset.eoSuppressClick = "1";
            root.setTimeout(function () { delete state.card.dataset.eoSuppressClick; }, 0);
            if (cancelling) {
                call("onSpecialCancel", undefined, state.card, state);
            } else if (state.target && canDrag(state.card)) {
                call("onSelect", undefined, state.card);
                call("onDrop", undefined, state.card, state.target);
            }
            state.card.classList.remove("is-picked-up", "is-pickup-waiting");
        }
        function bind(card) {
            if (!card || destroyed || card.dataset.eoSharedDragBound === "1") return;
            card.dataset.eoSharedDragBound = "1";
            var holdTimer = null;
            var holdPointerId = null;
            var holdStartX = 0;
            var holdStartY = 0;
            var detailOpened = false;
            function clearHold() {
                if (holdTimer) {
                    root.clearTimeout(holdTimer);
                    holdTimer = null;
                }
            }
            function suppressClick() {
                card.dataset.eoSuppressClick = "1";
                root.setTimeout(function () { delete card.dataset.eoSuppressClick; }, 480);
            }
            card.addEventListener("click", function (event) {
                if (card.dataset.eoSuppressClick === "1") {
                    event.preventDefault();
                    return;
                }
                if (isInactive(card)) {
                    event.preventDefault();
                    return;
                }
                if (isBlocked(card)) {
                    event.preventDefault();
                    call("onOpenDetail", undefined, card);
                    return;
                }
                call("onSelect", undefined, card);
            });
            card.addEventListener("pointerdown", function (event) {
                if (card.disabled || card.classList.contains("is-pending") || activeDrag) return;
                if (event.button !== undefined && event.button !== 0) return;
                holdPointerId = event.pointerId;
                holdStartX = event.clientX;
                holdStartY = event.clientY;
                detailOpened = false;
                clearHold();
                var manual = isManual(card);
                var loadable = canDrag(card);
                if (manual || loadable) preparePickupAudio();
                function lift() {
                    card.classList.remove("is-pickup-waiting");
                    card.classList.add("is-picked-up");
                    if (activeDrag) activeDrag.waitingForHold = false;
                    createPreview(activeDrag);
                    playGrabFeedback(options.haptic);
                }
                if (!manual && !loadable) {
                    holdTimer = root.setTimeout(function () {
                        detailOpened = Boolean(call("onOpenDetail", false, card));
                        if (detailOpened) suppressClick();
                    }, detailHoldMs);
                    return;
                }
                activeDrag = {
                    card: card, pointerId: event.pointerId,
                    originRect: card.getBoundingClientRect(),
                    startX: event.clientX, startY: event.clientY,
                    started: false, target: null, longPressOpened: false,
                    waitingForHold: manual
                };
                if (manual) {
                    card.classList.add("is-pickup-waiting");
                    holdTimer = root.setTimeout(function () {
                        if (holdPointerId !== event.pointerId || !activeDrag) return;
                        lift();
                        suppressClick();
                    }, manualHoldMs);
                } else {
                    lift();
                    call("onSelect", undefined, card);
                }
                if (card.setPointerCapture) {
                    try { card.setPointerCapture(event.pointerId); } catch (error) {}
                }
            });
            card.addEventListener("pointermove", function (event) {
                if (holdPointerId === event.pointerId) {
                    var dx = event.clientX - holdStartX;
                    var dy = event.clientY - holdStartY;
                    if (Math.hypot(dx, dy) > 9) {
                        clearHold();
                        if (activeDrag && activeDrag.waitingForHold) cancel(event.pointerId);
                    }
                }
                update(event);
            });
            card.addEventListener("pointerup", function (event) {
                if (holdPointerId === event.pointerId) {
                    clearHold();
                    holdPointerId = null;
                    if (detailOpened) {
                        event.preventDefault();
                        event.stopPropagation();
                    }
                }
                finish(event);
                detailOpened = false;
            });
            card.addEventListener("pointercancel", function (event) {
                if (holdPointerId === event.pointerId) {
                    clearHold();
                    holdPointerId = null;
                }
                cancel(event.pointerId);
                detailOpened = false;
            });
            card.addEventListener("lostpointercapture", function (event) {
                clearHold();
                holdPointerId = null;
                cancel(event.pointerId);
            });
            card.addEventListener("pointerleave", function (event) {
                if (holdPointerId === event.pointerId && !activeDrag) {
                    clearHold();
                    holdPointerId = null;
                    detailOpened = false;
                }
            });
            card.addEventListener("dragstart", function (event) {
                clearHold();
                event.preventDefault();
                event.stopPropagation();
            });
        }
        function preventNativeDrag(event) {
            if (event.target && event.target.closest(sourceSelector)) event.preventDefault();
        }
        shell.addEventListener("dragstart", preventNativeDrag, true);
        function bindAll() { shell.querySelectorAll(sourceSelector).forEach(bind); }
        function destroy() {
            destroyed = true;
            cancel();
            shell.removeEventListener("dragstart", preventNativeDrag, true);
        }
        bindAll();
        return {bind: bind, bindAll: bindAll, cancel: cancel, destroy: destroy, active: function () { return activeDrag; }};
    }

    var api = {
        attach: attach,
        gestureStarted: gestureStarted,
        createPreview: createPreview,
        movePreview: movePreview,
        createComet: createComet,
        removePreview: removePreview,
        findIntersectingTarget: findIntersectingTarget,
        preparePickupAudio: preparePickupAudio,
        playGrabFeedback: playGrabFeedback
    };
    root.ExcavatorDashboardDrag = api;
    if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
