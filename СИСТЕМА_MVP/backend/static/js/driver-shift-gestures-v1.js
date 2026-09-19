/* Удержания и жесты экрана водителя: защита действий при смене роли,
   восстановление незавершённой разгрузки и сам жест разгрузки. Вынесено из driver-shift-v1.js без изменений. */
/* DRIVER_ROLE_HOLD_GUARD_START */
window.createDriverRoleHoldGuard = function (options) {
    options = options || {};
    var holdMs = Math.max(0, Number(options.holdMs || 2000));
    var timerId = null;
    var frameId = null;
    var startedAt = 0;
    var holding = false;
    var destroyed = false;

    function roleIsReadonly() {
        return (
            typeof window.isAppRoleReadonly === "function"
            && window.isAppRoleReadonly()
        );
    }

    function clearScheduledCallbacks() {
        if (timerId !== null) {
            window.clearTimeout(timerId);
            timerId = null;
        }
        if (frameId !== null) {
            window.cancelAnimationFrame(frameId);
            frameId = null;
        }
    }

    function reset(force) {
        var hadActiveHold = holding || timerId !== null || frameId !== null;
        clearScheduledCallbacks();
        holding = false;
        startedAt = 0;
        if ((hadActiveHold || force === true) && typeof options.onReset === "function") {
            options.onReset();
        }
    }

    function drawProgress() {
        frameId = null;
        if (!holding) {
            return;
        }
        if (roleIsReadonly()) {
            reset(true);
            return;
        }
        // Most role actions retain their existing progress callback. The
        // unload dial deliberately has none, so a weak WebView has no
        // per-frame JavaScript work at all.
        if (typeof options.onProgress !== "function") {
            return;
        }
        var elapsed = Math.max(0, Date.now() - startedAt);
        var percent = Math.max(0, Math.min(100, (elapsed / holdMs) * 100));
        options.onProgress(percent);
        if (holding && percent < 100) {
            frameId = window.requestAnimationFrame(drawProgress);
        }
    }

    function complete() {
        timerId = null;
        if (!holding) {
            return;
        }
        if (roleIsReadonly()) {
            reset(true);
            return;
        }
        holding = false;
        if (frameId !== null) {
            window.cancelAnimationFrame(frameId);
            frameId = null;
        }
        if (typeof options.onProgress === "function") {
            options.onProgress(100);
        }
        if (roleIsReadonly()) {
            reset(true);
            return;
        }
        if (typeof options.onComplete === "function") {
            options.onComplete();
        }
    }

    function start() {
        reset(true);
        if (destroyed || roleIsReadonly()) {
            return false;
        }
        holding = true;
        startedAt = Date.now();
        if (typeof options.onStart === "function") {
            options.onStart();
        }
        if (roleIsReadonly()) {
            reset(true);
            return false;
        }
        // A dial that supplies no callback is deliberately CSS-only: retain the
        // completion timer, but do not even enter a requestAnimationFrame loop.
        if (typeof options.onProgress === "function") {
            drawProgress();
        }
        if (holding) {
            timerId = window.setTimeout(complete, holdMs);
        }
        return holding;
    }

    function resetOnInactiveRole(event) {
        if (event && event.detail && event.detail.active === false) {
            reset(true);
        }
    }

    window.addEventListener("active-role-state-changed", resetOnInactiveRole);

    return {
        start: start,
        reset: function () {
            reset(false);
        },
        cancel: function () {
            reset(true);
        },
        destroy: function () {
            if (destroyed) {
                return;
            }
            destroyed = true;
            window.removeEventListener("active-role-state-changed", resetOnInactiveRole);
            reset(true);
        }
    };
};
/* DRIVER_ROLE_HOLD_GUARD_END */

