(function (win, doc) {
    "use strict";
    if (win.CopperTabletEntryFields) {
        win.CopperTabletEntryFields.init(doc);
        return;
    }
    var formSelector = "form[data-tablet-shift-entry]";
    var autofocusSelector = formSelector + " input[autofocus]";
    var displays = new WeakMap();
    var pendingCaret = new WeakMap();
    var watchedModals = new WeakSet();
    var autofocused = new WeakSet();
    var fieldId = 0;

    function dismissAutofocus(input) {
        if (!autofocused.has(input) || doc.activeElement !== input) return;
        input.blur();
        win.setTimeout(function () {
            // Chromium may deliver the queued focus even after removal of its attribute.
            // A real touch or Tab in the meantime must always win.
            if (!autofocused.has(input)) return;
            if (doc.activeElement === input) input.blur();
            if (doc.activeElement && doc.activeElement.matches("input, textarea, [contenteditable=true]")) return;
            var keyboard = win.Capacitor && win.Capacitor.Plugins && win.Capacitor.Plugins.NativeKeyboard;
            if (keyboard && typeof keyboard.hide === "function") keyboard.hide().catch(function () {});
        }, 0);
    }
    function removeInitialFocus(input) {
        autofocused.add(input);
        input.removeAttribute("autofocus");
        dismissAutofocus(input);
    }
    function allowUserFocus(form) {
        form.querySelectorAll("input").forEach(function (input) {
            autofocused.delete(input);
            input.removeAttribute("autofocus");
        });
    }

    // The login template can request autofocus before the WebView finishes loading.
    // Drop it during parsing; leave deliberate touches, Tab and IME Next alone.
    if (doc.readyState === "loading" && /\/mining-master\/tablet\/(login|activate)\/$/.test(win.location.pathname)) {
        var autoObserver = new MutationObserver(function (records) {
            records.forEach(function (record) {
                record.addedNodes.forEach(function (node) {
                    if (node.nodeType !== 1) return;
                    if (node.matches(autofocusSelector)) removeInitialFocus(node);
                    else node.querySelectorAll(autofocusSelector).forEach(removeInitialFocus);
                });
            });
        });
        autoObserver.observe(doc, {childList: true, subtree: true});
        doc.addEventListener("DOMContentLoaded", function () { autoObserver.disconnect(); }, {once: true});
    }
    function checkFocus(event) {
        var target = event.target;
        if (target.matches && target.matches(autofocusSelector)) removeInitialFocus(target);
        else dismissAutofocus(target);
    }
    doc.addEventListener("focus", checkFocus, true);
    doc.addEventListener("focusin", checkFocus, true);
    doc.addEventListener("pointerdown", function (event) {
        var target = event.target.closest && event.target.closest(formSelector);
        if (target) allowUserFocus(target);
    }, true);
    doc.addEventListener("keydown", function (event) {
        if (event.key === "Tab") doc.querySelectorAll(formSelector).forEach(allowUserFocus);
    }, true);

    function setText(node, text) {
        if (node.textContent !== text) node.textContent = text;
    }

    function counterFor(input) {
        var label = input.closest("label");
        if (!label) return null;
        var counter = label.querySelector(".cr-entry-counter");
        if (counter) return counter;
        // Keep the original label and control association. The badge is purely visual.
        var title = Array.from(label.childNodes).find(function (node) {
            return node.nodeType === 3 ? node.textContent.trim() : node.tagName === "SPAN" && !node.contains(input);
        });
        if (!title) return null;
        var heading = doc.createElement("span");
        heading.className = "cr-entry-heading";
        label.insertBefore(heading, title);
        if (!input.hasAttribute("data-tablet-phone") && (input.name === "access_code" || input.hasAttribute("data-shared-shift-code"))) {
            title.textContent = "Личный PIN";
        }
        heading.appendChild(title);
        counter = doc.createElement("span");
        counter.className = "cr-entry-counter";
        counter.setAttribute("aria-hidden", "true");
        heading.appendChild(counter);
        return counter;
    }

    function nationalDigits(value, phone) {
        var digits = value.replace(/\D/g, "");
        return phone && digits.length === 11 && /^[78]/.test(digits) ? digits.slice(1) : digits;
    }

    // Selection is expressed in digits; the existing form owns its POST mask.
    function digitSelection(input) {
        var v = input.value;
        var prefix = input.hasAttribute("data-tablet-phone") && /^[78]\d{10}$/.test(v.replace(/\D/g, "")) ? 1 : 0;
        function position(offset) {
            return Math.max(0, v.slice(0, offset).replace(/\D/g, "").length - prefix);
        }
        return [position(input.selectionStart), position(input.selectionEnd)];
    }

    function selectDigits(input, anchor, end) {
        var offsets = [0];
        for (var i = 0; i < input.value.length; i++) {
            if (/\d/.test(input.value[i])) offsets.push(i + 1);
        }
        function offset(index) { return offsets[Math.min(index, offsets.length - 1)]; }
        input.setSelectionRange(offset(Math.min(anchor, end)), offset(Math.max(anchor, end)), anchor > end ? "backward" : "forward");
    }

    function makeSpan(className) {
        var el = doc.createElement("span");
        el.className = className;
        return el;
    }

    function remaining(n) {
        return n <= 0 ? "Заполнено" : "Ещё " + n + (n === 1 ? " цифра" : n < 5 ? " цифры" : " цифр");
    }

    function paint(input) {
        var display = displays.get(input);
        if (!display) return;
        var digits = nationalDigits(input.value, display.phone);
        var range = digitSelection(input), focused = doc.activeElement === input;
        display.cells.forEach(function (cell, i) {
            var filled = i < digits.length;
            var mark = filled ? (display.phone || display.revealed ? digits[i] : "●") : (display.phone ? "X" : "○");
            setText(cell, mark);
            var selected = focused && i >= range[0] && i < range[1];
            var caret = focused && range[0] === range[1] && range[0] === i;
            var atEnd = focused && range[0] === range[1] && range[0] === display.cells.length && i === range[0] - 1;
            var cls = "cr-digit" + (filled ? "" : " is-empty") + (selected ? " is-selected" : "") +
                (caret ? " caret-before" : atEnd ? " caret-after" : "");
            if (cell.className !== cls) cell.className = cls;
        });
        setText(display.help, remaining(display.cells.length - digits.length));
    }

    function setPinVisible(input, show) {
        var d = displays.get(input);
        if (!d || !d.toggle || d.revealed === show) return;
        d.revealed = show;
        var name = input.name === "confirm_access_code" ? "повторный PIN" :
            input.name === "new_access_code" ? "новый PIN" : "PIN";
        d.toggle.setAttribute("aria-label", (show ? "Скрыть " : "Показать ") + name);
        d.toggle.setAttribute("aria-pressed", String(show));
        d.slash.setAttribute("display", show ? "inline" : "none");
        paint(input);
    }

    function addPinToggle(input, label, display) {
        var frame = doc.createElement("div");
        frame.className = "cr-pin-field";
        // Move the original label as a unit. A physical tap may have arrived
        // before the late page-loaded fallback, so preserve its live selection.
        var wasFocused = doc.activeElement === input;
        var start = input.selectionStart, end = input.selectionEnd, direction = input.selectionDirection;
        label.before(frame);
        if (typeof frame.moveBefore === "function") frame.moveBefore(label, null);
        else frame.appendChild(label);
        if (wasFocused && doc.activeElement !== input) {
            input.focus({preventScroll: true});
            input.setSelectionRange(start, end, direction);
        }
        if (!input.id) input.id = "cr-entry-pin-" + fieldId;
        var toggle = doc.createElement("button");
        toggle.type = "button";
        toggle.className = "cr-pin-toggle";
        toggle.setAttribute("aria-controls", input.id);
        var ns = "http://www.w3.org/2000/svg", svg = doc.createElementNS(ns, "svg");
        svg.setAttribute("viewBox", "0 0 24 24");
        svg.setAttribute("aria-hidden", "true");
        svg.setAttribute("fill", "none");
        svg.setAttribute("stroke", "currentColor");
        svg.setAttribute("stroke-width", "1.8");
        svg.setAttribute("stroke-linecap", "round");
        svg.setAttribute("stroke-linejoin", "round");
        var eye = doc.createElementNS(ns, "path"), iris = doc.createElementNS(ns, "circle"), slash = doc.createElementNS(ns, "path");
        eye.setAttribute("d", "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z");
        iris.setAttribute("cx", "12"); iris.setAttribute("cy", "12"); iris.setAttribute("r", "3");
        slash.setAttribute("d", "m3 3 18 18");
        svg.append(eye, iris, slash);
        toggle.appendChild(svg);
        frame.appendChild(toggle);
        display.toggle = toggle;
        display.slash = slash;
        setPinVisible(input, false);
        // A touch on the eye must neither enter a digit nor detach the IME.
        toggle.addEventListener("pointerdown", function (event) { event.preventDefault(); });
        toggle.addEventListener("click", function () { setPinVisible(input, !display.revealed); });
    }

    function buildDisplay(input, phone) {
        var label = input.closest("label");
        if (!label || displays.has(input)) return;
        var strip = makeSpan("cr-cell-strip"), cells = [];
        strip.setAttribute("aria-hidden", "true");
        (phone ? [3, 3, 2, 2] : [2, 2, 2]).forEach(function (size, groupIndex) {
            if (phone || groupIndex) {
                var dash = makeSpan("cr-digit-dash");
                dash.textContent = "-";
                strip.appendChild(dash);
            }
            var group = makeSpan("cr-digit-group");
            if (phone) group.style.flexGrow = size;
            for (var i = 0; i < size; i++) {
                var cell = makeSpan("cr-digit");
                cells.push(cell);
                group.appendChild(cell);
            }
            strip.appendChild(group);
        });
        var help = makeSpan("cr-digit-hint");
        help.id = "cr-digit-hint-" + (++fieldId);
        input.setAttribute("aria-describedby", ((input.getAttribute("aria-describedby") || "") + " " + help.id).trim());
        var display = {phone: phone, cells: cells, help: help};
        displays.set(input, display);
        // Layer the read-only cells beside the original input in the same grid
        // area. Moving or replacing a focused input can dismiss the Android IME.
        input.classList.add("cr-cell-input");
        if (phone) input.parentElement.classList.add("cr-cell-phone-row");
        else label.classList.add("cr-cell-pin-label");
        input.after(strip);
        label.appendChild(help);
        if (!phone) addPinToggle(input, label, display);

        function point(x) {
            for (var i = 0; i < cells.length; i++) {
                var box = cells[i].getBoundingClientRect();
                if (x < box.left + box.width / 2) return i;
            }
            return cells.length;
        }
        var anchor = null;
        input.addEventListener("pointerdown", function (event) {
            if (event.button !== 0 || !event.isPrimary) return;
            var index = point(event.clientX);
            var range = digitSelection(input);
            anchor = event.shiftKey ? range[input.selectionDirection === "backward" ? 1 : 0] : index;
            input.focus();
            selectDigits(input, anchor, index);
            input.setPointerCapture(event.pointerId);
            event.preventDefault();
            paint(input);
        });
        input.addEventListener("pointermove", function (event) {
            if (anchor !== null && input.hasPointerCapture(event.pointerId)) selectDigits(input, anchor, point(event.clientX));
        });
        input.addEventListener("lostpointercapture", function () { anchor = null; });
        input.addEventListener("dblclick", function () { input.select(); });
    }

    function fieldComplete(input) {
        var phone = input.hasAttribute("data-tablet-phone");
        var digits = nationalDigits(input.value, phone);
        var target = phone ? 10 : 6;
        if (!phone && !input.hasAttribute("data-shared-shift-code") && input.value !== digits.slice(0, target)) {
            input.value = digits = digits.slice(0, target);
        }
        // Autofill may emit change only. Reuse the bound field mask; this event
        // stays on the input and does not re-enter the document update handler.
        if (digits.length === target && input.validity.patternMismatch && input.dataset.sharedShiftLoginBound === "true") {
            input.dispatchEvent(new Event("input"));
        }
        var counter = counterFor(input);
        if (counter) setText(counter, digits.length + "/" + target);
        buildDisplay(input, phone);
        paint(input);
        var label = input.closest("label");
        if (label) label.toggleAttribute("data-cr-complete", digits.length === target && input.validity.valid);
        return digits.length === target && input.validity.valid;
    }

    function refresh(form) {
        var modal = form.closest("[data-tablet-personal-login]");
        if (modal && !watchedModals.has(modal)) {
            watchedModals.add(modal);
            new MutationObserver(function () {
                // Close, Escape and header replacement can happen without a click on Cancel.
                resetPins(form, true);
                refresh(form);
            }).observe(modal, {attributes: true, attributeFilter: ["hidden"]});
        }
        var primary = form.querySelector('button[type=submit]:not([formnovalidate])');
        var fields = form.querySelectorAll("input[data-tablet-phone], input[type=password]");
        if (!primary || fields.length !== 2) return;
        var phoneEntry = fields[0].hasAttribute("data-tablet-phone");
        var first = fieldComplete(fields[0]), last = fieldComplete(fields[1]);
        var mismatch = !phoneEntry && first && last && fields[0].value !== fields[1].value;
        var ready = first && last && !mismatch;
        var state = ready ? "ready" : mismatch ? "mismatch" : "incomplete";
        var status = form.querySelector(".cr-entry-status");
        if (!status) {
            status = doc.createElement("span");
            status.className = "cr-entry-status";
            status.setAttribute("role", "status");
            var entryLink = form.querySelector(".shared-shift-first-entry");
            if (entryLink) entryLink.after(status);
            else form.querySelector(".tablet-auth-actions").before(status);
        }
        if (form.dataset.crEntryState !== state) form.dataset.crEntryState = state;
        var message;
        if (ready) message = phoneEntry ? "Поля заполнены" : "PIN совпадают";
        else if (mismatch) message = "PIN не совпадают";
        else if (phoneEntry) message = !first && !last ? "Введите телефон и PIN" : !first ? "Проверьте номер телефона" : "Введите 6 цифр PIN";
        else message = first ? "Повторите PIN" : "Введите 6 цифр нового PIN";
        setText(status, message);
        if (!ready && !primary.disabled) {
            primary.setAttribute("data-cr-entry-disabled", "");
            primary.disabled = true;
        } else if (ready && primary.hasAttribute("data-cr-entry-disabled")) {
            primary.removeAttribute("data-cr-entry-disabled");
            primary.disabled = false;
        }
        return ready;
    }

    function init(root) {
        root = root || doc;
        root.querySelectorAll(autofocusSelector).forEach(removeInitialFocus);
        dismissAutofocus(doc.activeElement);
        if (root.matches && root.matches(formSelector)) refresh(root);
        root.querySelectorAll(formSelector).forEach(refresh);
    }
    function refreshTarget(event) {
        var input = event.target, form = input.form;
        var selection = pendingCaret.get(input);
        if (selection && event.type === "input") {
            pendingCaret.delete(input);
            if (doc.activeElement === input) selectDigits(input, selection[0], selection[1]);
        }
        if (form && form.matches(formSelector)) refresh(form);
    }
    function afterChange() { win.setTimeout(function () { init(doc); }, 0); }

    function resetPins(form, clear) {
        form.querySelectorAll('input[type="password"]').forEach(function (input) {
            if (clear) input.value = "";
            setPinVisible(input, false);
            paint(input);
        });
    }
    function resetAllPins(clear) {
        doc.querySelectorAll(formSelector).forEach(function (form) { resetPins(form, clear); });
    }

    doc.addEventListener("input", function (event) {
        var input = event.target, display = displays.get(input);
        if (!display || !event.bubbles) return;
        pendingCaret.set(input, digitSelection(input));
        if (!display.phone && !input.hasAttribute("data-shared-shift-code")) {
            var plain = nationalDigits(input.value, false).slice(0, 6);
            if (input.value !== plain) input.value = plain;
        }
    }, true);
    ["input", "change", "focusin", "focusout"].forEach(function (type) {
        doc.addEventListener(type, refreshTarget);
    });
    doc.addEventListener("selectionchange", function () { paint(doc.activeElement); });

    function moveDigitCaret(event, input, display) {
        if (event.altKey || event.metaKey || event.ctrlKey || event.isComposing) return;
        var key = event.key;
        if (key !== "Home" && key !== "End" && key !== "ArrowLeft" && key !== "ArrowRight") return;
        var range = digitSelection(input), backward = input.selectionDirection === "backward";
        var anchor = range[backward ? 1 : 0], tip = range[backward ? 0 : 1];
        var length = nationalDigits(input.value, display.phone).length;
        if (key === "Home") tip = 0;
        else if (key === "End") tip = length;
        else if (!event.shiftKey && range[0] !== range[1]) tip = range[key === "ArrowLeft" ? 0 : 1];
        else tip = Math.max(0, Math.min(length, tip + (key === "ArrowLeft" ? -1 : 1)));
        event.preventDefault();
        selectDigits(input, event.shiftKey ? anchor : tip, tip);
        paint(input);
    }

    function replaceRange(input, digits, start, end, replacement) {
        var maxlength = displays.get(input).cells.length;
        var insert = replacement.slice(0, Math.max(0, maxlength - (digits.length - end + start)));
        input.value = digits.slice(0, start) + insert + digits.slice(end);
        input.setSelectionRange(start + insert.length, start + insert.length);
        input.dispatchEvent(new Event("input", {bubbles: true}));
    }
    doc.addEventListener("paste", function (event) {
        var input = event.target, display = displays.get(input);
        if (!display || !event.clipboardData) return;
        var pasted = nationalDigits(event.clipboardData.getData("text"), display.phone);
        event.preventDefault();
        event.stopPropagation(); // The legacy PIN paste handler replaces the whole field.
        if (!pasted) return;
        var digits = nationalDigits(input.value, display.phone), range = digitSelection(input);
        if (pasted.length === display.cells.length) range = [0, digits.length];
        replaceRange(input, digits, range[0], range[1], pasted);
    }, true);
    doc.addEventListener("beforeinput", function (event) {
        var input = event.target, display = displays.get(input);
        if (!display || !event.cancelable || event.isComposing) return;
        var range = digitSelection(input), digits = nationalDigits(input.value, display.phone);
        if (event.inputType === "insertText" || event.inputType === "insertReplacementText") {
            if (event.data === null) return;
            var insertion = nationalDigits(event.data, display.phone);
            // Keep Android's normal editing and keyboard feedback for a single digit.
            if (/^[0-9]$/.test(event.data)) {
                if (digits.length >= display.cells.length && range[0] === range[1]) event.preventDefault();
                return;
            }
            event.preventDefault();
            if (!insertion) return;
            if (insertion.length === display.cells.length) range = [0, digits.length];
            replaceRange(input, digits, range[0], range[1], insertion);
        } else if (event.inputType === "deleteContentBackward" || event.inputType === "deleteContentForward") {
            if (range[0] !== range[1]) return;
            var backward = event.inputType === "deleteContentBackward";
            var adjacent = input.value.charAt(backward ? input.selectionStart - 1 : input.selectionEnd);
            if (input.selectionStart === input.selectionEnd && !/[ -]/.test(adjacent)) return;
            event.preventDefault();
            if (backward) range[0] = Math.max(0, range[0] - 1);
            else range[1] = Math.min(digits.length, range[1] + 1);
            replaceRange(input, digits, range[0], range[1], "");
        }
    }, true);
    doc.addEventListener("click", function (event) {
        var opener = event.target.closest && event.target.closest("[data-shared-shift-login-open]");
        var slot = opener && opener.closest("form");
        var modal = slot && slot.parentElement.querySelector("[data-tablet-personal-login][hidden]");
        var first = modal && modal.querySelector("[data-tablet-phone]");
        if (!first || first.disabled) return;
        // The server opener positions and clears its modal, then tries to focus the phone.
        // During that one click it also calls first.focus(), which opens Android's IME.
        // The real field is available again before the operator can type or tap.
        first.disabled = true;
        function release() {
            first.disabled = false;
            doc.removeEventListener("click", release);
        }
        doc.addEventListener("click", release);
        win.setTimeout(release, 0);
    }, true);
    doc.addEventListener("click", function (event) {
        if (!event.target.closest) return;
        if (event.target.closest("[data-shared-shift-login-open], [data-shared-shift-login-cancel]")) afterChange();
        var cancel = event.target.closest("[data-shared-shift-login-cancel], .tablet-auth-secondary");
        if (cancel && cancel.closest(formSelector)) resetPins(cancel.closest(formSelector), true);
    });
    doc.addEventListener("reset", function (event) {
        if (event.target.matches(formSelector)) {
            resetPins(event.target, false);
            afterChange();
        }
    });
    doc.addEventListener("submit", function (event) {
        if (event.target.matches(formSelector)) {
            resetPins(event.target, false);
            if (!(event.submitter && event.submitter.formNoValidate) && refresh(event.target) === false) event.preventDefault();
        }
    }, true);
    doc.addEventListener("keydown", function (event) {
        var display = displays.get(event.target);
        if (display) moveDigitCaret(event, event.target, display);
        if (event.key === "Enter") refreshTarget(event);
    }, true);
    win.addEventListener("native-ime-action", function () {
        var form = doc.activeElement && doc.activeElement.form;
        if (form && form.matches(formSelector)) refresh(form);
    }, true);
    doc.addEventListener("visibilitychange", function () {
        if (doc.hidden) resetAllPins(false);
    });
    win.addEventListener("pagehide", function () { resetAllPins(true); });
    win.addEventListener("pageshow", function () { resetAllPins(true); afterChange(); });
    win.CopperTabletEntryFields = {init: init};
    if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", function () { init(doc); }, {once: true});
    else init(doc);
})(window, document);
