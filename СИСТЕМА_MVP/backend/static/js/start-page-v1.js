(function (window, document) {
    "use strict";

    var body = document.body;
    var form = document.querySelector("[data-start-form]");
    var input = document.querySelector("[data-start-phone]");
    var phoneShell = document.querySelector("[data-start-phone-shell]");
    var hint = document.querySelector("[data-start-hint]");
    var submit = document.querySelector("[data-start-submit]");
    var submitLabel = document.querySelector("[data-start-submit-label]");
    var viewport = window.visualViewport || null;

    if (!body || !form || !input || !submit) return;

    var baselineWidth = 0;
    var baselineHeight = 0;
    var framePending = false;
    var blurTimer = 0;
    var scrollSignature = "";
    var submitting = false;
    var wasValid = false;
    var defaultSubmitLabel = submitLabel ? submitLabel.textContent : "Далее";

    function viewportSnapshot() {
        return {
            width: Math.round(viewport ? viewport.width : window.innerWidth),
            height: Math.round(viewport ? viewport.height : window.innerHeight),
            offsetTop: Math.max(0, Math.round(viewport ? viewport.offsetTop : 0)),
            scale: viewport ? Number(viewport.scale || 1) : 1
        };
    }

    function hasFormFocus() {
        return Boolean(document.activeElement && form.contains(document.activeElement));
    }

    function hasPhoneFocus() {
        return document.activeElement === input;
    }

    function keepFormVisible(snapshot, keyboardOpen) {
        if (!keyboardOpen) {
            scrollSignature = "";
            return;
        }

        var rect = form.getBoundingClientRect();
        var visibleTop = snapshot.offsetTop + 8;
        var visibleBottom = snapshot.offsetTop + snapshot.height - 14;
        var signature = [snapshot.height, Math.round(rect.top), Math.round(rect.bottom)].join(":");
        if (signature === scrollSignature) return;

        if (rect.bottom > visibleBottom || rect.top < visibleTop) {
            scrollSignature = signature;
            form.scrollIntoView({
                block: "end",
                inline: "nearest",
                behavior: "auto"
            });
        }
    }

    function syncViewport() {
        framePending = false;
        var snapshot = viewportSnapshot();
        var focused = hasFormFocus();
        var phoneFocused = hasPhoneFocus();
        var normalScale = snapshot.scale <= 1.05;
        var orientationChanged = baselineWidth && Math.abs(snapshot.width - baselineWidth) > 80;

        if (!baselineWidth || orientationChanged) {
            baselineWidth = snapshot.width;
            baselineHeight = snapshot.height;
        }

        if (normalScale && (!focused || snapshot.height >= baselineHeight - 80)) {
            baselineHeight = Math.max(baselineHeight, snapshot.height);
        }

        var shrink = Math.max(0, baselineHeight - snapshot.height);
        var threshold = Math.max(120, baselineHeight * 0.22);
        var keyboardOpen = Boolean(
            phoneFocused &&
            normalScale &&
            !orientationChanged &&
            shrink >= threshold
        );

        body.classList.toggle("is-input-mode", focused);
        body.classList.toggle("is-keyboard-open", keyboardOpen);
        body.classList.toggle("is-start-viewport-tight", snapshot.height <= 620);
        body.style.setProperty("--start-vv-height", snapshot.height + "px");
        body.style.setProperty("--start-vv-top", snapshot.offsetTop + "px");
        keepFormVisible(snapshot, keyboardOpen);
    }

    function scheduleViewportSync() {
        if (framePending) return;
        framePending = true;
        window.requestAnimationFrame(syncViewport);
    }

    function countDigits(value) {
        return (String(value || "").match(/\d/g) || []).length;
    }

    function nationalDigits(value) {
        var digits = String(value || "").replace(/\D/g, "");
        if (digits.charAt(0) === "8") digits = "7" + digits.slice(1);
        if (digits.charAt(0) === "7") digits = digits.slice(1);
        return digits.slice(0, 10);
    }

    function formatDigits(digits) {
        var result = digits.slice(0, 3);
        if (digits.length > 3) result += "-" + digits.slice(3, 6);
        if (digits.length > 6) result += "-" + digits.slice(6, 8);
        if (digits.length > 8) result += "-" + digits.slice(8, 10);
        return result;
    }

    function caretForDigit(formatted, desiredDigits) {
        if (desiredDigits <= 0) return 0;
        var seen = 0;
        for (var index = 0; index < formatted.length; index += 1) {
            if (/\d/.test(formatted.charAt(index))) seen += 1;
            if (seen >= desiredDigits) return index + 1;
        }
        return formatted.length;
    }

    function formatInputPreservingCaret() {
        var raw = input.value;
        var cursor = typeof input.selectionStart === "number" ? input.selectionStart : raw.length;
        var digitsBeforeCursor = countDigits(raw.slice(0, cursor));
        var allDigits = String(raw).replace(/\D/g, "");
        var prefixWasRemoved = allDigits.charAt(0) === "7" || allDigits.charAt(0) === "8";
        var digits = nationalDigits(raw);
        var desiredDigits = Math.max(0, digitsBeforeCursor - (prefixWasRemoved ? 1 : 0));
        var formatted = formatDigits(digits);

        input.value = formatted;
        try {
            var nextCaret = caretForDigit(formatted, desiredDigits);
            input.setSelectionRange(nextCaret, nextCaret);
        } catch (_error) {
            /* Старые WebView могут не поддерживать selectionRange для tel-like input. */
        }
        return digits;
    }

    function validationMessage(digits, forceMessage) {
        if (!digits) return forceMessage ? "Введите 10 цифр номера после +7." : "10 цифр номера";
        if (digits.length < 10) return "Нужно 10 цифр после +7. Сейчас введено: " + digits.length + ".";
        if (digits.charAt(0) !== "9") return "Мобильный номер после +7 должен начинаться с 9.";
        return "Введено правильно.";
    }

    function check(options) {
        options = options || {};
        var digits = formatInputPreservingCaret();
        var valid = digits.length === 10 && digits.charAt(0) === "9";
        var invalid = Boolean(digits) && !valid;
        var showInvalid = Boolean(invalid || (Boolean(options.forceMessage) && !valid));
        var message = validationMessage(digits, Boolean(options.forceMessage));

        input.setAttribute("aria-invalid", showInvalid ? "true" : "false");
        if (phoneShell) {
            phoneShell.classList.toggle("is-invalid", showInvalid);
            phoneShell.classList.toggle("is-valid", valid);
        }
        if (hint) {
            hint.textContent = message;
            hint.classList.toggle("is-invalid", showInvalid);
            hint.classList.toggle("is-valid", valid);
        }

        if (!submitting) submit.disabled = !valid;
        if (valid && !wasValid) {
            submit.classList.remove("is-ready");
            window.requestAnimationFrame(function () {
                submit.classList.add("is-ready");
            });
        } else if (!valid) {
            submit.classList.remove("is-ready");
        }
        wasValid = valid;
        return valid;
    }

    function beginInputMode() {
        window.clearTimeout(blurTimer);
        body.classList.add("is-input-mode");
        syncViewport();
        scheduleViewportSync();
        window.setTimeout(scheduleViewportSync, 100);
        window.setTimeout(scheduleViewportSync, 260);
    }

    function finishInputModeLater() {
        window.clearTimeout(blurTimer);
        blurTimer = window.setTimeout(function () {
            if (!hasFormFocus()) {
                body.classList.remove("is-input-mode");
                body.classList.remove("is-keyboard-open");
                syncViewport();
            }
        }, 240);
    }

    function resetSubmitState() {
        submitting = false;
        form.removeAttribute("aria-busy");
        submit.removeAttribute("aria-busy");
        if (submitLabel) submitLabel.textContent = defaultSubmitLabel;
        check();
        syncViewport();
    }

    input.addEventListener("input", function () { check(); });
    input.addEventListener("paste", function () {
        window.setTimeout(function () { check(); }, 0);
    });
    form.addEventListener("focusin", beginInputMode);
    form.addEventListener("focusout", finishInputModeLater);
    form.addEventListener("submit", function (event) {
        if (submitting) {
            event.preventDefault();
            return;
        }
        if (!check({forceMessage: true})) {
            event.preventDefault();
            input.focus();
            beginInputMode();
            return;
        }
        submitting = true;
        form.setAttribute("aria-busy", "true");
        submit.setAttribute("aria-busy", "true");
        submit.disabled = true;
        if (submitLabel) submitLabel.textContent = "Проверяем…";
    });

    if (viewport) {
        viewport.addEventListener("resize", scheduleViewportSync);
        viewport.addEventListener("scroll", scheduleViewportSync);
    }
    window.addEventListener("resize", scheduleViewportSync);
    window.addEventListener("orientationchange", function () {
        baselineWidth = 0;
        baselineHeight = 0;
        body.classList.remove("is-keyboard-open");
        window.setTimeout(scheduleViewportSync, 120);
        window.setTimeout(scheduleViewportSync, 360);
    });
    window.addEventListener("pageshow", resetSubmitState);

    check();
    syncViewport();
})(window, document);
