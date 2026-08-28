(function (window, document) {
    "use strict";

    /* Пропуск администратора в чужое приложение.

       Здесь две разные обязанности, и раньше они были склеены в одну:
       переносить пропуск по страницам и запрещать действия. Из-за этого режим
       управления оставался вовсе без скрипта — пропуск жил до первой ссылки, а
       дальше администратора выбрасывало из приложения.

       Переносить пропуск нужно всегда: он живёт в адресе, а не в сессии, —
       настоящую сессию сотрудника мы намеренно не трогаем, чтобы человека не
       выбило из смены. Запрещать действия нужно только в наблюдении. */

    var body = document.body;
    if (!body || body.dataset.observerMode !== "true") return;
    var token = body.dataset.observerToken || "";
    if (!token) return;

    var readOnly = body.dataset.observerControl !== "true";

    /* Адреса, ведущие из приложения наружу: пропуск туда тащить незачем. */
    var EXIT_PATHS = ["/", "/login/", "/logout/", "/activate-access/", "/home/"];

    function sameOrigin(url) {
        return url.origin === window.location.origin;
    }

    function carriesToken(url) {
        return !url.pathname.startsWith("/static/") && !url.pathname.startsWith("/media/");
    }

    var nativeFetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
        var requestUrl = typeof input === "string" ? input : input.url;
        var url;
        try {
            url = new URL(requestUrl, window.location.href);
        } catch (error) {
            return nativeFetch(input, init);
        }
        var options = Object.assign({}, init || {});
        var method = String(options.method || (input && input.method) || "GET").toUpperCase();
        if (sameOrigin(url)) {
            if (readOnly && !(["GET", "HEAD", "OPTIONS"].includes(method))) {
                return Promise.resolve(new Response(
                    "Режим наблюдения не разрешает изменяющие действия.",
                    {status: 403, headers: {"Content-Type": "text/plain; charset=utf-8"}}
                ));
            }
            var headers = new Headers(options.headers || (input && input.headers) || {});
            headers.set("X-Observer-Token", token);
            options.headers = headers;
        }
        return nativeFetch(input, options);
    };

    function eachIn(scope, selector) {
        var found = Array.from(scope.querySelectorAll(selector));
        if (scope.matches && scope.matches(selector)) found.unshift(scope);
        return found;
    }

    function tagLinks(scope) {
        eachIn(scope, "a[href]").forEach(function (link) {
            var url;
            try {
                url = new URL(link.href, window.location.href);
            } catch (error) {
                return;
            }
            if (!sameOrigin(url) || !carriesToken(url)) return;
            if (EXIT_PATHS.includes(url.pathname)) {
                link.removeAttribute("href");
                link.setAttribute("aria-disabled", "true");
                link.dataset.observerBlocked = "true";
                return;
            }
            url.searchParams.set("observe", token);
            link.href = url.toString();
        });
    }

    function tagForms(scope) {
        /* Форма без action уходит на текущий адрес вместе с пропуском, а вот
           с явным action пропуск теряется — дописываем его руками. */
        eachIn(scope, "form").forEach(function (form) {
            var action = form.getAttribute("action");
            if (!action) return;
            var url;
            try {
                url = new URL(action, window.location.href);
            } catch (error) {
                return;
            }
            if (!sameOrigin(url) || !carriesToken(url)) return;
            url.searchParams.set("observe", token);
            form.setAttribute("action", url.pathname + url.search + url.hash);
        });
    }

    function tagImages(scope) {
        /* Фото сотрудника отдаётся защищённым маршрутом (/media/employee_photos/…),
           не обычным файлом статики — без пропуска в адресе он видит анонимный
           запрос и честно отказывает. Обычный /media/ carriesToken() нарочно
           пропускает как публичный, здесь — отдельная, более узкая проверка. */
        eachIn(scope, "img[src]").forEach(function (img) {
            var url;
            try {
                url = new URL(img.src, window.location.href);
            } catch (error) {
                return;
            }
            if (!sameOrigin(url) || !url.pathname.startsWith("/media/employee_photos/")) return;
            if (url.searchParams.has("observe")) return;
            url.searchParams.set("observe", token);
            img.src = url.toString();
        });
    }

    function blockControls(scope) {
        eachIn(scope, "form").forEach(function (form) {
            form.dataset.observerBlocked = "true";
        });
        eachIn(scope, "button, input, select, textarea").forEach(function (control) {
            control.disabled = true;
            control.setAttribute("aria-disabled", "true");
            control.dataset.observerBlocked = "true";
        });
    }

    function apply(scope) {
        tagLinks(scope);
        tagImages(scope);
        if (readOnly) {
            blockControls(scope);
        } else {
            tagForms(scope);
        }
    }

    if (readOnly) {
        document.addEventListener("submit", function (event) {
            event.preventDefault();
            event.stopImmediatePropagation();
        }, true);
    }

    apply(document);
    new MutationObserver(function (mutations) {
        mutations.forEach(function (mutation) {
            mutation.addedNodes.forEach(function (node) {
                if (node.nodeType === 1) apply(node);
            });
        });
    }).observe(document.documentElement, {childList: true, subtree: true});
})(window, document);