/* DRIVER_UNLOAD_RECOVERY_START */
window.createDriverUnloadRecovery = function (options) {
    options = options || {};
    var storagePrefix = String(options.storagePrefix || "driver-trip-unloaded:");
    var storage = options.storage || null;
    var input = options.input || null;
    var tripId = String(options.tripId || "").trim();
    var storageKey = tripId ? storagePrefix + tripId : "";
    var eventTarget = options.eventTarget || window;
    var destroyed = false;

    function storageKeys() {
        var keys = [];
        if (!storage) {
            return keys;
        }
        try {
            for (var index = 0; index < storage.length; index += 1) {
                var key = storage.key(index);
                if (key && key.indexOf(storagePrefix) === 0) {
                    keys.push(key);
                }
            }
        } catch (error) {}
        return keys;
    }

    function removeStorageKey(key) {
        if (!storage || !key) {
            return;
        }
        try {
            storage.removeItem(key);
        } catch (error) {}
    }

    function clearStaleStorage(keepKey) {
        storageKeys().forEach(function (key) {
            if (!keepKey || key !== keepKey) {
                removeStorageKey(key);
            }
        });
    }

    function readStoredActionId() {
        if (!storage || !storageKey) {
            return "";
        }
        try {
            return String(storage.getItem(storageKey) || "").trim();
        } catch (error) {
            return "";
        }
    }

    function storeActionId(actionId) {
        if (!storage || !storageKey || !actionId) {
            return;
        }
        try {
            storage.setItem(storageKey, actionId);
        } catch (error) {}
    }

    function ensureActionId() {
        if (!input || !storageKey) {
            return "";
        }
        var actionId = String(input.value || "").trim() || readStoredActionId();
        if (!actionId && typeof options.generateActionId === "function") {
            actionId = String(options.generateActionId() || "").trim();
        }
        if (actionId) {
            input.value = actionId;
            storeActionId(actionId);
        }
        return actionId;
    }

    function recoverPendingUi(event) {
        if (destroyed || (event && event.type === "pageshow" && event.persisted !== true)) {
            return;
        }
        ensureActionId();
        if (typeof options.onRecover === "function") {
            options.onRecover(event || null);
        }
    }

    clearStaleStorage(storageKey);
    if (storageKey && input) {
        ensureActionId();
        if (eventTarget && typeof eventTarget.addEventListener === "function") {
            eventTarget.addEventListener("pageshow", recoverPendingUi);
        }
    } else {
        clearStaleStorage("");
    }

    return {
        ensureActionId: ensureActionId,
        storageKey: storageKey,
        recover: recoverPendingUi,
        destroy: function () {
            if (destroyed) {
                return;
            }
            destroyed = true;
            if (eventTarget && typeof eventTarget.removeEventListener === "function") {
                eventTarget.removeEventListener("pageshow", recoverPendingUi);
            }
        }
    };
};
/* DRIVER_UNLOAD_RECOVERY_END */

