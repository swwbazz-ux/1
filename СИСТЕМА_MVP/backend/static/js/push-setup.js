(function (window, document) {
    "use strict";

    /* Экран первичной настройки уведомлений.

       Заканчивается настоящим проверочным уведомлением: доставку подтверждает
       фоновый модуль, отметив уведомление показанным. Всплыл ли при этом
       баннер поверх экрана, из браузера узнать нельзя — про это спрашиваем
       самого человека, он тут единственный, кто это видит. */

    var root = document.querySelector("[data-push-setup]");
    if (!root) return;

    var DONE_KEY = "push-setup-done";
    var POSTPONE_KEY = "push-setup-postponed-until";
    var POSTPONE_MS = 24 * 60 * 60 * 1000;
    var TEST_SENT_KEY = "push-setup-test-sent";
    /* Дольше этого ждать проверку бессмысленно: человек уже ушёл работать. */
    var TEST_SENT_TTL = 10 * 60 * 1000;
    var TEST_KIND = "setup_test";
    var WAIT_MS = 20000;
    var POLL_MS = 1500;

    function store(key, value) {
        try { window.localStorage.setItem(key, value); } catch (error) {}
    }

    function read(key) {
        try { return window.localStorage.getItem(key) || ""; } catch (error) { return ""; }
    }

    function csrfToken() {
        var prefix = "csrftoken=";
        var item = document.cookie.split(";").map(function (part) {
            return part.trim();
        }).find(function (part) {
            return part.indexOf(prefix) === 0;
        });
        if (item) return decodeURIComponent(item.slice(prefix.length));
        var meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.content : "";
    }

    function show(name) {
        root.querySelectorAll("[data-push-setup-stage]").forEach(function (stage) {
            stage.hidden = stage.dataset.pushSetupStage !== name;
        });
        root.hidden = false;
    }

    function close() {
        root.hidden = true;
    }

    function forget() {
        try { window.localStorage.removeItem(TEST_SENT_KEY); } catch (error) {}
    }

    function finish() {
        forget();
        store(DONE_KEY, "1");
        show("done");
    }

    function postpone() {
        forget();
        store(POSTPONE_KEY, String(Date.now() + POSTPONE_MS));
        close();
    }

    /* Ждём, пока фоновый модуль отчитается, что показал проверку. Это и есть
       доказательство доставки: до телефона дошло и он это отрисовал. */
    function waitForDelivery() {
        var deadline = Date.now() + WAIT_MS;
        return new Promise(function (resolve) {
            function poll() {
                window.fetch("/push/pending/", {
                    credentials: "same-origin",
                    cache: "no-store",
                    headers: {"X-Requested-With": "XMLHttpRequest"}
                }).then(function (response) {
                    return response.ok ? response.json() : null;
                }).then(function (payload) {
                    var waiting = !payload || !payload.ok || (payload.notifications || []).some(function (item) {
                        return item.kind === TEST_KIND;
                    });
                    if (!waiting) return resolve(true);
                    if (Date.now() >= deadline) return resolve(false);
                    window.setTimeout(poll, POLL_MS);
                }).catch(function () {
                    if (Date.now() >= deadline) return resolve(false);
                    window.setTimeout(poll, POLL_MS);
                });
            }
            window.setTimeout(poll, POLL_MS);
        });
    }

    function runTest() {
        show("sending");
        /* Нажав на само уведомление, человек открывает приложение заново —
           страница перезагружается и ожидание обрывается. Помним, что проверка
           уже отправлена, чтобы вернуть его к вопросу, а не к началу круга. */
        store(TEST_SENT_KEY, String(Date.now()));
        return window.fetch("/push/test/", {
            method: "POST",
            credentials: "same-origin",
            cache: "no-store",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrfToken(),
                "X-Requested-With": "XMLHttpRequest"
            },
            body: "{}"
        }).then(function (response) {
            return response.ok ? waitForDelivery() : false;
        }).then(function (delivered) {
            show(delivered ? "ask" : "none");
        }).catch(function () {
            show("none");
        });
    }

    function requestPermission(button) {
        var push = window.AppPushNotifications;
        if (!push || !push.supported()) {
            close();
            return;
        }
        button.disabled = true;
        push.enable({interactive: true}).then(function (ok) {
            button.disabled = false;
            if (!ok) {
                show(window.Notification.permission === "denied" ? "denied" : "none");
                return;
            }
            runTest();
        });
    }

    root.querySelectorAll("[data-push-setup-allow]").forEach(function (button) {
        button.addEventListener("click", function () { requestPermission(button); });
    });
    root.querySelectorAll("[data-push-setup-later]").forEach(function (button) {
        button.addEventListener("click", postpone);
    });
    root.querySelectorAll("[data-push-setup-close]").forEach(function (button) {
        button.addEventListener("click", close);
    });
    root.querySelectorAll("[data-push-setup-accept]").forEach(function (button) {
        /* Уведомления доходят, баннер человек включать не стал. Настройка
           выполнена: возвращать его сюда каждый запуск незачем. */
        button.addEventListener("click", function () { forget(); store(DONE_KEY, "1"); close(); });
    });
    root.querySelectorAll("[data-push-setup-retry]").forEach(function (button) {
        button.addEventListener("click", function () {
            var push = window.AppPushNotifications;
            if (!push) return;
            if (window.Notification && window.Notification.permission !== "granted") {
                requestPermission(button);
                return;
            }
            /* Разрешение вернули в настройках телефона — подписку надо поднять
               заново, старая могла не создаться. */
            push.enable({interactive: false}).then(runTest);
        });
    });
    root.querySelectorAll("[data-push-setup-answer]").forEach(function (button) {
        button.addEventListener("click", function () {
            var answer = button.dataset.pushSetupAnswer;
            if (answer === "banner") return finish();
            if (answer === "silent") return show("tune");
            show("none");
        });
    });

    function start() {
        var push = window.AppPushNotifications;
        if (!push || !push.supported()) return;
        if (read(DONE_KEY) === "1") return;
        var until = Number(read(POSTPONE_KEY) || 0);
        if (until && Date.now() < until) return;
        if (window.Notification.permission === "denied") {
            show("denied");
            return;
        }
        var sentAt = Number(read(TEST_SENT_KEY) || 0);
        if (sentAt && Date.now() - sentAt < TEST_SENT_TTL) {
            show("sending");
            waitForDelivery().then(function (delivered) {
                show(delivered ? "ask" : "none");
            });
            return;
        }
        show("permission");
    }

    /* Ждём фоновый модуль: он подключается с defer и создаёт AppPushNotifications. */
    if (document.readyState === "complete") {
        start();
    } else {
        window.addEventListener("load", start);
    }
})(window, document);
