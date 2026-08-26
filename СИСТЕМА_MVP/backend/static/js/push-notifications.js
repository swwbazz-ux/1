(function (window, document) {
    "use strict";

    /* Подписка телефона на уведомления и значок на иконке приложения.

       Разрешение не спрашиваем при загрузке: браузер запоминает отказ навсегда,
       и переспросить будет нельзя. Ждём осознанного нажатия на кнопку
       [data-push-enable] либо продолжаем работу, если разрешение уже выдано. */

    var KEY_URL = "/push/key/";
    var SUBSCRIBE_URL = "/push/subscribe/";
    var PENDING_URL = "/push/pending/";

    function cookie(name) {
        var prefix = name + "=";
        var item = document.cookie.split(";").map(function (part) {
            return part.trim();
        }).find(function (part) {
            return part.indexOf(prefix) === 0;
        });
        return item ? decodeURIComponent(item.slice(prefix.length)) : "";
    }

    function csrfToken() {
        var meta = document.querySelector('meta[name="csrf-token"]');
        return cookie("csrftoken") || (meta && meta.content) || "";
    }

    function urlBase64ToUint8Array(value) {
        var padding = "=".repeat((4 - (value.length % 4)) % 4);
        var base64 = (value + padding).replace(/-/g, "+").replace(/_/g, "/");
        var raw = window.atob(base64);
        var output = new Uint8Array(raw.length);
        for (var i = 0; i < raw.length; i += 1) {
            output[i] = raw.charCodeAt(i);
        }
        return output;
    }

    function supported() {
        return Boolean(
            window.navigator &&
            "serviceWorker" in window.navigator &&
            "PushManager" in window &&
            window.Notification
        );
    }

    function setBadge(count) {
        try {
            if (count > 0 && window.navigator.setAppBadge) {
                window.navigator.setAppBadge(count);
            } else if (window.navigator.clearAppBadge) {
                window.navigator.clearAppBadge();
            }
        } catch (error) {}
    }

    function refreshBadge() {
        return window.fetch(PENDING_URL, {
            credentials: "same-origin",
            cache: "no-store",
            headers: {"X-Requested-With": "XMLHttpRequest"}
        }).then(function (response) {
            return response.ok ? response.json() : null;
        }).then(function (payload) {
            if (payload && payload.ok) setBadge(Number(payload.badge) || 0);
        }).catch(function () {});
    }

    function subscribe(registration, publicKey) {
        return registration.pushManager.getSubscription().then(function (existing) {
            if (existing) return existing;
            return registration.pushManager.subscribe({
                userVisibleOnly: true,
                applicationServerKey: urlBase64ToUint8Array(publicKey)
            });
        }).then(function (subscription) {
            if (!subscription) return null;
            var payload = subscription.toJSON ? subscription.toJSON() : {};
            return window.fetch(SUBSCRIBE_URL, {
                method: "POST",
                credentials: "same-origin",
                cache: "no-store",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": csrfToken(),
                    "X-Requested-With": "XMLHttpRequest"
                },
                body: JSON.stringify({
                    endpoint: subscription.endpoint,
                    keys: payload.keys || {}
                })
            }).then(function () {
                return subscription;
            });
        });
    }

    function enable(options) {
        var interactive = !!(options && options.interactive);
        if (!supported()) return Promise.resolve(false);

        return window.fetch(KEY_URL, {credentials: "same-origin", cache: "no-store"})
            .then(function (response) {
                return response.ok ? response.json() : null;
            })
            .then(function (payload) {
                if (!payload || !payload.ok || !payload.configured || !payload.public_key) {
                    return false;
                }
                var permission = window.Notification.permission;
                if (permission === "denied") return false;
                if (permission !== "granted") {
                    /* Спрашиваем только по явному действию человека. */
                    if (!interactive) return false;
                    return window.Notification.requestPermission().then(function (result) {
                        if (result !== "granted") return false;
                        return window.navigator.serviceWorker.ready.then(function (registration) {
                            return subscribe(registration, payload.public_key);
                        }).then(function () {
                            return true;
                        });
                    });
                }
                return window.navigator.serviceWorker.ready.then(function (registration) {
                    return subscribe(registration, payload.public_key);
                }).then(function () {
                    return true;
                });
            })
            .catch(function () {
                return false;
            });
    }

    function bindEnableButtons() {
        document.querySelectorAll("[data-push-enable]").forEach(function (button) {
            if (button.dataset.pushEnableBound === "true") return;
            button.dataset.pushEnableBound = "true";
            button.addEventListener("click", function () {
                button.disabled = true;
                enable({interactive: true}).then(function (ok) {
                    button.disabled = false;
                    button.dataset.pushEnabled = ok ? "true" : "false";
                    if (ok) {
                        refreshBadge();
                        hideInvite();
                    } else {
                        /* Человек нажал «Блокировать» — кнопка больше не поможет. */
                        renderInvite();
                    }
                });
            });
        });
    }

    /* Приглашение включить уведомления в начале смены.

       Показываем только пока разрешение не запрошено. Отказ «Позже» прячет
       карточку до следующей смены и НЕ тратит единственную попытку спросить:
       системное окно всплывает лишь по нажатию «Включить». */
    /* У каждой роли свой поддомен, поэтому хранилище и так раздельное. */
    var DISMISS_KEY = "push-invite-dismissed-shift";

    function inviteNode() {
        return document.querySelector("[data-push-invite]");
    }

    function dismissedShift() {
        try {
            return window.localStorage.getItem(DISMISS_KEY) || "";
        } catch (error) {
            return "";
        }
    }

    function hideInvite() {
        var invite = inviteNode();
        if (invite) invite.hidden = true;
    }

    function renderInvite() {
        var invite = inviteNode();
        if (!invite || !supported()) return;

        var permission = window.Notification.permission;
        if (permission === "granted") {
            invite.hidden = true;
            return;
        }
        if (permission === "denied") {
            /* Кнопка бесполезна: разрешение можно вернуть только в настройках. */
            var title = invite.querySelector("[data-push-title]");
            var text = invite.querySelector("[data-push-text]");
            var actions = invite.querySelector("[data-push-actions]");
            if (title) title.textContent = "Уведомления запрещены";
            if (text) {
                /* Название приложения берём из карточки: экран у каждой роли свой. */
                var appName = invite.dataset.appName || "приложение";
                text.textContent = "События смены придётся смотреть в приложении."
                    + " Включить можно в настройках телефона: " + appName + " → Уведомления.";
            }
            if (actions) actions.hidden = true;
            invite.hidden = false;
            return;
        }
        if (dismissedShift() && dismissedShift() === String(invite.dataset.shiftKey || "")) {
            invite.hidden = true;
            return;
        }
        invite.hidden = false;
    }

    function bindInvite() {
        var invite = inviteNode();
        if (!invite || invite.dataset.pushInviteBound === "true") return;
        invite.dataset.pushInviteBound = "true";
        var later = invite.querySelector("[data-push-later]");
        if (later) {
            later.addEventListener("click", function () {
                try {
                    window.localStorage.setItem(DISMISS_KEY, String(invite.dataset.shiftKey || ""));
                } catch (error) {}
                invite.hidden = true;
            });
        }
    }

    function start() {
        if (!supported()) return;
        bindEnableButtons();
        bindInvite();
        renderInvite();
        /* Уже разрешено — молча продлеваем подписку: она может истечь. */
        if (window.Notification.permission === "granted") {
            enable({interactive: false});
        }
        refreshBadge();
        /* Вернулись в приложение — значок мог устареть. */
        document.addEventListener("visibilitychange", function () {
            if (!document.hidden) refreshBadge();
        });
    }

    window.AppPushNotifications = {
        enable: enable,
        refreshBadge: refreshBadge,
        setBadge: setBadge,
        supported: supported,
        /* Экран водителя перерисовывает вкладки, приглашение надо переподнять. */
        refreshInvite: function () {
            bindEnableButtons();
            bindInvite();
            renderInvite();
        }
    };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start);
    } else {
        start();
    }
})(window, document);
