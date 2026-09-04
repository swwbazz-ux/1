(function () {
    "use strict";

    function setLabel(button, text) {
        var label = button.querySelector("[data-mobile-shift-label]");
        if (label) label.textContent = text;
        else button.textContent = text;
    }

    function bind(button, options) {
        options = options || {};
        if (!button || button.dataset.mobileShiftHoldBound === "true") return null;
        button.dataset.mobileShiftHoldBound = "true";

        var readyLabel = options.readyLabel || button.textContent.trim();
        var holdLabel = options.holdLabel || "Держите";
        var timer = null;
        var frame = null;
        var startedAt = 0;
        var completed = false;
        var blockedClick = false;

        function holdMs() {
            var value = typeof options.holdMs === "function" ? options.holdMs() : options.holdMs;
            return Math.max(250, Number(value) || 2000);
        }

        function interactable() {
            return !button.disabled && !button.hidden && !button.classList.contains("is-pending");
        }

        function allowed() {
            if (!interactable() || button.getAttribute("aria-disabled") === "true") return false;
            return typeof options.canStart !== "function" || options.canStart() !== false;
        }

        function reportBlocked(event) {
            if (event) event.preventDefault();
            if (!interactable()) return;
            blockedClick = true;
            if (typeof options.onBlockedPress === "function") options.onBlockedPress();
        }

        function setProgress(value) {
            button.style.setProperty("--mobile-shift-hold", Math.max(0, Math.min(100, value)) + "%");
        }

        function clearClock() {
            window.clearTimeout(timer);
            if (frame) window.cancelAnimationFrame(frame);
            timer = null;
            frame = null;
            startedAt = 0;
            button.classList.remove("is-holding");
        }

        function reset() {
            clearClock();
            completed = false;
            setProgress(0);
            if (!button.classList.contains("is-pending")) setLabel(button, readyLabel);
        }

        function draw() {
            if (!startedAt) return;
            var value = ((Date.now() - startedAt) / holdMs()) * 100;
            setProgress(value);
            if (value < 100) frame = window.requestAnimationFrame(draw);
        }

        function finish() {
            if (!allowed()) {
                reset();
                reportBlocked();
                return;
            }
            setProgress(100);
            clearClock();
            completed = true;
            if (typeof options.onComplete === "function") {
                options.onComplete({ reset: reset, setLabel: function (text) { setLabel(button, text); } });
            }
        }

        function start(event) {
            if (startedAt || !interactable()) return;
            if (!allowed()) {
                reportBlocked(event);
                return;
            }
            if (event) event.preventDefault();
            var active = document.activeElement;
            if (
                active
                && active !== button
                && active.matches
                && active.matches("input, textarea, select, [contenteditable='true'], [contenteditable='']")
                && typeof active.blur === "function"
            ) {
                active.blur();
            }
            blockedClick = false;
            startedAt = Date.now();
            button.classList.add("is-holding");
            setLabel(button, holdLabel);
            setProgress(0);
            draw();
            timer = window.setTimeout(finish, holdMs());
        }

        function cancel(event) {
            if (event) event.preventDefault();
            if (startedAt && !completed) reset();
        }

        button.addEventListener("pointerdown", start);
        ["pointerup", "pointercancel", "pointerleave"].forEach(function (name) {
            button.addEventListener(name, cancel);
        });
        button.addEventListener("click", function (event) {
            event.preventDefault();
            if (blockedClick) {
                blockedClick = false;
                return;
            }
            if (completed) {
                completed = false;
                return;
            }
            if (!interactable()) return;
            if (!allowed()) {
                reportBlocked();
                blockedClick = false;
                return;
            }
            if (typeof options.onShortPress === "function") options.onShortPress();
        });
        button.addEventListener("keydown", function (event) {
            if ((event.key === "Enter" || event.key === " ") && !event.repeat) start(event);
        });
        button.addEventListener("keyup", function (event) {
            if (event.key === "Enter" || event.key === " ") cancel(event);
        });

        return { reset: reset, setLabel: function (text) { setLabel(button, text); } };
    }

    function keepWholeNumber(input) {
        var raw = String(input.value || "");
        var match = raw.match(/^\d+/);
        var next = match ? match[0] : "";
        if (raw !== next) input.value = next;
    }

    function focusWithoutScroll(element) {
        try {
            element.focus({ preventScroll: true });
        } catch (error) {
            element.focus();
        }
    }

    function hideVirtualKeyboard() {
        var nativeKeyboard = window.Capacitor
            && window.Capacitor.Plugins
            && window.Capacitor.Plugins.NativeKeyboard;
        if (nativeKeyboard && typeof nativeKeyboard.hide === "function") {
            try {
                var nativeResult = nativeKeyboard.hide();
                if (nativeResult && typeof nativeResult.catch === "function") {
                    nativeResult.catch(function () {});
                }
            } catch (error) {}
        }

        var keyboard = window.navigator && window.navigator.virtualKeyboard;
        if (!keyboard || typeof keyboard.hide !== "function") return;
        try {
            keyboard.hide();
        } catch (error) {}
    }

    function focusShiftAction(component, input) {
        var action = component.querySelector(
            "[data-driver-shift-open-button], [data-driver-shift-close-button], [data-eo-shift-button]"
        );
        var focusTarget = action && !action.disabled && !action.hidden && !action.classList.contains("is-pending")
            ? action
            : component;
        if (focusTarget === component && !component.hasAttribute("tabindex")) {
            component.setAttribute("tabindex", "-1");
        }

        input.blur();
        window.requestAnimationFrame(function () {
            focusWithoutScroll(focusTarget);
            hideVirtualKeyboard();
            if (focusTarget !== action) return;

            action.classList.add("is-keyboard-target");
            var clearKeyboardTarget = function () {
                action.classList.remove("is-keyboard-target");
            };
            action.addEventListener("blur", clearKeyboardTarget, { once: true });
            action.addEventListener("pointerdown", clearKeyboardTarget, { once: true });
        });
    }

    function bindFieldNavigation(component) {
        if (!component || component.dataset.mobileShiftFieldsBound === "true") return;
        component.dataset.mobileShiftFieldsBound = "true";
        var fields = Array.prototype.filter.call(
            component.querySelectorAll(".mobile-shift__metric-value input"),
            function (input) { return !input.disabled && !input.readOnly; }
        );
        var navigationLocked = false;

        function advanceField(input, index, event) {
            if (event && event.preventDefault) event.preventDefault();
            if (navigationLocked) return;
            navigationLocked = true;
            window.setTimeout(function () { navigationLocked = false; }, 180);

            var next = fields[index + 1];
            if (next) {
                focusWithoutScroll(next);
                if (next.select) next.select();
                return;
            }
            focusShiftAction(component, input);
        }

        component.addEventListener("keyup", function (event) {
            if (event.key === "Enter" || event.keyCode === 13) navigationLocked = false;
        });

        fields.forEach(function (input, index) {
            var isLast = index === fields.length - 1;

            function isEnterEvent(event) {
                return event.key === "Enter" || event.keyCode === 13;
            }

            input.setAttribute("inputmode", "numeric");
            input.setAttribute("step", "1");
            input.setAttribute("pattern", "[0-9]*");
            input.setAttribute("enterkeyhint", isLast ? "done" : "next");
            if (!input.getAttribute("placeholder")) input.setAttribute("placeholder", "0");
            input.addEventListener("beforeinput", function (event) {
                if (event.inputType === "insertLineBreak" || event.inputType === "insertParagraph") {
                    advanceField(input, index, event);
                    return;
                }
                if (event.data && /\D/.test(event.data)) event.preventDefault();
            });
            input.addEventListener("input", function () { keepWholeNumber(input); });
            input.addEventListener("keydown", function (event) {
                if (isEnterEvent(event)) advanceField(input, index, event);
            });
        });
    }

    function bindScreens() {
        document.querySelectorAll(".mobile-shift").forEach(bindFieldNavigation);
    }

    function syncPhysicalOrientation() {
        var orientation = window.screen && window.screen.orientation;
        var type = orientation && String(orientation.type || "");
        var landscape = type
            ? type.indexOf("landscape") === 0
            : Number(window.screen && window.screen.width || 0) > Number(window.screen && window.screen.height || 0);
        document.documentElement.dataset.mobileShiftOrientation = landscape ? "landscape" : "portrait";
    }

    function init() {
        syncPhysicalOrientation();
        window.addEventListener("orientationchange", syncPhysicalOrientation, { passive: true });
        if (window.screen && window.screen.orientation && window.screen.orientation.addEventListener) {
            window.screen.orientation.addEventListener("change", syncPhysicalOrientation);
        }
        bindScreens();
    }

    window.MobileShiftHold = { bind: bind };
    window.bindMobileShiftScreens = bindScreens;
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init, { once: true });
    else init();
})();
