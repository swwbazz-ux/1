(function () {
    "use strict";

    function copyText(value) {
        if (navigator.clipboard && window.isSecureContext) {
            return navigator.clipboard.writeText(value);
        }
        var textarea = document.createElement("textarea");
        textarea.value = value;
        textarea.setAttribute("readonly", "");
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.select();
        var copied = document.execCommand("copy");
        textarea.remove();
        return copied ? Promise.resolve() : Promise.reject(new Error("copy_failed"));
    }

    function showNotice(message, kind) {
        var stack = document.querySelector("[data-card-notices]");
        if (!stack) return;
        var notice = document.createElement("div");
        notice.className = "employee-card-notice " + (kind || "success");
        notice.textContent = message;
        stack.appendChild(notice);
        window.setTimeout(function () {
            notice.classList.add("is-hiding");
            window.setTimeout(function () { notice.remove(); }, 200);
        }, 1800);
    }

    function initNotices() {
        document.querySelectorAll("[data-card-notice]").forEach(function (notice, index) {
            window.setTimeout(function () {
                notice.classList.add("is-hiding");
                window.setTimeout(function () { notice.remove(); }, 200);
            }, 2600 + (index * 250));
        });
    }

    function initTheme() {
        var shell = document.querySelector("[data-admin-theme]");
        var toggle = document.querySelector("[data-admin-theme-toggle]");
        if (!shell || !toggle) return;
        var isOup = document.body.classList.contains("employee-card-context-oup");
        var storageKey = isOup ? "oup-theme" : "admin-theme";

        function applyTheme(theme) {
            var isNight = theme === "night";
            shell.classList.toggle("admin-night", isNight);
            shell.classList.toggle("admin-day", !isNight);
            toggle.dataset.themeIcon = isNight ? "moon" : "sun";
            toggle.setAttribute("aria-label", isNight ? "Включить светлую тему" : "Включить темную тему");
            toggle.setAttribute("title", isNight ? "Светлая тема" : "Темная тема");
        }

        var stored = "day";
        try { stored = window.localStorage.getItem(storageKey) || "day"; } catch (error) {}
        applyTheme(stored);
        toggle.addEventListener("click", function () {
            var next = shell.classList.contains("admin-night") ? "day" : "night";
            applyTheme(next);
            try { window.localStorage.setItem(storageKey, next); } catch (error) {}
        });
    }

    function phoneDigits(value) {
        var digits = String(value || "").replace(/\D/g, "");
        if (digits.length && digits.charAt(0) === "8") digits = "7" + digits.slice(1);
        if (digits.length && digits.charAt(0) === "9") digits = "7" + digits;
        return digits.slice(0, 11);
    }

    function formatPhone(value) {
        var digits = phoneDigits(value);
        if (!digits) return "";
        if (digits.charAt(0) !== "7") return digits;
        var result = "+7";
        if (digits.length > 1) result += " " + digits.slice(1, 4);
        if (digits.length > 4) result += " " + digits.slice(4, 7);
        if (digits.length > 7) result += "-" + digits.slice(7, 9);
        if (digits.length > 9) result += "-" + digits.slice(9, 11);
        return result;
    }

    function initPhone() {
        var input = document.querySelector("[data-employee-phone]");
        var form = document.getElementById("employee-card-form");
        if (!input) return;

        function validatePhone() {
            var digits = phoneDigits(input.value);
            var valid = (!digits && !input.required) || (digits.length === 11 && digits.slice(0, 2) === "79");
            input.setCustomValidity(valid ? "" : "Укажите российский мобильный номер в формате +7 900 000-00-00.");
            input.setAttribute("aria-invalid", valid ? "false" : "true");
            return valid;
        }

        input.value = formatPhone(input.value);
        input.addEventListener("input", function () {
            var cursorAtEnd = input.selectionStart === input.value.length;
            input.value = formatPhone(input.value);
            input.setCustomValidity("");
            input.setAttribute("aria-invalid", "false");
            if (cursorAtEnd) input.setSelectionRange(input.value.length, input.value.length);
        });
        input.addEventListener("blur", validatePhone);
        if (form) {
            form.addEventListener("submit", function (event) {
                if (!validatePhone()) {
                    event.preventDefault();
                    input.reportValidity();
                    input.focus();
                }
            });
        }
    }

    function fieldValue(field) {
        if (!field) return "";
        var value = field.value || "";
        if (field.tagName === "SELECT") {
            var option = field.options[field.selectedIndex];
            value = option ? option.textContent.trim() : "";
        }
        if (field.type === "date" && /^\d{4}-\d{2}-\d{2}$/.test(value)) {
            var parts = value.split("-");
            value = parts[2] + "." + parts[1] + "." + parts[0];
        }
        return value.trim();
    }

    function markCopied(button) {
        button.classList.add("is-copied");
        window.setTimeout(function () { button.classList.remove("is-copied"); }, 1300);
    }

    function initCopyControls() {
        document.querySelectorAll("[data-copy-target]").forEach(function (button) {
            button.addEventListener("click", function () {
                var field = document.querySelector(button.dataset.copyTarget || "");
                var value = fieldValue(field);
                if (!value) {
                    showNotice("Поле пока не заполнено", "info");
                    return;
                }
                copyText(value).then(function () {
                    markCopied(button);
                    showNotice("Значение скопировано");
                }).catch(function () { showNotice("Не удалось скопировать", "error"); });
            });
        });

        var copyCard = document.querySelector("[data-copy-card]");
        if (!copyCard) return;
        copyCard.addEventListener("click", function () {
            var lines = [];
            document.querySelectorAll("[data-card-field]").forEach(function (wrapper) {
                var field = wrapper.querySelector("input:not([type='hidden']), select, textarea");
                var value = fieldValue(field);
                if (value) lines.push((wrapper.dataset.copyLabel || "Поле") + ": " + value);
            });
            copyText(lines.join("\n")).then(function () {
                markCopied(copyCard);
                showNotice("Карточка скопирована");
            }).catch(function () { showNotice("Не удалось скопировать карточку", "error"); });
        });
    }

    function initPrint() {
        var button = document.querySelector("[data-print-card]");
        if (button) button.addEventListener("click", function () { window.print(); });
    }

    function initPhoto() {
        var input = document.querySelector("[data-employee-photo-input]");
        var preview = document.querySelector("[data-employee-photo-preview]");
        var placeholder = document.querySelector("[data-employee-photo-placeholder]");
        if (input && preview) {
            input.addEventListener("change", function () {
                var file = input.files && input.files[0];
                if (!file || !String(file.type).startsWith("image/")) return;
                var objectUrl = window.URL.createObjectURL(file);
                preview.src = objectUrl;
                preview.hidden = false;
                if (placeholder) placeholder.hidden = true;
                preview.onload = function () { window.URL.revokeObjectURL(objectUrl); };
            });
        }

        var open = document.querySelector("[data-photo-open]");
        var modal = document.querySelector("[data-photo-modal]");
        var close = document.querySelector("[data-photo-close]");
        if (!open || !modal) return;
        function closeModal() { modal.hidden = true; }
        open.addEventListener("click", function () { modal.hidden = false; });
        if (close) close.addEventListener("click", closeModal);
        modal.addEventListener("click", function (event) { if (event.target === modal) closeModal(); });
        document.addEventListener("keydown", function (event) { if (event.key === "Escape") closeModal(); });
    }

    function initAccessToggle() {
        var toggle = document.querySelector("input[name='issue_access']");
        var wrap = document.querySelector("[data-access-role-wrap]");
        var select = wrap ? wrap.querySelector("select") : null;
        if (!toggle || !wrap || !select) return;
        function sync() {
            wrap.hidden = !toggle.checked;
            select.disabled = !toggle.checked;
        }
        sync();
        toggle.addEventListener("change", sync);
    }

    function initAssignment() {
        var roleWrap = document.querySelector("[data-employee-assignment-role]");
        var equipmentWrap = document.querySelector("[data-employee-assignment-equipment]");
        var role = roleWrap ? roleWrap.querySelector("select") : null;
        var shift = document.querySelector("select[name='assignment_shift_type']");
        var equipment = equipmentWrap ? equipmentWrap.querySelector("select") : null;
        var status = document.querySelector("[data-employee-assignment-status]");
        var statusTitle = status ? status.querySelector("[data-employee-assignment-status-title]") : null;
        var statusText = status ? status.querySelector("[data-employee-assignment-status-text]") : null;
        if (!role || !shift || !equipment) return;

        function updateStatus() {
            if (!statusTitle || !statusText || status.classList.contains("is-warning")) return;
            var shiftOption = shift.options[shift.selectedIndex];
            var equipmentOption = equipment.options[equipment.selectedIndex];
            if (shift.value && equipmentOption && equipmentOption.value) {
                statusTitle.textContent = shiftOption.textContent.trim() + " · " + (equipmentOption.dataset.baseLabel || equipmentOption.textContent).trim();
                statusText.textContent = "Назначение будет сохранено вместе с карточкой.";
            } else {
                statusTitle.textContent = "Рабочее назначение не задано";
                statusText.textContent = "Смена и техника могут быть назначены позднее.";
            }
        }

        function filterEquipment() {
            var selectedRole = role.options[role.selectedIndex];
            var roleCode = selectedRole ? selectedRole.dataset.workRole || "" : "";
            var supportsEquipment = Array.prototype.some.call(equipment.options, function (option) {
                return option.value && option.dataset.workRole === roleCode;
            });
            if (!supportsEquipment) shift.value = "";
            shift.disabled = !supportsEquipment;
            var shiftType = shift.value;
            var selectedStillAllowed = false;
            Array.prototype.forEach.call(equipment.options, function (option) {
                if (!option.value) return;
                var roleAllowed = supportsEquipment && option.dataset.workRole === roleCode;
                var busyBy = shiftType ? option.dataset[shiftType === "day" ? "busyDay" : "busyNight"] || "" : "";
                var baseLabel = option.dataset.baseLabel || option.textContent.split(" — занято:")[0];
                option.textContent = busyBy ? baseLabel + " — занято: " + busyBy : baseLabel;
                option.hidden = !roleAllowed;
                option.disabled = !roleAllowed || Boolean(busyBy);
                if (option.selected && !option.disabled) selectedStillAllowed = true;
            });
            if (!selectedStillAllowed && equipment.value) equipment.value = "";
            equipment.disabled = !supportsEquipment || !shiftType;
            updateStatus();
        }

        filterEquipment();
        role.addEventListener("change", filterEquipment);
        shift.addEventListener("change", filterEquipment);
        equipment.addEventListener("change", updateStatus);
    }

    function loginMessage(container) {
        return [
            container.dataset.loginUrl || "https://driverform.ru",
            "Телефон: " + (container.dataset.loginPhone || "—"),
            "Пин код: " + (container.dataset.loginCode || "—")
        ].join("\n");
    }

    function initLoginShare() {
        document.querySelectorAll("[data-login-share]").forEach(function (container) {
            var copyButton = container.querySelector("[data-login-copy]");
            var shareButton = container.querySelector("[data-login-share-button]");
            var message = loginMessage(container);
            if (copyButton) {
                copyButton.addEventListener("click", function () {
                    copyText(message).then(function () { showNotice("Данные для входа скопированы"); });
                });
            }
            if (shareButton) {
                shareButton.addEventListener("click", function () {
                    if (navigator.share) {
                        navigator.share({title: "Данные для входа", text: message}).catch(function () {});
                    } else {
                        copyText(message).then(function () { showNotice("Данные скопированы для отправки"); });
                    }
                });
            }
        });
    }

    document.addEventListener("DOMContentLoaded", function () {
        initTheme();
        initNotices();
        initPhone();
        initCopyControls();
        initPrint();
        initPhoto();
        initAccessToggle();
        initAssignment();
        initLoginShare();
    });
})();
