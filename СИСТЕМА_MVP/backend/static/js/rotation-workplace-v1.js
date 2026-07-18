(function () {
    "use strict";

    function initTheme() {
        var shell = document.querySelector("[data-admin-theme]");
        var toggle = document.querySelector("[data-admin-theme-toggle]");
        if (!shell) return;

        function applyTheme(theme) {
            var isNight = theme === "night";
            shell.classList.toggle("admin-night", isNight);
            shell.classList.toggle("admin-day", !isNight);
            if (!toggle) return;
            toggle.setAttribute("data-theme-icon", isNight ? "moon" : "sun");
            toggle.setAttribute("aria-label", isNight ? "Включить светлую тему" : "Включить тёмную тему");
            toggle.setAttribute("title", isNight ? "Светлая тема" : "Тёмная тема");
        }

        var savedTheme = "day";
        try {
            savedTheme = window.localStorage.getItem("admin-theme") || "day";
        } catch (error) {}
        applyTheme(savedTheme);

        if (!toggle) return;
        toggle.addEventListener("click", function () {
            var nextTheme = shell.classList.contains("admin-night") ? "day" : "night";
            applyTheme(nextTheme);
            try {
                window.localStorage.setItem("admin-theme", nextTheme);
            } catch (error) {}
        });
    }

    function setControlsDisabled(container, disabled) {
        if (!container) return;
        container.querySelectorAll("input, select, textarea").forEach(function (control) {
            if (control.type !== "hidden") control.disabled = disabled;
        });
    }

    function initResponseForm() {
        var form = document.querySelector("[data-rotation-response-form]");
        if (!form) return;

        var intent = form.querySelector("[name='intent']");
        var sections = Array.prototype.slice.call(form.querySelectorAll("[data-intent-section]"));
        var intentOnlyFields = Array.prototype.slice.call(form.querySelectorAll("[data-intent-only]"));
        var details = form.querySelector("[data-response-details]");
        var comment = form.querySelector("[name='comment']");

        if (!intent) return;

        function updateConditionalFields() {
            var value = intent.value || "";

            sections.forEach(function (section) {
                var allowed = (section.getAttribute("data-intents") || "")
                    .split(/\s+/)
                    .filter(Boolean);
                var isVisible = allowed.indexOf(value) !== -1;
                section.hidden = !isVisible;
                section.setAttribute("aria-hidden", isVisible ? "false" : "true");
                setControlsDisabled(section, !isVisible);
            });

            intentOnlyFields.forEach(function (field) {
                var isVisible = field.getAttribute("data-intent-only") === value;
                field.hidden = !isVisible;
                field.setAttribute("aria-hidden", isVisible ? "false" : "true");
                setControlsDisabled(field, !isVisible);
            });

            if (details) {
                var showDetails = Boolean(value);
                details.hidden = !showDetails;
                details.setAttribute("aria-hidden", showDetails ? "false" : "true");
                setControlsDisabled(details, !showDetails);
            }

            if (comment) {
                var needsReason = value === "extension";
                comment.required = needsReason;
                comment.setAttribute("aria-required", needsReason ? "true" : "false");
            }
        }

        intent.addEventListener("change", updateConditionalFields);
        updateConditionalFields();
    }

    function initCopyLink() {
        var button = document.querySelector("[data-copy-rotation-link]");
        if (!button) return;
        button.addEventListener("click", function () {
            var value = button.getAttribute("data-copy-rotation-link") || "";
            if (!value) return;
            var originalText = button.textContent;
            function showResult(text) {
                button.textContent = text;
                window.setTimeout(function () { button.textContent = originalText; }, 1800);
            }
            function fallbackCopy() {
                var field = document.createElement("textarea");
                field.value = value;
                field.setAttribute("readonly", "");
                field.style.position = "fixed";
                field.style.opacity = "0";
                document.body.appendChild(field);
                field.select();
                var copied = false;
                try { copied = document.execCommand("copy"); } catch (error) {}
                field.remove();
                showResult(copied ? "Ссылка скопирована" : "Не удалось скопировать");
            }
            if (navigator.clipboard && window.isSecureContext) {
                navigator.clipboard.writeText(value).then(
                    function () { showResult("Ссылка скопирована"); },
                    fallbackCopy
                );
                return;
            }
            fallbackCopy();
        });
    }

    initTheme();
    initResponseForm();
    initCopyLink();
})();
