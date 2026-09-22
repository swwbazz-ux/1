(function (root) {
    "use strict";

    var RETURN_DISTANCE = 48;
    var MOVE_DISTANCE = 5;
    var HOLD_CANCEL_DISTANCE = 8;
    var RETURN_DOMINANCE = 1.25;
    var REBOUND_DURATION_MS = 860;
    var CLICK_SUPPRESS_MS = 450;

    function isDumpReturnSwipe(deltaX, deltaY) {
        return deltaY <= -RETURN_DISTANCE
            && Math.abs(deltaY) >= Math.max(RETURN_DISTANCE, Math.abs(deltaX) * RETURN_DOMINANCE);
    }

    function isDumpCompleteSwipe(deltaX, deltaY) {
        return deltaY >= RETURN_DISTANCE
            && Math.abs(deltaY) >= Math.max(RETURN_DISTANCE, Math.abs(deltaX) * RETURN_DOMINANCE);
    }

    function rubberBandDumpReturnOffset(value, limit, directRatio, overflowRatio) {
        var direction = value < 0 ? -1 : 1;
        var distance = Math.abs(Number(value) || 0);
        var directDistance = Math.min(distance, 56) * directRatio;
        var resistedDistance = Math.max(0, distance - 56) * overflowRatio;
        return direction * Math.min(limit, directDistance + resistedDistance);
    }

    function updateDumpReturnElastic(target, swipe) {
        if (!target || !swipe) return;
        var elasticX = rubberBandDumpReturnOffset(swipe.deltaX, 56, .64, .15);
        var elasticY = rubberBandDumpReturnOffset(swipe.deltaY, 72, .84, .18);
        var tilt = Math.max(-4.5, Math.min(4.5, elasticX / 11));
        var stretchX = 1 + Math.min(.075, Math.abs(elasticX) / 760 + Math.abs(elasticY) / 2200);
        var stretchY = 1 + Math.min(.105, Math.abs(elasticY) / 620 + Math.abs(elasticX) / 2400);
        swipe.elasticX = elasticX;
        swipe.elasticY = elasticY;
        swipe.elasticTilt = tilt;
        swipe.elasticStretchX = stretchX;
        swipe.elasticStretchY = stretchY;
        target.style.setProperty("--eo-return-drag-x", elasticX.toFixed(2) + "px");
        target.style.setProperty("--eo-return-drag-y", elasticY.toFixed(2) + "px");
        target.style.setProperty("--eo-return-tilt", tilt.toFixed(2) + "deg");
        target.style.setProperty("--eo-return-stretch-x", stretchX.toFixed(4));
        target.style.setProperty("--eo-return-stretch-y", stretchY.toFixed(4));
        target.style.setProperty("--eo-return-origin-x", elasticX > 4 ? "0%" : (elasticX < -4 ? "100%" : "50%"));
        target.style.setProperty("--eo-return-origin-y", elasticY > 4 ? "0%" : (elasticY < -4 ? "100%" : "50%"));
        target.style.setProperty("--eo-return-tether-x", (-elasticX * .52).toFixed(2) + "px");
        target.style.setProperty("--eo-return-tether-y", (-elasticY * .52).toFixed(2) + "px");
        target.style.setProperty("--eo-return-anchor-x", (-elasticX).toFixed(2) + "px");
        target.style.setProperty("--eo-return-anchor-y", (-elasticY).toFixed(2) + "px");
    }

    function setDumpReturnReleaseVector(target, swipe) {
        var x = swipe && Number.isFinite(swipe.elasticX) ? swipe.elasticX : 0;
        var y = swipe && Number.isFinite(swipe.elasticY) ? swipe.elasticY : 0;
        var tilt = swipe && Number.isFinite(swipe.elasticTilt) ? swipe.elasticTilt : 0;
        var stretchX = swipe && Number.isFinite(swipe.elasticStretchX) ? swipe.elasticStretchX : 1;
        var stretchY = swipe && Number.isFinite(swipe.elasticStretchY) ? swipe.elasticStretchY : 1;
        var vectors = [
            ["--eo-return-release-x", x, "px"],
            ["--eo-return-release-y", y, "px"],
            ["--eo-return-release-tilt", tilt, "deg"],
            ["--eo-return-bounce-1-x", -x * .28, "px"],
            ["--eo-return-bounce-1-y", -y * .28, "px"],
            ["--eo-return-bounce-1-tilt", -tilt * .38, "deg"],
            ["--eo-return-bounce-2-x", x * .15, "px"],
            ["--eo-return-bounce-2-y", y * .15, "px"],
            ["--eo-return-bounce-2-tilt", tilt * .2, "deg"],
            ["--eo-return-bounce-3-x", -x * .075, "px"],
            ["--eo-return-bounce-3-y", -y * .075, "px"],
            ["--eo-return-bounce-3-tilt", -tilt * .1, "deg"],
            ["--eo-return-bounce-4-x", x * .03, "px"],
            ["--eo-return-bounce-4-y", y * .03, "px"],
            ["--eo-return-bounce-4-tilt", tilt * .04, "deg"]
        ];
        vectors.forEach(function (vector) {
            target.style.setProperty(vector[0], vector[1].toFixed(2) + vector[2]);
        });
        target.style.setProperty("--eo-return-release-stretch-x", stretchX.toFixed(4));
        target.style.setProperty("--eo-return-release-stretch-y", stretchY.toFixed(4));
    }

    function clearDumpReturnReleaseVector(target) {
        [
            "--eo-return-release-x", "--eo-return-release-y", "--eo-return-release-tilt",
            "--eo-return-release-stretch-x", "--eo-return-release-stretch-y",
            "--eo-return-bounce-1-x", "--eo-return-bounce-1-y", "--eo-return-bounce-1-tilt",
            "--eo-return-bounce-2-x", "--eo-return-bounce-2-y", "--eo-return-bounce-2-tilt",
            "--eo-return-bounce-3-x", "--eo-return-bounce-3-y", "--eo-return-bounce-3-tilt",
            "--eo-return-bounce-4-x", "--eo-return-bounce-4-y", "--eo-return-bounce-4-tilt"
        ].forEach(function (propertyName) {
            target.style.removeProperty(propertyName);
        });
    }

    function playDumpReturnRebound(target, swipe, timerRoot) {
        if (!target) return;
        timerRoot = timerRoot || root;
        target.classList.remove("is-return-rebounding");
        setDumpReturnReleaseVector(target, swipe);
        void target.offsetWidth;
        target.classList.add("is-return-rebounding");
        timerRoot.setTimeout(function () {
            target.classList.remove("is-return-rebounding");
            clearDumpReturnReleaseVector(target);
        }, REBOUND_DURATION_MS);
    }

    function attach(options) {
        options = options || {};
        var shell = options.shell;
        if (!shell || typeof shell.querySelectorAll !== "function") return null;
        var targetSelector = String(options.targetSelector || "[data-eo-dump-target]");
        var bindings = [];
        var destroyed = false;

        function bindTarget(target) {
            if (!target || target.__eoDumpReturnSwipeBinding) return target && target.__eoDumpReturnSwipeBinding;
            var queueHoldTimer = null;
            var dumpSwipe = null;

            function clearQueueHold() {
                if (!queueHoldTimer) return;
                root.clearTimeout(queueHoldTimer);
                queueHoldTimer = null;
            }

            function clearDumpSwipe() {
                clearQueueHold();
                target.classList.remove("is-return-swiping", "is-return-armed", "is-complete-armed");
                [
                    "--eo-return-swipe-progress", "--eo-return-drag-x", "--eo-return-drag-y",
                    "--eo-return-tilt", "--eo-return-stretch-x", "--eo-return-stretch-y",
                    "--eo-return-origin-x", "--eo-return-origin-y",
                    "--eo-return-tether-x", "--eo-return-tether-y",
                    "--eo-return-anchor-x", "--eo-return-anchor-y"
                ].forEach(function (propertyName) {
                    target.style.removeProperty(propertyName);
                });
                var releasedSwipe = dumpSwipe;
                dumpSwipe = null;
                if (releasedSwipe && target.releasePointerCapture) {
                    try { target.releasePointerCapture(releasedSwipe.pointerId); } catch (error) {}
                }
                return releasedSwipe;
            }

            function suppressClick() {
                target.dataset.eoSuppressClick = "1";
                root.setTimeout(function () { delete target.dataset.eoSuppressClick; }, CLICK_SUPPRESS_MS);
            }

            function onPointerDown(event) {
                if (event.button !== undefined && event.button !== 0) return;
                if (
                    target.classList.contains("is-return-pending")
                    || shell.classList.contains("is-truck-drag-active")
                    || (typeof options.canStart === "function" && !options.canStart(target, event))
                ) return;
                dumpSwipe = {
                    pointerId: event.pointerId,
                    startX: event.clientX,
                    startY: event.clientY,
                    deltaX: 0,
                    deltaY: 0,
                    moved: false,
                    armedDirection: ""
                };
                if (target.setPointerCapture) {
                    try { target.setPointerCapture(event.pointerId); } catch (error) {}
                }
                clearQueueHold();
                if (typeof options.onHold === "function" && Number(options.holdMs) > 0) {
                    queueHoldTimer = root.setTimeout(function () {
                        target.dataset.eoSuppressClick = "1";
                        options.onHold(target, event);
                        clearDumpSwipe();
                        root.setTimeout(function () { delete target.dataset.eoSuppressClick; }, CLICK_SUPPRESS_MS);
                    }, Number(options.holdMs));
                }
            }

            function onPointerMove(event) {
                if (!dumpSwipe || event.pointerId !== dumpSwipe.pointerId) return;
                dumpSwipe.deltaX = event.clientX - dumpSwipe.startX;
                dumpSwipe.deltaY = event.clientY - dumpSwipe.startY;
                if (Math.abs(dumpSwipe.deltaX) > HOLD_CANCEL_DISTANCE || Math.abs(dumpSwipe.deltaY) > HOLD_CANCEL_DISTANCE) {
                    clearQueueHold();
                }
                var distance = Math.hypot(dumpSwipe.deltaX, dumpSwipe.deltaY);
                if (distance < MOVE_DISTANCE) {
                    target.classList.remove("is-return-swiping", "is-return-armed", "is-complete-armed");
                    target.style.removeProperty("--eo-return-swipe-progress");
                    return;
                }
                event.preventDefault();
                dumpSwipe.moved = true;
                var progress = Math.min(1, Math.abs(dumpSwipe.deltaY) / 56);
                target.style.setProperty("--eo-return-swipe-progress", String(progress));
                updateDumpReturnElastic(target, dumpSwipe);
                target.classList.add("is-return-swiping");
                var returnArmed = isDumpReturnSwipe(dumpSwipe.deltaX, dumpSwipe.deltaY);
                var completeArmed = typeof options.onComplete === "function"
                    && isDumpCompleteSwipe(dumpSwipe.deltaX, dumpSwipe.deltaY);
                var armedDirection = returnArmed ? "return" : (completeArmed ? "complete" : "");
                target.classList.toggle("is-return-armed", returnArmed);
                target.classList.toggle("is-complete-armed", completeArmed);
                if (armedDirection && armedDirection !== dumpSwipe.armedDirection && typeof options.onArm === "function") {
                    options.onArm(target, armedDirection, dumpSwipe, event);
                }
                dumpSwipe.armedDirection = armedDirection;
            }

            function onPointerUp(event) {
                if (!dumpSwipe || event.pointerId !== dumpSwipe.pointerId) return;
                if (Number.isFinite(event.clientX) && Number.isFinite(event.clientY)) {
                    dumpSwipe.deltaX = event.clientX - dumpSwipe.startX;
                    dumpSwipe.deltaY = event.clientY - dumpSwipe.startY;
                }
                var shouldReturn = isDumpReturnSwipe(dumpSwipe.deltaX, dumpSwipe.deltaY);
                var shouldComplete = typeof options.onComplete === "function"
                    && isDumpCompleteSwipe(dumpSwipe.deltaX, dumpSwipe.deltaY);
                if (shouldReturn || shouldComplete) {
                    event.preventDefault();
                    target.dataset.eoSuppressClick = "1";
                }
                updateDumpReturnElastic(target, dumpSwipe);
                dumpSwipe.moved = dumpSwipe.moved || Math.hypot(dumpSwipe.deltaX, dumpSwipe.deltaY) >= MOVE_DISTANCE;
                var releasedSwipe = clearDumpSwipe();
                if (releasedSwipe && releasedSwipe.moved) {
                    event.preventDefault();
                    suppressClick();
                    playDumpReturnRebound(target, releasedSwipe, root);
                }
                if (shouldReturn && typeof options.onReturn === "function") options.onReturn(target, releasedSwipe, event);
                if (shouldComplete) options.onComplete(target, releasedSwipe, event);
            }

            function onPointerCancel() {
                var releasedSwipe = clearDumpSwipe();
                if (releasedSwipe && releasedSwipe.moved) playDumpReturnRebound(target, releasedSwipe, root);
            }

            function onLostPointerCapture() {
                if (!dumpSwipe) return;
                onPointerCancel();
            }

            function onPointerLeave() { clearQueueHold(); }

            var listeners = [
                ["pointerdown", onPointerDown],
                ["pointermove", onPointerMove],
                ["pointerup", onPointerUp],
                ["pointercancel", onPointerCancel],
                ["lostpointercapture", onLostPointerCapture],
                ["pointerleave", onPointerLeave]
            ];
            listeners.forEach(function (entry) { target.addEventListener(entry[0], entry[1]); });
            var binding = {
                target: target,
                cancel: function () {
                    var releasedSwipe = clearDumpSwipe();
                    if (releasedSwipe && releasedSwipe.moved) playDumpReturnRebound(target, releasedSwipe, root);
                },
                destroy: function () {
                    clearDumpSwipe();
                    listeners.forEach(function (entry) { target.removeEventListener(entry[0], entry[1]); });
                    if (target.__eoDumpReturnSwipeBinding === binding) delete target.__eoDumpReturnSwipeBinding;
                }
            };
            target.__eoDumpReturnSwipeBinding = binding;
            bindings.push(binding);
            return binding;
        }

        function bindAll() {
            if (destroyed) return [];
            var targets = Array.prototype.slice.call(shell.querySelectorAll(targetSelector));
            targets.forEach(bindTarget);
            bindings = bindings.filter(function (binding) {
                if (targets.indexOf(binding.target) >= 0) return true;
                binding.destroy();
                return false;
            });
            return targets;
        }

        function cancel() { bindings.forEach(function (binding) { binding.cancel(); }); }
        function destroy() {
            if (destroyed) return;
            destroyed = true;
            bindings.slice().forEach(function (binding) { binding.destroy(); });
            bindings = [];
        }

        bindAll();
        return {bindAll: bindAll, cancel: cancel, destroy: destroy};
    }

    var api = {
        attach: attach,
        isDumpReturnSwipe: isDumpReturnSwipe,
        isDumpCompleteSwipe: isDumpCompleteSwipe,
        rubberBandDumpReturnOffset: rubberBandDumpReturnOffset,
        updateDumpReturnElastic: updateDumpReturnElastic,
        setDumpReturnReleaseVector: setDumpReturnReleaseVector,
        clearDumpReturnReleaseVector: clearDumpReturnReleaseVector,
        playDumpReturnRebound: playDumpReturnRebound
    };
    root.ExcavatorDumpReturnSwipe = api;
    if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
