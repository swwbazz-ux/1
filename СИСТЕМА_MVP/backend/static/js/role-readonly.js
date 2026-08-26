(function (window, document) {
    "use strict";

    var SAFE_PATHS = {
        "/": true,
        "/logout/": true,
        "/activate-access/": true,
        // Taking the session back must work while read-only, otherwise the
        // only way out is retyping the PIN.
        "/reclaim-session/": true,
        // Уведомления не меняют рабочие данные и нужны в любом режиме.
        "/push/subscribe/": true,
        "/push/unsubscribe/": true,
        "/push/shown/": true,
        "/push/test/": true
    };
    var SAFE_METHODS = ["GET", "HEAD", "OPTIONS"];
    var BLOCKED_ATTRIBUTE = "data-role-readonly-blocked";
    var CONTROL_SELECTOR = [
        "[data-role-mutation]",
        "[data-mm-mobile-activate-excavator]",
        "[data-mm-mobile-fill-truck-id]",
        "[data-mm-mobile-assigned-truck-id]",
        "[data-mm-mobile-fill-drop]",
        "[data-eo-shift-button]",
        "[data-eo-shift-check]",
        "[data-eo-apply-settings]",
        "[data-eo-downtime-reason-id]",
        "[data-eo-close-event]",
        "[data-driver-shift-open-button]",
        "[data-driver-shift-close-button]",
        "[data-driver-hold-button]",
        "[data-driver-downtime-reason-button]",
        "[data-driver-close-downtime]"
    ].join(",");
    var DRAG_SELECTOR = "[data-dispatcher-drag], [data-dispatcher-drop]";
    var SAFE_CONTROL_SELECTOR = [
        "[data-role-readonly-safe]",
        "[data-driver-tab-open]",
        "[data-driver-shift-logout]",
        "[data-driver-shift-update]",
        "[data-driver-downtime-refresh]",
        "[data-driver-pwa-update-apply]",
        "[data-driver-pwa-update-later]",
        "[data-eo-tab]",
        "[data-eo-logout-button]",
        "[data-eo-refresh-work]",
        "[data-eo-pwa-update-check]",
        "[data-eo-pwa-update-apply]",
        "[data-eo-pwa-update-later]",
        "[data-mm-mobile-nav]",
        "[data-mm-pwa-check-update]",
        "[data-mm-pwa-update-apply]",
        "[data-mm-pwa-update-later]"
    ].join(",");
    var GESTURE_EVENTS = [
        "click",
        "pointerdown",
        "mousedown",
        "touchstart",
        "dragstart",
        "dragover",
        "drop",
        "keydown"
    ];
    var scheduledApply = false;
    var observer = null;

    function normalizedPath(rawUrl) {
        var path = "/";
        try {
            path = new URL(rawUrl || window.location.href, window.location.origin).pathname || "/";
        } catch (error) {
            path = "/";
        }
        if (path !== "/" && path.slice(-1) !== "/") {
            path += "/";
        }
        return path;
    }

    function isSafePath(rawUrl) {
        return SAFE_PATHS[normalizedPath(rawUrl)] === true;
    }

    function normalizedMethod(value) {
        return String(value || "GET").toUpperCase();
    }

    function isSafeMethod(value) {
        var method = normalizedMethod(value);
        return method === "GET" || method === "HEAD" || SAFE_METHODS.indexOf(method) !== -1;
    }

    function formMethod(form) {
        return normalizedMethod(form && (form.getAttribute("method") || form.method));
    }

    function isMutationForm(form) {
        if (!form || form.nodeName !== "FORM") {
            return false;
        }
        return !isSafeMethod(formMethod(form)) && !isSafePath(form.action);
    }

    function roleIsReadonly() {
        return !!document.body && document.body.dataset.roleAccessActive === "false";
    }

    function rememberControlState(control) {
        if (control.dataset.roleReadonlyStateSaved === "true") {
            return;
        }
        control.dataset.roleReadonlyStateSaved = "true";
        control.dataset.roleReadonlyWasDisabled = control.disabled ? "true" : "false";
        control.dataset.roleReadonlyAriaDisabled = control.hasAttribute("aria-disabled")
            ? control.getAttribute("aria-disabled")
            : "__missing__";
        if (control.hasAttribute("draggable")) {
            control.dataset.roleReadonlyDraggable = control.getAttribute("draggable");
        }
    }

    function blockControl(control) {
        if (!control) {
            return;
        }
        rememberControlState(control);
        control.setAttribute(BLOCKED_ATTRIBUTE, "");
        if ("disabled" in control && !control.disabled) {
            control.disabled = true;
        }
        control.setAttribute("aria-disabled", "true");
        if (control.hasAttribute("draggable") && control.getAttribute("draggable") !== "false") {
            control.setAttribute("draggable", "false");
        }
    }

    function restoreControl(control) {
        if (!control || control.dataset.roleReadonlyStateSaved !== "true") {
            return;
        }
        if ("disabled" in control) {
            var wasDisabled = control.dataset.roleReadonlyWasDisabled === "true";
            if (control.disabled !== wasDisabled) {
                control.disabled = wasDisabled;
            }
        }
        if (control.dataset.roleReadonlyAriaDisabled === "__missing__") {
            control.removeAttribute("aria-disabled");
        } else {
            control.setAttribute("aria-disabled", control.dataset.roleReadonlyAriaDisabled);
        }
        if (Object.prototype.hasOwnProperty.call(control.dataset, "roleReadonlyDraggable")) {
            control.setAttribute("draggable", control.dataset.roleReadonlyDraggable);
        }
        control.removeAttribute(BLOCKED_ATTRIBUTE);
        delete control.dataset.roleReadonlyStateSaved;
        delete control.dataset.roleReadonlyWasDisabled;
        delete control.dataset.roleReadonlyAriaDisabled;
        delete control.dataset.roleReadonlyDraggable;
    }

    function mutationFormControls(form) {
        return Array.prototype.filter.call(
            form.querySelectorAll("button, input:not([type='hidden']), select, textarea"),
            function (control) {
                return !control.matches(SAFE_CONTROL_SELECTOR);
            }
        );
    }

    function applyFormState(form, readonly) {
        if (!isMutationForm(form)) {
            return;
        }
        if (readonly) {
            form.setAttribute(BLOCKED_ATTRIBUTE, "");
            mutationFormControls(form).forEach(blockControl);
            return;
        }
        form.removeAttribute(BLOCKED_ATTRIBUTE);
        mutationFormControls(form).forEach(restoreControl);
    }

    function applyExplicitControls(readonly) {
        document.querySelectorAll(CONTROL_SELECTOR).forEach(function (control) {
            if (readonly) {
                blockControl(control);
            } else {
                restoreControl(control);
            }
        });
    }

    function applyDragState(readonly) {
        document.querySelectorAll(DRAG_SELECTOR).forEach(function (node) {
            if (readonly) {
                if (node.dataset.roleReadonlyDragStateSaved !== "true") {
                    node.dataset.roleReadonlyDragStateSaved = "true";
                    node.dataset.roleReadonlyDraggable = node.hasAttribute("draggable")
                        ? node.getAttribute("draggable")
                        : "__missing__";
                }
                node.setAttribute(BLOCKED_ATTRIBUTE, "");
                if (node.hasAttribute("draggable") && node.getAttribute("draggable") !== "false") {
                    node.setAttribute("draggable", "false");
                }
                return;
            }
            if (node.dataset.roleReadonlyDragStateSaved !== "true") {
                return;
            }
            if (node.dataset.roleReadonlyDraggable === "__missing__") {
                node.removeAttribute("draggable");
            } else {
                node.setAttribute("draggable", node.dataset.roleReadonlyDraggable);
            }
            node.removeAttribute(BLOCKED_ATTRIBUTE);
            delete node.dataset.roleReadonlyDragStateSaved;
            delete node.dataset.roleReadonlyDraggable;
        });
    }

    var INACTIVE_BANNER_VISIBLE_MS = 6000;
    var inactiveBannerHideTimer = null;

    function cookieValue(name) {
        var prefix = name + "=";
        var item = document.cookie.split(";").map(function (part) {
            return part.trim();
        }).find(function (part) {
            return part.indexOf(prefix) === 0;
        });
        return item ? decodeURIComponent(item.slice(prefix.length)) : "";
    }

    function cancelInactiveBannerTimer() {
        if (inactiveBannerHideTimer !== null && typeof window.clearTimeout === "function") {
            window.clearTimeout(inactiveBannerHideTimer);
        }
        inactiveBannerHideTimer = null;
    }

    function hideInactiveBannerSoon(banner) {
        cancelInactiveBannerTimer();
        if (typeof window.setTimeout !== "function") {
            return;
        }
        inactiveBannerHideTimer = window.setTimeout(function () {
            banner.classList.add("is-idle");
        }, INACTIVE_BANNER_VISIBLE_MS);
    }

    function revealInactiveBanner(banner) {
        banner.classList.remove("is-idle");
        hideInactiveBannerSoon(banner);
    }

    function buildInactiveBanner() {
        var banner = document.createElement("div");
        banner.className = "app-inactive-role-banner";
        banner.dataset.inactiveRoleBanner = "";
        banner.setAttribute("role", "status");
        banner.setAttribute("aria-live", "polite");
        var text = document.createElement("span");
        text.dataset.inactiveRoleText = "";
        text.textContent = "Вы вошли с другого устройства — доступен только просмотр";
        var button = document.createElement("button");
        button.type = "button";
        button.className = "app-inactive-role-reclaim";
        button.dataset.inactiveRoleReclaim = "";
        button.textContent = "Продолжить здесь";
        banner.appendChild(text);
        banner.appendChild(button);
        return banner;
    }

    /* No screen has room to park this permanently, so it behaves as a toast:
       it says its piece, steps aside, and returns the moment a blocked control
       is pressed. The button gives the driver a way out in one tap instead of
       leaving them to guess that only retyping the PIN helps. */
    function bindInactiveBanner(banner) {
        if (banner.dataset.inactiveRoleBound === "true") {
            return;
        }
        banner.dataset.inactiveRoleBound = "true";
        hideInactiveBannerSoon(banner);

        /* Blocked controls are click-through, so their taps arrive here on the
           panel behind them. Working navigation is excluded — nagging someone
           for switching tabs helps nobody. */
        document.addEventListener("click", function (event) {
            if (
                typeof document.body.contains === "function"
                && !document.body.contains(banner)
            ) {
                return;
            }
            var node = event.target;
            while (node && node !== document.body) {
                if (node.hasAttribute && node.hasAttribute("data-inactive-role-reclaim")) {
                    return;
                }
                if (node.matches && node.matches("nav, [data-driver-bottom-nav], .mm-mobile-bottom-nav")) {
                    return;
                }
                node = node.parentNode;
            }
            revealInactiveBanner(banner);
        }, true);

        var button = banner.querySelector("[data-inactive-role-reclaim]");
        var textNode = banner.querySelector("[data-inactive-role-text]");
        if (!button) {
            return;
        }
        button.addEventListener("click", function () {
            cancelInactiveBannerTimer();
            banner.classList.remove("is-idle");
            button.disabled = true;
            button.textContent = "Возвращаем…";
            var token = cookieValue("csrftoken")
                || (document.querySelector('meta[name="csrf-token"]') || {}).content
                || "";
            window.fetch("/reclaim-session/", {
                method: "POST",
                credentials: "same-origin",
                cache: "no-store",
                headers: {
                    "X-CSRFToken": token,
                    "X-Requested-With": "XMLHttpRequest"
                }
            }).then(function (response) {
                return response.json().catch(function () {
                    return {};
                });
            }).then(function (payload) {
                if (payload && payload.ok) {
                    window.location.reload();
                    return;
                }
                if (textNode) {
                    textNode.textContent = (payload && payload.error)
                        || "Не удалось вернуть смену на это устройство.";
                }
                button.disabled = false;
                button.textContent = "Повторить";
                hideInactiveBannerSoon(banner);
            }).catch(function () {
                if (textNode) {
                    textNode.textContent = "Нет связи с сервером. Попробуйте ещё раз.";
                }
                button.disabled = false;
                button.textContent = "Повторить";
                hideInactiveBannerSoon(banner);
            });
        });
    }

    function ensureInactiveBanner(readonly) {
        var banner = document.querySelector("[data-inactive-role-banner]");
        if (readonly && !banner && document.body) {
            banner = buildInactiveBanner();
            document.body.insertBefore(banner, document.body.firstChild);
        }
        if (readonly && banner) {
            bindInactiveBanner(banner);
        } else if (!readonly && banner) {
            cancelInactiveBannerTimer();
            banner.remove();
        }
    }

    function applyRoleReadonlyState(isActive) {
        if (!document.body) {
            return;
        }
        var readonly = isActive === false;
        document.body.dataset.roleAccessActive = readonly ? "false" : "true";
        document.body.dataset.roleReadonly = readonly ? "true" : "false";
        document.body.classList.toggle("is-role-readonly", readonly);
        ensureInactiveBanner(readonly);
        document.querySelectorAll("form").forEach(function (form) {
            applyFormState(form, readonly);
        });
        applyExplicitControls(readonly);
        applyDragState(readonly);
    }

    function scheduleCurrentStateApply() {
        if (scheduledApply) {
            return;
        }
        scheduledApply = true;
        window.requestAnimationFrame(function () {
            scheduledApply = false;
            applyRoleReadonlyState(!roleIsReadonly());
        });
    }

    function blockedGestureTarget(event) {
        if (!roleIsReadonly() || !event.target || !event.target.closest) {
            return null;
        }
        if (event.target.closest(SAFE_CONTROL_SELECTOR)) {
            return null;
        }
        var form = event.target.closest("form");
        if (form && isMutationForm(form)) {
            return form;
        }
        if (
            event.type === "dragstart"
            || event.type === "dragover"
            || event.type === "drop"
        ) {
            return event.target.closest(DRAG_SELECTOR);
        }
        return event.target.closest(CONTROL_SELECTOR);
    }

    function preventReadonlyGesture(event) {
        var target = blockedGestureTarget(event);
        if (!target) {
            return;
        }
        if (event.type === "keydown" && event.key !== "Enter" && event.key !== " ") {
            return;
        }
        event.preventDefault();
        event.stopImmediatePropagation();
    }

    function installFormBarriers() {
        document.addEventListener("submit", function (event) {
            if (!roleIsReadonly() || !isMutationForm(event.target)) {
                return;
            }
            event.preventDefault();
            event.stopImmediatePropagation();
        }, true);

        if (
            window.HTMLFormElement
            && window.HTMLFormElement.prototype
            && !window.HTMLFormElement.prototype.submit.__roleReadonlyWrapped
        ) {
            var nativeSubmit = window.HTMLFormElement.prototype.submit;
            var guardedSubmit = function () {
                if (roleIsReadonly() && isMutationForm(this)) {
                    applyFormState(this, true);
                    return;
                }
                return nativeSubmit.call(this);
            };
            guardedSubmit.__roleReadonlyWrapped = true;
            window.HTMLFormElement.prototype.submit = guardedSubmit;
        }
    }

    function installFetchBarrier() {
        if (!window.fetch || window.fetch.__roleReadonlyWrapped) {
            return;
        }
        var nativeFetch = window.fetch.bind(window);
        window.fetch = function (input, init) {
            var options = init || {};
            var requestMethod = options.method;
            var requestUrl = input;
            if (window.Request && input instanceof window.Request) {
                requestMethod = requestMethod || input.method;
                requestUrl = input.url;
            }
            var method = normalizedMethod(requestMethod);
            var parsedUrl = null;
            try {
                parsedUrl = new URL(requestUrl || window.location.href, window.location.origin);
            } catch (error) {
                parsedUrl = new URL(window.location.href);
            }
            if (
                roleIsReadonly()
                && parsedUrl.origin === window.location.origin
                && !isSafeMethod(method)
                && !isSafePath(parsedUrl.href)
            ) {
                return Promise.resolve(new Response(
                    JSON.stringify({
                        ok: false,
                        code: "inactive_role",
                        error: "Роль неактивна — доступен только просмотр"
                    }),
                    {
                        status: 409,
                        headers: {"Content-Type": "application/json; charset=utf-8"}
                    }
                ));
            }
            return nativeFetch(input, init);
        };
        window.fetch.__roleReadonlyWrapped = true;
    }

    function installObserver() {
        if (!window.MutationObserver || observer) {
            return;
        }
        observer = new MutationObserver(scheduleCurrentStateApply);
        observer.observe(document.body, {
            childList: true,
            subtree: true,
            attributes: true,
            attributeFilter: ["disabled", "draggable"]
        });
    }

    window.isAppRoleReadonly = roleIsReadonly;
    window.applyAppRoleReadonlyState = applyRoleReadonlyState;

    installFetchBarrier();
    installFormBarriers();
    GESTURE_EVENTS.forEach(function (eventName) {
        document.addEventListener(eventName, preventReadonlyGesture, true);
    });
    window.addEventListener("active-role-state-changed", function (event) {
        var detail = event.detail || {};
        applyRoleReadonlyState(detail.active !== false);
    });

    document.addEventListener("DOMContentLoaded", function () {
        applyRoleReadonlyState(!roleIsReadonly());
        installObserver();
    });
})(window, document);
