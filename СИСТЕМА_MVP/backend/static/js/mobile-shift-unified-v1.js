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

        function holdMs() {
            var value = typeof options.holdMs === "function" ? options.holdMs() : options.holdMs;
            return Math.max(250, Number(value) || 2000);
        }

        function enabled() {
            return !button.disabled && button.getAttribute("aria-disabled") !== "true" && !button.classList.contains("is-pending");
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
            if (!enabled()) {
                reset();
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
            if (!enabled() || startedAt) return;
            if (event) event.preventDefault();
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
            if (completed) {
                completed = false;
                return;
            }
            if (enabled() && typeof options.onShortPress === "function") options.onShortPress();
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

    function bindFieldNavigation(component) {
        if (!component || component.dataset.mobileShiftFieldsBound === "true") return;
        component.dataset.mobileShiftFieldsBound = "true";
        var fields = Array.prototype.filter.call(
            component.querySelectorAll(".mobile-shift__metric-value input"),
            function (input) { return !input.disabled && !input.readOnly; }
        );
        fields.forEach(function (input, index) {
            var isLast = index === fields.length - 1;
            input.setAttribute("inputmode", "numeric");
            input.setAttribute("step", "1");
            input.setAttribute("pattern", "[0-9]*");
            input.setAttribute("enterkeyhint", isLast ? "done" : "next");
            if (!input.getAttribute("placeholder")) input.setAttribute("placeholder", "0");
            input.addEventListener("beforeinput", function (event) {
                if (event.data && /\D/.test(event.data)) event.preventDefault();
            });
            input.addEventListener("input", function () { keepWholeNumber(input); });
            input.addEventListener("keydown", function (event) {
                if (event.key !== "Enter" && event.keyCode !== 13) return;
                event.preventDefault();
                var next = fields[index + 1];
                if (next) {
                    next.focus();
                    if (next.select) next.select();
                    return;
                }
                input.blur();
                var action = component.querySelector(
                    "[data-driver-shift-open-button], [data-driver-shift-close-button], [data-eo-shift-button]"
                );
                if (action && !action.disabled && action.getAttribute("aria-disabled") !== "true") {
                    action.focus({ preventScroll: true });
                    return;
                }
                if (!component.hasAttribute("tabindex")) component.setAttribute("tabindex", "-1");
                component.focus({ preventScroll: true });
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
