(function (win, doc) {
    "use strict";
    if (win.CopperTabletEntryFields) {
        win.CopperTabletEntryFields.init(doc);
        return;
    }
    var formSelector = "form[data-tablet-shift-entry]";
    var displays = new WeakMap();
    var pendingCaret = new WeakMap();

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
            setText(cell, display.phone ? (filled ? digits[i] : "X") : (filled ? "●" : "○"));
            var selected = focused && i >= range[0] && i < range[1];
            var caret = focused && range[0] === range[1] && range[0] === i;
            var atEnd = focused && range[0] === range[1] && range[0] === display.cells.length && i === range[0] - 1;
            var cls = "cr-digit" + (filled ? "" : " is-empty") + (selected ? " is-selected" : "") +
                (caret ? " caret-before" : atEnd ? " caret-after" : "");
            if (cell.className !== cls) cell.className = cls;
        });
        setText(display.help, remaining(display.cells.length - digits.length));
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
            group.style.flexGrow = size;
            for (var i = 0; i < size; i++) {
                var cell = makeSpan("cr-digit");
                cells.push(cell);
                group.appendChild(cell);
            }
            strip.appendChild(group);
        });
        var help = makeSpan("cr-digit-hint");
        help.id = "cr-digit-hint-" + input.name;
        input.setAttribute("aria-describedby", ((input.getAttribute("aria-describedby") || "") + " " + help.id).trim());
        displays.set(input, {phone: phone, cells: cells, help: help});
        // Layer the read-only cells beside the original input in the same grid
        // area. Moving or replacing a focused input can dismiss the Android IME.
        input.classList.add("cr-cell-input");
        if (phone) input.parentElement.classList.add("cr-cell-phone-row");
        else label.classList.add("cr-cell-pin-label");
        input.after(strip);
        label.appendChild(help);

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
        // Autofill may emit change only. Reuse the bound field mask; this event
        // stays on the input and does not re-enter the document update handler.
        if (digits.length === target && input.validity.patternMismatch && input.dataset.sharedShiftLoginBound === "true") {
            input.dispatchEvent(new Event("input"));
        }
        var counter = counterFor(input);
        if (counter) setText(counter, digits.length + "/" + target);
        buildDisplay(input, phone);
        paint(input);
        return digits.length === target && input.validity.valid;
    }

    function refresh(form) {
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
        if (range[0] !== range[1]) return;
        if (event.inputType === "insertText" && digits.length >= display.cells.length) event.preventDefault();
        // Backspace/Delete must remove a digit even beside a wire-format separator.
        if (event.inputType === "deleteContentBackward" && /[ -]/.test(input.value.charAt(input.selectionStart - 1))) {
            event.preventDefault();
            replaceRange(input, digits, Math.max(0, range[0] - 1), range[1], "");
        } else if (event.inputType === "deleteContentForward" && /[ -]/.test(input.value.charAt(input.selectionEnd))) {
            event.preventDefault();
            replaceRange(input, digits, range[0], Math.min(range[1] + 1, digits.length), "");
        }
    }, true);
    doc.addEventListener("click", function (event) {
        if (event.target.closest("[data-shared-shift-login-open], [data-shared-shift-login-cancel]")) afterChange();
    });
    doc.addEventListener("reset", function (event) {
        if (event.target.matches(formSelector)) afterChange();
    });
    doc.addEventListener("submit", function (event) {
        if (event.target.matches(formSelector) && !(event.submitter && event.submitter.formNoValidate) && refresh(event.target) === false) {
            event.preventDefault();
        }
    }, true);
    doc.addEventListener("keydown", function (event) {
        if (event.key === "Enter") refreshTarget(event);
    }, true);
    win.addEventListener("native-ime-action", function () {
        var form = doc.activeElement && doc.activeElement.form;
        if (form && form.matches(formSelector)) refresh(form);
    }, true);
    win.addEventListener("pageshow", afterChange);
    win.CopperTabletEntryFields = {init: init};
    if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", function () { init(doc); }, {once: true});
    else init(doc);
})(window, document);
