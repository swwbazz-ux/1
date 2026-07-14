(function () {
    "use strict";

    function updateThemeLabel() {
        var shell = document.querySelector("[data-admin-theme]");
        var toggle = document.querySelector("[data-admin-theme-toggle]");
        if (!shell || !toggle) return;

        var isDark = shell.classList.contains("admin-night");
        toggle.setAttribute("aria-label", isDark ? "Включить светлую тему" : "Включить тёмную тему");
        toggle.setAttribute("title", isDark ? "Светлая тема" : "Тёмная тема");
    }

    document.addEventListener("DOMContentLoaded", function () {
        var toggle = document.querySelector("[data-admin-theme-toggle]");
        updateThemeLabel();
        if (toggle) toggle.addEventListener("click", updateThemeLabel);
    });
})();
