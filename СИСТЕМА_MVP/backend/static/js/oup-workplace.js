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
            toggle.setAttribute("aria-label", isNight ? "Включить дневную тему" : "Включить ночную тему");
            toggle.setAttribute("title", isNight ? "Дневная тема" : "Ночная тема");
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

    document.addEventListener("DOMContentLoaded", function () {
        initTheme();
        initPhotoPreview();
    });
})();
