(function (window, document) {
    "use strict";

    var body = document.body;
    if (!body || body.dataset.observerMode !== "true") return;
    var token = body.dataset.observerToken || "";
    if (!token) return;

    var nativeFetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
        var requestUrl = typeof input === "string" ? input : input.url;
        var url = new URL(requestUrl, window.location.href);
        var options = Object.assign({}, init || {});
        var method = String(options.method || (input && input.method) || "GET").toUpperCase();
        if (url.origin === window.location.origin) {
            if (!(["GET", "HEAD", "OPTIONS"].includes(method))) {
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

    function protectControls(root) {
        var scope = root || document;
        var forms = Array.from(scope.querySelectorAll("form"));
        if (scope.matches && scope.matches("form")) forms.unshift(scope);
        forms.forEach(function (form) {
            form.dataset.observerBlocked = "true";
        });
        var controls = Array.from(scope.querySelectorAll("button, input, select, textarea"));
        if (scope.matches && scope.matches("button, input, select, textarea")) controls.unshift(scope);
        controls.forEach(function (control) {
            control.disabled = true;
            control.setAttribute("aria-disabled", "true");
            control.dataset.observerBlocked = "true";
        });
        var links = Array.from(scope.querySelectorAll("a[href]"));
        if (scope.matches && scope.matches("a[href]")) links.unshift(scope);
        links.forEach(function (link) {
            var url;
            try {
                url = new URL(link.href, window.location.href);
            } catch (error) {
                return;
            }
            if (url.origin !== window.location.origin) return;
            if (url.pathname.startsWith("/static/") || url.pathname.startsWith("/media/")) return;
            if (["/", "/login/", "/logout/", "/activate-access/", "/home/"].includes(url.pathname)) {
                link.removeAttribute("href");
                link.setAttribute("aria-disabled", "true");
                link.dataset.observerBlocked = "true";
                return;
            }
            url.searchParams.set("observe", token);
            link.href = url.toString();
        });
    }

    document.addEventListener("submit", function (event) {
        event.preventDefault();
        event.stopImmediatePropagation();
    }, true);
    protectControls(document);
    new MutationObserver(function (mutations) {
        mutations.forEach(function (mutation) {
            mutation.addedNodes.forEach(function (node) {
                if (node.nodeType === 1) protectControls(node);
            });
        });
    }).observe(document.documentElement, {childList: true, subtree: true});
})(window, document);
