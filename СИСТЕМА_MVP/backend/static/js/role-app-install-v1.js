(function (root, factory) {
    "use strict";

    var api = factory();
    if (typeof module === "object" && module.exports) {
        module.exports = api;
    }
    if (root && root.document) {
        api.init(root, root.document);
    }
}(typeof window !== "undefined" ? window : null, function () {
    "use strict";

    function isStandalone(win) {
        return Boolean(
            (win.matchMedia && win.matchMedia("(display-mode: standalone)").matches) ||
            (win.navigator && win.navigator.standalone === true)
        );
    }

    function isAndroidYandex(win) {
        var userAgent = String((win.navigator && win.navigator.userAgent) || "");
        return /android/i.test(userAgent) && /yabrowser|yandexbrowser/i.test(userAgent);
    }

    function manualInstruction(win) {
        var userAgent = String((win.navigator && win.navigator.userAgent) || "");
        if (/iphone|ipad|ipod/i.test(userAgent)) {
            return "На iPhone: нажмите «Поделиться», затем «На экран Домой».";
        }
        if (isAndroidYandex(win)) {
            return "Яндекс Браузер может создать обычный ярлык с нижней строкой поиска. Удалите такой ярлык, откройте эту страницу в Google Chrome и выберите «Установить приложение».";
        }
        return "Если окно установки не появилось, откройте меню браузера и выберите «Установить приложение».";
    }

    function initBrowserModeWarning(win, doc) {
        var warning = doc.querySelector("[data-pwa-browser-mode-warning]");
        if (!warning || !isAndroidYandex(win) || isStandalone(win)) return false;

        var closeButton = warning.querySelector("[data-pwa-browser-warning-close]");
        var copyButton = warning.querySelector("[data-pwa-browser-warning-copy]");
        var copyStatus = warning.querySelector("[data-pwa-browser-warning-copy-status]");
        warning.hidden = false;

        if (closeButton) {
            closeButton.addEventListener("click", function () {
                warning.hidden = true;
            });
        }
        if (copyButton) {
            copyButton.addEventListener("click", function () {
                var targetUrl = String((win.location && win.location.origin) || "") + "/";
                var clipboard = win.navigator && win.navigator.clipboard;
                if (!clipboard || typeof clipboard.writeText !== "function") {
                    if (copyStatus) copyStatus.textContent = "Откройте адрес приложения в Google Chrome вручную.";
                    return;
                }
                Promise.resolve(clipboard.writeText(targetUrl)).then(function () {
                    copyButton.textContent = "Адрес скопирован";
                    if (copyStatus) copyStatus.textContent = "Откройте Google Chrome, вставьте адрес и нажмите «Установить приложение».";
                }).catch(function () {
                    if (copyStatus) copyStatus.textContent = "Не удалось скопировать. Откройте адрес приложения в Google Chrome вручную.";
                });
            });
        }
        return true;
    }

    function init(win, doc) {
        initBrowserModeWarning(win, doc);
        var panel = doc.querySelector("[data-role-app-install]");
        if (!panel) return;
        var button = panel.querySelector("[data-install-button]");
        var status = panel.querySelector("[data-install-status]");
        if (!button || !status) return;

        var deferredPrompt = null;

        if (isAndroidYandex(win)) {
            panel.classList.add("is-browser-limited");
            button.textContent = "Как установить правильно";
            status.textContent = manualInstruction(win);
        }

        function markInstalled() {
            deferredPrompt = null;
            panel.classList.remove("is-ready");
            panel.classList.add("is-installed");
            button.disabled = true;
            button.textContent = "Приложение уже установлено";
            status.textContent = "Откройте его с главного экрана телефона.";
        }

        if (isStandalone(win)) {
            markInstalled();
            return;
        }

        win.addEventListener("beforeinstallprompt", function (event) {
            event.preventDefault();
            deferredPrompt = event;
            panel.classList.remove("is-browser-limited");
            panel.classList.add("is-ready");
            button.textContent = "Установить приложение";
            status.textContent = "Приложение готово к установке на это устройство.";
        });

        win.addEventListener("appinstalled", markInstalled);

        button.addEventListener("click", function () {
            if (!deferredPrompt) {
                status.textContent = manualInstruction(win);
                return;
            }
            var promptEvent = deferredPrompt;
            deferredPrompt = null;
            promptEvent.prompt();
            Promise.resolve(promptEvent.userChoice).then(function (choice) {
                if (choice && choice.outcome === "accepted") {
                    status.textContent = "Установка началась. Дождитесь появления иконки на экране.";
                } else {
                    status.textContent = "Установку можно запустить позже из меню браузера.";
                }
            }).catch(function () {
                status.textContent = manualInstruction(win);
            });
        });
    }

    return {
        init: init,
        isStandalone: isStandalone,
        isAndroidYandex: isAndroidYandex,
        initBrowserModeWarning: initBrowserModeWarning,
        manualInstruction: manualInstruction
    };
}));
