/* T-031 B3c: вахтенная смена делопроизводителя в существующей панели
   управления картой. Карта и аренда (acquire/heartbeat/release) остаются в
   settlement-clerk.js; этот файл показывает состояние смены и открывает /
   закрывает / принимает вахту, после чего просит карту взять управление, если
   аренда ещё не у этого экрана. Разметка панели не перерисовывается. */
(function () {
    "use strict";

    var root = document.querySelector("[data-settlement-root]");
    if (!root) return;

    var shiftBlock = root.querySelector("[data-clerk-shift]");
    var shiftStatus = root.querySelector("[data-clerk-shift-status]");
    var shiftWatch = root.querySelector("[data-clerk-shift-watch]");
    var openButton = root.querySelector("[data-clerk-shift-open]");
    var checkinStart = root.querySelector("[data-checkin-start]");
    var closeButton = root.querySelector("[data-clerk-shift-close]");
    var handoverButton = root.querySelector("[data-clerk-shift-handover]");
    var controlFeedback = root.querySelector("[data-control-feedback]");
    var acquireButton = root.querySelector("[data-control-acquire]");

    var state = {
        payload: null,
        loading: false,
        saving: false
    };

    function setHidden(node, hidden) {
        if (!node) return;
        if (node.hidden === Boolean(hidden)) return;
        node.hidden = Boolean(hidden);
    }

    function csrfToken() {
        var input = root.querySelector("input[name='csrfmiddlewaretoken']");
        if (input && input.value) return input.value;
        var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : "";
    }

    function formatIsoDate(value) {
        var parts = String(value || "").split("-");
        if (parts.length !== 3) return String(value || "");
        return parts[2] + "." + parts[1] + "." + parts[0];
    }

    function formatMoment(value) {
        if (!value) return "";
        var moment = new Date(value);
        if (Number.isNaN(moment.getTime())) return String(value);
        return moment.toLocaleString("ru-RU", {
            day: "2-digit",
            month: "2-digit",
            year: "numeric",
            hour: "2-digit",
            minute: "2-digit"
        });
    }

    function showMessage(message, isError) {
        if (!controlFeedback) return;
        controlFeedback.textContent = message;
        controlFeedback.classList.toggle("is-error", Boolean(isError));
        controlFeedback.hidden = !message;
    }

    function askReason(question) {
        var value = window.prompt(question);
        if (value === null) return null;
        value = String(value).trim();
        if (!value) {
            showMessage("Причина обязательна.", true);
            return null;
        }
        return value;
    }

    function requestJson(url, options) {
        var config = options || {};
        return fetch(url, {
            method: config.method || "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrfToken(),
                "X-Requested-With": "XMLHttpRequest"
            },
            body: config.payload === undefined
                ? undefined
                : JSON.stringify(config.payload)
        }).then(function (response) {
            if (response.redirected || response.status === 401) {
                var sessionError = new Error("Сессия истекла, войдите заново.");
                sessionError.httpStatus = response.status;
                throw sessionError;
            }
            return response.json()
                .catch(function () { return {}; })
                .then(function (body) {
                    if (!response.ok || body.ok === false) {
                        var error = new Error(
                            body && body.error
                                ? body.error
                                : "Не удалось выполнить действие со сменой."
                        );
                        error.code = body && body.code ? body.code : "";
                        error.httpStatus = response.status;
                        throw error;
                    }
                    return body;
                });
        });
    }

    /* --- Состояние смены -------------------------------------------------- */

    function blockedByWatch(payload) {
        if (!payload || !payload.watch_error) return false;
        return (
            !payload.can_open
            && !payload.can_close
            && !payload.can_handover
        );
    }

    function shiftMode(payload) {
        if (!payload || blockedByWatch(payload)) return "";
        var openShift = payload.open_shift;
        if (openShift && openShift.is_mine && payload.can_close) return "close";
        if (openShift && !openShift.is_mine && payload.can_handover) return "handover";
        if (!openShift && payload.can_open) return "open";
        return "";
    }

    function syncButtons() {
        var mode = state.saving ? "" : shiftMode(state.payload);
        if (checkinStart) checkinStart.textContent = mode === "open" ? "Открыть смену и начать работу" : "Начать работу";
        if (openButton) {
            setHidden(openButton, mode !== "open");
            openButton.disabled = mode !== "open";
        }
        if (closeButton) {
            setHidden(closeButton, mode !== "close");
            closeButton.disabled = mode !== "close";
        }
        if (handoverButton) {
            setHidden(handoverButton, mode !== "handover");
            handoverButton.disabled = mode !== "handover";
        }
    }

    function renderShiftState(payload) {
        state.payload = payload || {};
        var period = state.payload.watch_period;
        var openShift = state.payload.open_shift;
        var watchError = state.payload.watch_error;

        if (shiftWatch) {
            if (period) {
                shiftWatch.textContent = "Вахта: " + (period.name || "")
                    + " (" + formatIsoDate(period.starts_on)
                    + " — " + formatIsoDate(period.ends_on) + ")";
                setHidden(shiftWatch, false);
            } else {
                shiftWatch.textContent = "";
                setHidden(shiftWatch, true);
            }
        }

        var text = "Смена не открыта";
        if (blockedByWatch(state.payload)) {
            text = watchError.message || "Смена недоступна.";
        } else if (openShift && openShift.is_mine) {
            text = "Смена открыта: " + (openShift.clerk_name || "")
                + ", с " + formatMoment(openShift.opened_at);
        } else if (openShift) {
            text = "Смена открыта другим делопроизводителем: "
                + (openShift.clerk_name || "");
        } else if (watchError && watchError.message) {
            text = watchError.message;
        }
        if (shiftStatus) shiftStatus.textContent = text;
        setHidden(shiftBlock, false);
        syncButtons();
    }

    function showShiftError(message) {
        if (shiftStatus) {
            shiftStatus.textContent = message || "Состояние смены недоступно.";
        }
        setHidden(shiftBlock, false);
        syncButtons();
    }

    /* --- Действия --------------------------------------------------------- */

    function ensureControlLease() {
        if (root.dataset.controlState === "held") return;
        if (!acquireButton) return;
        if (!acquireButton.disabled) {
            acquireButton.click();
            return;
        }
        window.setTimeout(function () {
            if (root.dataset.controlState !== "held" && !acquireButton.disabled) {
                acquireButton.click();
            }
        }, 1200);
    }

    function runAction(url, payload, onSuccess) {
        if (!url || state.saving) return Promise.resolve();
        state.saving = true;
        syncButtons();
        return requestJson(url, {payload: payload || {}})
            .then(function (body) {
                return onSuccess ? onSuccess(body) : loadShiftState();
            })
            .catch(function (error) {
                showMessage(
                    error && error.message
                        ? error.message
                        : "Не удалось выполнить действие со сменой.",
                    true
                );
                if (error && error.httpStatus === 409) {
                    loadShiftState();
                }
            })
            .finally(function () {
                state.saving = false;
                syncButtons();
            });
    }

    function openShift() {
        runAction(root.dataset.clerkShiftOpenUrl, {}, function () {
            showMessage("Смена открыта.", false);
            return loadShiftState().then(ensureControlLease);
        });
    }

    function closeShift() {
        var payload = state.payload && state.payload.open_shift
            ? {shift_id: state.payload.open_shift.id}
            : {};
        runAction(root.dataset.clerkShiftCloseUrl, payload, function () {
            showMessage("Смена закрыта. Для записи начните работу заново.", false);
            return loadShiftState();
        });
    }

    function handoverShift() {
        var reason = askReason("Причина приёма вахты");
        if (!reason) return;
        runAction(
            root.dataset.clerkShiftHandoverUrl,
            {reason: reason},
            function (body) {
                var lease = body && body.lease ? body.lease : null;
                if (lease && lease.acquired) {
                    showMessage("Вахта принята. Управление передано этой сессии.", false);
                    return loadShiftState().then(ensureControlLease);
                }
                showMessage(
                    "Вахта принята. Управление занято другой сессией — запись недоступна.",
                    false
                );
                return loadShiftState();
            }
        );
    }

    function loadShiftState() {
        if (!root.dataset.clerkShiftStateUrl || state.loading) {
            return Promise.resolve();
        }
        state.loading = true;
        return requestJson(root.dataset.clerkShiftStateUrl, {method: "GET"})
            .then(function (payload) {
                renderShiftState(payload);
            })
            .catch(function (error) {
                var message = error && error.message
                    ? error.message
                    : "Не удалось загрузить состояние смены.";
                showShiftError(message);
                if (error && error.httpStatus) {
                    showMessage(message, true);
                }
            })
            .finally(function () {
                state.loading = false;
            });
    }

    /* --- Обработчики ------------------------------------------------------ */

    if (openButton) openButton.addEventListener("click", openShift);
    if (checkinStart) checkinStart.addEventListener("click", function () {
        if (state.saving) return;
        if (shiftMode(state.payload) === "open") openShift();
        else ensureControlLease();
    });
    if (closeButton) closeButton.addEventListener("click", closeShift);
    if (handoverButton) handoverButton.addEventListener("click", handoverShift);

    /* Карта меняет data-control-state при acquire/heartbeat/release: после
       захвата управления чужой сессией состояние смены стоит перечитать. */
    if (typeof window.MutationObserver === "function") {
        var observer = new window.MutationObserver(function () {
            if (state.payload === null) return;
            loadShiftState();
        });
        observer.observe(root, {attributes: true, attributeFilter: ["data-control-state"]});
    }

    loadShiftState();
}());