/* DRIVER_UNLOAD_GESTURE_START */
window.bindDriverUnloadGesture = function (options) {
    options = options || {};
    var form = options.form;
    var button = options.button;
    var holdGuard = options.holdGuard;
    if (!form || !button || !holdGuard) {
        return null;
    }
    var activePointerId = null;
    var suppressPointerClickUntil = 0;

    function isOneTap() {
        return form.dataset.driverUnloadOneTap === "true";
    }

    function canTrigger() {
        return (
            !button.disabled
            && !document.hidden
            && (!options.canTrigger || options.canTrigger())
        );
    }

    function clearActivePointer() {
        var pointerId = activePointerId;
        activePointerId = null;
        button.classList.remove("is-touch-armed");
        if (pointerId !== null && typeof button.releasePointerCapture === "function") {
            try {
                button.releasePointerCapture(pointerId);
            } catch (error) { /* Capture may already have been released by the WebView. */ }
        }
    }

    function triggerOneTap(event) {
        if (event && typeof event.preventDefault === "function") {
            event.preventDefault();
        }
        if (!isOneTap() || !canTrigger()) {
            return false;
        }
        clearActivePointer();
        return options.onOneTap() !== false;
    }

    function onPointerDown(event) {
        if (
            !canTrigger()
            || activePointerId !== null
            || event.isPrimary === false
            || (event.button !== undefined && event.button !== 0)
        ) {
            return;
        }
        activePointerId = event.pointerId;
        try {
            button.setPointerCapture?.(event.pointerId);
        } catch (error) { /* Pointer-up/cancel still applies without capture. */ }
        if (isOneTap()) {
            event.preventDefault();
            button.classList.add("is-touch-armed");
            return;
        }
        // A completed normal hold is an application action, never a native text
        // selection gesture.  This matters on budget Android WebViews, where a
        // long press otherwise opens the selection toolbar over the dial.
        event.preventDefault();
        holdGuard.start();
    }

    function onPointerUp(event) {
        if (activePointerId === null || activePointerId !== event.pointerId) {
            return;
        }
        if (isOneTap()) {
            suppressPointerClickUntil = Date.now() + 750;
            triggerOneTap(event);
            clearActivePointer();
            return;
        }
        clearActivePointer();
        if (form.dataset.holdComplete !== "true") {
            holdGuard.reset();
        }
    }

    function cancelGesture() {
        if (activePointerId !== null) suppressPointerClickUntil = Date.now() + 750;
        clearActivePointer();
        if (form.dataset.holdComplete !== "true") {
            holdGuard.reset();
        }
    }

    function onPointerCancel(event) {
        if (activePointerId === null || activePointerId !== event.pointerId) {
            return;
        }
        /* Prevent a compatibility click from resurrecting a cancelled tap. */
        suppressPointerClickUntil = Date.now() + 750;
        cancelGesture();
    }

    function onPointerLeave(event) {
        /* Pointer capture keeps a short tap reliable even when a finger moves
           a few pixels outside the round button. A real pointercancel still
           aborts the action, while the normal hold keeps its old reset rule. */
        if (activePointerId === event.pointerId && !isOneTap() && form.dataset.holdComplete !== "true") {
            cancelGesture();
        }
    }

    function onVisibilityChange() {
        if (document.hidden || document.visibilityState === "hidden") {
            cancelGesture();
        }
    }

    function onClick(event) {
        event.preventDefault();
        /* Keyboard activation and old WebView engines may emit click without
           Pointer Events. A pointer-up compatibility click must not repeat it. */
        if (
            (event.button !== undefined && event.button !== 0)
            || (event.detail !== 0 && Date.now() < suppressPointerClickUntil)
        ) {
            return;
        }
        triggerOneTap();
    }

    function onSubmit(event) {
        if (options.canTrigger && !options.canTrigger()) {
            event.preventDefault();
            holdGuard.cancel();
            return;
        }
        if (!isOneTap() && form.dataset.holdComplete !== "true") {
            event.preventDefault();
            holdGuard.reset();
        }
    }

    button.addEventListener("pointerdown", onPointerDown);
    button.addEventListener("pointerup", onPointerUp);
    button.addEventListener("pointercancel", onPointerCancel);
    button.addEventListener("lostpointercapture", onPointerCancel);
    button.addEventListener("pointerleave", onPointerLeave);
    button.addEventListener("click", onClick);
    form.addEventListener("submit", onSubmit);
    document.addEventListener("visibilitychange", onVisibilityChange);
    window.addEventListener("blur", cancelGesture);

    return {
        triggerOneTap: triggerOneTap,
        destroy: function () {
            button.removeEventListener("pointerdown", onPointerDown);
            button.removeEventListener("pointerup", onPointerUp);
            button.removeEventListener("pointercancel", onPointerCancel);
            button.removeEventListener("lostpointercapture", onPointerCancel);
            button.removeEventListener("pointerleave", onPointerLeave);
            button.removeEventListener("click", onClick);
            form.removeEventListener("submit", onSubmit);
            document.removeEventListener("visibilitychange", onVisibilityChange);
            window.removeEventListener("blur", cancelGesture);
            cancelGesture();
        }
    };
};
/* DRIVER_UNLOAD_GESTURE_END */
