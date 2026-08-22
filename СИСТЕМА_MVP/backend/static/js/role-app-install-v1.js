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

    function manualInstruction(win) {
        var userAgent = String((win.navigator && win.navigator.userAgent) || "");
        if (/iphone|ipad|ipod/i.test(userAgent)) {
            return "На iPhone: нажмите «Поделиться», затем «На экран Домой».";
        }
        return "Если окно установки не появилось, откройте меню браузера и выберите «Установить приложение».";
    }

    function init(win, doc) {
        var panel = doc.querySelector("[data-role-app-install]");
        if (!panel) return;
        var button = panel.querySelector("[data-install-button]");
        var status = panel.querySelector("[data-install-status]");
        if (!button || !status) return;

        var deferredPrompt = null;

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
            panel.classList.add("is-ready");
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
        manualInstruction: manualInstruction
    };
}));
