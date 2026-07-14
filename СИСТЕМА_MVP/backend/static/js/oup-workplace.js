(function () {
    "use strict";

    function initTheme() {
        var shell = document.querySelector("[data-admin-theme]");
        var toggle = document.querySelector("[data-admin-theme-toggle]");
        if (!shell || !toggle) return;

        function applyTheme(theme) {
            var isNight = theme === "night";
            shell.classList.toggle("admin-night", isNight);
            shell.classList.toggle("admin-day", !isNight);
            toggle.dataset.themeIcon = isNight ? "moon" : "sun";
            toggle.setAttribute("aria-label", isNight ? "Включить светлую тему" : "Включить тёмную тему");
            toggle.setAttribute("title", isNight ? "Светлая тема" : "Тёмная тема");
        }

        var storedTheme = "day";
        try {
            storedTheme = window.localStorage.getItem("oup-theme") || "day";
        } catch (error) {}
        applyTheme(storedTheme);

        toggle.addEventListener("click", function () {
            var nextTheme = shell.classList.contains("admin-night") ? "day" : "night";
            applyTheme(nextTheme);
            try {
                window.localStorage.setItem("oup-theme", nextTheme);
            } catch (error) {}
        });
    }

    function initPhotoPreview() {
        var input = document.querySelector("[data-oup-photo-input]");
        var preview = document.querySelector("[data-oup-photo-preview]");
        var placeholder = document.querySelector("[data-oup-photo-placeholder]");
        if (!input || !preview) return;

        input.addEventListener("change", function () {
            var file = input.files && input.files[0];
            if (!file) return;
            var objectUrl = window.URL.createObjectURL(file);
            preview.src = objectUrl;
            preview.hidden = false;
            if (placeholder) placeholder.hidden = true;
            preview.onload = function () {
                window.URL.revokeObjectURL(objectUrl);
            };
        });
    }

    function copyText(value) {
        if (navigator.clipboard && window.isSecureContext) {
            return navigator.clipboard.writeText(value);
        }
        var input = document.createElement("textarea");
        input.value = value;
        input.setAttribute("readonly", "");
        input.style.position = "fixed";
        input.style.opacity = "0";
        document.body.appendChild(input);
        input.select();
        var copied = document.execCommand("copy");
        input.remove();
        return copied ? Promise.resolve() : Promise.reject(new Error("copy_failed"));
    }

    function initCopyButtons() {
        document.querySelectorAll("[data-copy-value]").forEach(function (button) {
            button.addEventListener("click", function () {
                var originalLabel = button.textContent;
                copyText(button.dataset.copyValue || "").then(function () {
                    button.textContent = "Скопировано";
                    button.classList.add("is-copied");
                    window.setTimeout(function () {
                        button.textContent = originalLabel;
                        button.classList.remove("is-copied");
                    }, 1600);
                }).catch(function () {
                    button.textContent = "Не скопировано";
                });
            });
        });
    }

    function initEmployeeLiveFilter() {
        var form = document.querySelector("[data-oup-employee-filter-form]");
        var search = document.querySelector("[data-oup-employee-search]");
        if (!form || !search) return;

        var rows = Array.prototype.slice.call(document.querySelectorAll("[data-oup-employee-row]"));
        var count = document.querySelector("[data-oup-visible-count]");
        var empty = document.querySelector("[data-oup-live-empty]");
        var submit = form.querySelector("[data-oup-filter-submit]");

        function normalize(value) {
            return (value || "").toLocaleLowerCase("ru-RU").replace(/ё/g, "е").trim();
        }

        function digits(value) {
            return (value || "").replace(/\D/g, "");
        }

        function applySearch() {
            var query = normalize(search.value);
            var numericQuery = digits(query);
            var isNumericQuery = Boolean(numericQuery) && !query.replace(/[\d\s()+\-]/g, "");
            var visible = 0;

            rows.forEach(function (row) {
                var value = normalize(row.dataset.employeeSearch);
                var matches = !query || value.indexOf(query) !== -1;
                if (!matches && isNumericQuery) {
                    matches = digits(value).indexOf(numericQuery) !== -1;
                }
                row.hidden = !matches;
                if (matches) visible += 1;
            });

            if (count) count.textContent = visible + " найдено";
            if (empty) empty.hidden = visible !== 0;
        }

        function submitFilters() {
            if (form.requestSubmit) form.requestSubmit();
            else form.submit();
        }

        if (submit) submit.hidden = true;
        form.querySelectorAll("select").forEach(function (select) {
            select.addEventListener("change", submitFilters);
        });
        search.addEventListener("input", applySearch);
        search.addEventListener("keydown", function (event) {
            if (event.key === "Enter") {
                event.preventDefault();
                applySearch();
            }
        });
        applySearch();
    }

    document.addEventListener("DOMContentLoaded", function () {
        initTheme();
        initPhotoPreview();
        initCopyButtons();
        initEmployeeLiveFilter();
    });
})();
