/* Открытие и закрытие смены водителя, включая очередь закрытий на случай
   работы без связи. Вынесено из driver-shift-v1.js без изменений. */
/* DRIVER_SHIFT_CLOSE_OUTBOX_START */
window.DriverShiftCloseOutbox = (function () {
    var storageKey = "driver-shift-close-pending:v1";
    var retryInFlight = false;
    var retryTimer = null;
    var maxRetryDelayMs = 60000;

    function nativeConnection() {
        return window.NativeBackgroundConnection || null;
    }

    function currentAuthGeneration() {
        var shell = document.querySelector("[data-driver-shell]");
        return shell ? String(shell.dataset.driverAuthGeneration || "") : "";
    }

    function retryDelayMs(attempt) {
        return Math.min(maxRetryDelayMs, 2000 * Math.pow(2, Math.min(Math.max(1, attempt) - 1, 5)));
    }

    function readLocal() {
        try {
            var value = JSON.parse(window.localStorage.getItem(storageKey) || "null");
            return value && value.clientActionId ? value : null;
        } catch (error) {
            return null;
        }
    }

    function writeLocal(payload) {
        try {
            window.localStorage.setItem(storageKey, JSON.stringify(payload));
            return true;
        } catch (error) {
            return false;
        }
    }

    function removeLocal(expectedActionId) {
        var pending = readLocal();
        if (pending && expectedActionId && pending.clientActionId !== expectedActionId) {
            return false;
        }
        try {
            window.localStorage.removeItem(storageKey);
            return true;
        } catch (error) {
            return false;
        }
    }

    function payloadFromForm(form) {
        function field(name) {
            var input = form.querySelector('[name="' + name + '"]');
            return input ? String(input.value || "").trim() : "";
        }
        return {
            shiftId: String(form.dataset.nativeShiftId || "").trim(),
            clientActionId: field("client_action_id"),
            endFuel: field("end_fuel"),
            endMileage: field("end_mileage"),
            endEngineHours: field("end_engine_hours"),
            confirmationToken: field("reading_confirmation_token"),
            authGeneration: currentAuthGeneration(),
            state: "queued",
            createdAt: Date.now(),
            retryAttempts: 0,
            nextAttemptAt: 0,
            requiresAuthentication: false
        };
    }

    function fillForm(form, payload) {
        if (!form || !payload) return;
        [
            ["client_action_id", "clientActionId"],
            ["end_fuel", "endFuel"],
            ["end_mileage", "endMileage"],
            ["end_engine_hours", "endEngineHours"],
            ["reading_confirmation_token", "confirmationToken"]
        ].forEach(function (mapping) {
            var input = form.querySelector('[name="' + mapping[0] + '"]');
            if (input && payload[mapping[1]] !== undefined) {
                input.value = String(payload[mapping[1]] || "");
            }
        });
    }

    function persistPending(payload) {
        var localStored = writeLocal(payload);
        var native = nativeConnection();
        if (native && typeof native.queueDriverShiftClose === "function") {
            return Promise.resolve(native.queueDriverShiftClose(payload)).then(function () {
                return true;
            }).catch(function (error) {
                removeLocal(payload.clientActionId);
                if (!localStored) throw error;
                throw new Error("Не удалось включить фоновую отправку. Оставьте приложение открытым и повторите.");
            });
        }
        if (localStored) {
            return Promise.resolve(true);
        }
        return Promise.reject(new Error("Не удалось сохранить закрытие смены на телефоне."));
    }

    function persistQueued(payload) {
        payload.state = "queued";
        payload.requiresAttention = false;
        payload.requiresAuthentication = false;
        return persistPending(payload);
    }

    function persistRetry(payload) {
        payload.state = "retry";
        payload.requiresAttention = false;
        payload.requiresAuthentication = false;
        payload.retryAttempts = Math.max(0, Number(payload.retryAttempts) || 0) + 1;
        payload.nextAttemptAt = Date.now() + retryDelayMs(payload.retryAttempts);
        return persistPending(payload);
    }

    function persistAuthRequired(payload) {
        payload.state = "auth_required";
        payload.requiresAttention = false;
        payload.requiresAuthentication = true;
        payload.blockedAuthGeneration = String(payload.authGeneration || currentAuthGeneration() || "");
        payload.nextAttemptAt = 0;
        return persistPending(payload);
    }

    function hasFreshAuthentication(payload) {
        var current = currentAuthGeneration();
        return !!current && current !== String(payload.blockedAuthGeneration || payload.authGeneration || "");
    }

    function resumeAuthentication(payload) {
        payload.authGeneration = currentAuthGeneration();
        payload.blockedAuthGeneration = "";
        payload.retryAttempts = 0;
        payload.nextAttemptAt = 0;
        return persistQueued(payload);
    }

    function clearNative(clientActionId) {
        var native = nativeConnection();
        if (!native || typeof native.acknowledgeDriverShiftClose !== "function") {
            return Promise.resolve();
        }
        return Promise.resolve(native.acknowledgeDriverShiftClose(clientActionId)).catch(function () {});
    }

    function acknowledge(clientActionId) {
        removeLocal(clientActionId);
        return clearNative(clientActionId);
    }

    function showPending(form) {
        if (!form) return;
        form.classList.add("is-sync-pending");
        var panel = form.querySelector("[data-driver-shift-sync-pending]");
        if (panel) panel.hidden = false;
        if (document.body) document.body.dataset.driverShiftClosePending = "true";
        form.querySelectorAll("input, button, select, textarea").forEach(function (control) {
            control.disabled = true;
        });
    }

    function clearPendingUi(form) {
        if (!form) return;
        form.classList.remove("is-sync-pending");
        var panel = form.querySelector("[data-driver-shift-sync-pending]");
        if (panel) panel.hidden = true;
        if (document.body) delete document.body.dataset.driverShiftClosePending;
        form.querySelectorAll("input, button, select, textarea").forEach(function (control) {
            control.disabled = false;
        });
    }

    function resetSubmitUi(form) {
        if (!form) return;
        form.dataset.driverInPlacePending = "false";
        var closeButton = form.querySelector("[data-driver-shift-close-button]");
        if (closeButton) {
            closeButton.disabled = false;
            closeButton.classList.remove("is-pending");
            var closeLabel = closeButton.querySelector("[data-mobile-shift-label]");
            if (closeLabel) closeLabel.textContent = "Закрыть смену";
        }
    }

    function hideAttention(form) {
        if (!form) return;
        var modal = form.querySelector("[data-driver-reading-confirmation]");
        if (modal) modal.hidden = true;
        if (document.body) document.body.classList.remove("modal-open");
    }

    function warningItems(data) {
        if (data && Array.isArray(data.warnings) && data.warnings.length) {
            return data.warnings.map(function (warning) {
                return {
                    code: String(warning.code || "warning"),
                    field: String(warning.field || ""),
                    title: String(warning.title || "Проверьте показание"),
                    message: String(warning.message || data.error || "Проверьте введённое значение.")
                };
            });
        }
        var fieldErrors = data && data.field_errors ? data.field_errors : {};
        var items = [];
        Object.keys(fieldErrors).forEach(function (field) {
            var messages = Array.isArray(fieldErrors[field]) ? fieldErrors[field] : [fieldErrors[field]];
            messages.forEach(function (message) {
                items.push({code: "validation_error", field: field, title: "Показание не принято", message: String(message || "")});
            });
        });
        if (!items.length) {
            items.push({code: "server_error", field: "", title: "Закрытие смены не выполнено", message: String(data && data.error || "Проверьте показания.")});
        }
        return items;
    }

    function renderWarningList(list, items) {
        if (!list) return;
        while (list.firstChild) list.removeChild(list.firstChild);
        items.forEach(function (warning) {
            var item = document.createElement("li");
            item.dataset.warningCode = warning.code;
            var title = document.createElement("strong");
            var message = document.createElement("span");
            title.textContent = warning.title;
            message.textContent = warning.message;
            item.appendChild(title);
            item.appendChild(message);
            list.appendChild(item);
        });
    }

    function focusWarningField(form, items) {
        var firstField = items.length ? items[0].field : "";
        var input = firstField ? form.querySelector('[name="' + firstField + '"]') : null;
        if (input && typeof input.focus === "function") input.focus({preventScroll: true});
    }

    function showAttention(form, payload) {
        if (!form || !payload) return;
        clearPendingUi(form);
        resetSubmitUi(form);
        fillForm(form, payload);
        var modal = form.querySelector("[data-driver-reading-confirmation]");
        if (!modal) {
            if (typeof window.showDriverToast === "function") {
                window.showDriverToast(payload.attentionMessage || payload.error || "Проверьте показания.");
            }
            return;
        }
        var confirmationRequired = payload.confirmationRequired === true;
        var items = warningItems(payload);
        var title = modal.querySelector("[data-driver-reading-confirmation-title]");
        var message = modal.querySelector("[data-driver-reading-confirmation-message]");
        var list = modal.querySelector("[data-driver-reading-confirmation-warnings]");
        var back = modal.querySelector("[data-driver-reading-confirmation-back]");
        var accept = modal.querySelector("[data-driver-reading-confirmation-accept]");
        if (title) title.textContent = confirmationRequired ? "Проверьте подозрительные показания" : "Показания не приняты";
        if (message) {
            message.textContent = confirmationRequired
                ? "Смена ещё не закрыта. Сверьте значения или подтвердите, что они верны."
                : "Сервер отклонил значения. Исправьте поля и отправьте закрытие ещё раз.";
        }
        renderWarningList(list, items);
        if (accept) {
            accept.hidden = !confirmationRequired;
            accept.onclick = function () {
                var tokenInput = form.querySelector('[name="reading_confirmation_token"]');
                if (tokenInput) tokenInput.value = String(payload.confirmationToken || "");
                hideAttention(form);
                form.dataset.driverInPlacePending = "true";
                return submit(form, {payload: payload, confirmed: true}).catch(function (error) {
                    resetSubmitUi(form);
                    if (typeof window.showDriverToast === "function") {
                        window.showDriverToast(error && error.message || "Не удалось сохранить закрытие смены.", "error");
                    }
                    return false;
                });
            };
        }
        if (back) {
            back.textContent = payload.hasActiveShift === false
                ? "Обновить экран"
                : confirmationRequired
                    ? "Вернуться и проверить"
                    : "Отменить отправку и вернуться к вводу";
            back.onclick = function () {
                hideAttention(form);
                if (payload.hasActiveShift === false) {
                    acknowledge(payload.clientActionId).then(function () {
                        if (window.location && typeof window.location.reload === "function") window.location.reload();
                    });
                    return;
                }
                acknowledge(payload.clientActionId);
                var tokenInput = form.querySelector('[name="reading_confirmation_token"]');
                if (tokenInput) tokenInput.value = "";
                resetSubmitUi(form);
                focusWarningField(form, items);
            };
        }
        modal.hidden = false;
        if (document.body) document.body.classList.add("modal-open");
        if (back && typeof back.focus === "function") back.focus();
    }

    function serverAttentionPayload(payload, data) {
        return Object.assign({}, payload, {
            state: "attention",
            requiresAttention: true,
            attentionCode: String(data.code || ""),
            confirmationRequired: data.confirmation_required === true,
            confirmationToken: String(data.confirmation_token || payload.confirmationToken || ""),
            warnings: Array.isArray(data.warnings) ? data.warnings : [],
            field_errors: data.field_errors || {},
            error: String(data.error || "Проверьте показания на конец смены."),
            attentionMessage: String(data.error || "Проверьте показания на конец смены."),
            hasActiveShift: data.has_active_shift !== false
        });
    }

    function isRecoverableClockAttention(payload) {
        if (!payload || (payload.state !== "attention" && !payload.requiresAttention)) return false;
        if (String(payload.attentionCode || "") === "device_clock_ahead") return true;
        return /(часы|время) устройства.*опережа(ют|ет) сервер/i.test(
            String(payload.attentionMessage || payload.error || "")
        );
    }

    function resumeClockAttention(form, payload) {
        payload.state = "queued";
        payload.requiresAttention = false;
        payload.attentionCode = "";
        payload.retryAttempts = 0;
        payload.nextAttemptAt = 0;
        return persistQueued(payload).then(function () {
            if (nativeConnection()) {
                showPending(form);
                return true;
            }
            if (navigator.onLine !== false) return sendStored(form, payload, {quiet: true});
            showPending(form);
            return true;
        });
    }

    function requestServer(form, payload) {
        fillForm(form, payload);
        var body = new window.FormData(form);
        var occurredAt = new Date(Number(payload.createdAt) || Date.now()).toISOString();
        body.set("occurred_at", occurredAt);
        return window.fetch(form.getAttribute("action") || window.location.href, {
            method: "POST",
            credentials: "same-origin",
            cache: "no-store",
            redirect: "follow",
            headers: {
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest"
            },
            body: body
        }).then(function (response) {
            return response.text().then(function (text) {
                var data = {};
                try { data = JSON.parse(text || "{}"); } catch (error) {
                    data = {ok: false, error: "Сервер вернул непонятный ответ."};
                }
                return {response: response, data: data, rawText: text};
            });
        });
    }

    function isRetryableStatus(status) {
        return status === 408 || status === 429 || status >= 500;
    }

    function isAuthenticationResult(result) {
        var response = result.response;
        if (response.status === 401 || response.status === 403) return true;
        if (!response.redirected || !response.url) return false;
        try {
            var path = new URL(response.url, window.location.origin).pathname;
            return path === "/" || path === "/login" || path === "/login/";
        } catch (error) {
            return false;
        }
    }

    function scheduleRetry(form, payload) {
        if (nativeConnection() || navigator.onLine === false) return;
        if (retryTimer !== null) window.clearTimeout(retryTimer);
        var delay = Math.max(0, Number(payload.nextAttemptAt || 0) - Date.now());
        retryTimer = window.setTimeout(function () {
            retryTimer = null;
            sendStored(form, payload, {quiet: true});
        }, delay);
    }

    function completeApplied(payload, data) {
        return acknowledge(payload.clientActionId).then(function () {
            if (typeof playDriverVoice === "function") playDriverVoice("shift_end", "voice_shift_closed");
            var target = String(data.redirect_url || "/driver/?tab=manifest");
            if (window.location) {
                if (typeof window.location.assign === "function") window.location.assign(target);
                else window.location.href = target;
            }
            return true;
        });
    }

    function handleServerResult(form, payload, result) {
        var response = result.response;
        var data = result.data || {};
        if (response.ok && data.ok) return completeApplied(payload, data);
        if (isAuthenticationResult(result)) {
            return persistAuthRequired(payload).then(function () {
                showPending(form);
                return true;
            });
        }
        if (isRetryableStatus(response.status)) {
            return persistRetry(payload).then(function () {
                showPending(form);
                scheduleRetry(form, payload);
                return true;
            });
        }
        var attention = serverAttentionPayload(payload, data);
        return clearNative(payload.clientActionId).then(function () {
            writeLocal(attention);
            showAttention(form, attention);
            return true;
        });
    }

    function sendStored(form, payload, options) {
        options = options || {};
        if (!form || retryInFlight) return Promise.resolve(false);
        if (Number(payload.nextAttemptAt || 0) > Date.now()) {
            showPending(form);
            scheduleRetry(form, payload);
            return Promise.resolve(true);
        }
        retryInFlight = true;
        clearPendingUi(form);
        fillForm(form, payload);
        return requestServer(form, payload).then(function (result) {
                return handleServerResult(form, payload, result);
            }, function () {
                return persistRetry(payload).then(function () {
                    showPending(form);
                    scheduleRetry(form, payload);
                    return true;
                });
            }).finally(function () {
                retryInFlight = false;
            });
    }

    function submit(form, options) {
        options = options || {};
        var payload = options.payload ? Object.assign({}, options.payload) : payloadFromForm(form);
        if (options.confirmed) {
            var tokenInput = form.querySelector('[name="reading_confirmation_token"]');
            payload.confirmationToken = tokenInput ? String(tokenInput.value || "").trim() : String(payload.confirmationToken || "");
        }
        payload.state = "queued";
        payload.requiresAttention = false;
        return sendStored(form, payload);
    }

    function bindInvalidation(form) {
        if (!form || form.dataset.driverShiftConfirmationBound === "true") return;
        form.dataset.driverShiftConfirmationBound = "true";
        form.querySelectorAll("[name='end_fuel'], [name='end_mileage'], [name='end_engine_hours']").forEach(function (input) {
            input.addEventListener("input", function () {
                var tokenInput = form.querySelector('[name="reading_confirmation_token"]');
                if (tokenInput) tokenInput.value = "";
                var pending = readLocal();
                if (pending && pending.state === "attention") acknowledge(pending.clientActionId);
                hideAttention(form);
            });
        });
    }

    function restore(form) {
        var localPending = readLocal();
        var native = nativeConnection();
        bindInvalidation(form);
        if (!form) {
            if (localPending) acknowledge(localPending.clientActionId);
            return Promise.resolve(false);
        }
        if (!native || typeof native.getState !== "function") {
            if (!localPending || String(form.dataset.nativeShiftId || "") !== String(localPending.shiftId || "")) {
                return Promise.resolve(false);
            }
            if (localPending.state === "attention" || localPending.requiresAttention) {
                if (isRecoverableClockAttention(localPending)) {
                    return resumeClockAttention(form, localPending);
                }
                showAttention(form, localPending);
                return Promise.resolve(true);
            }
            if (localPending.state === "auth_required" || localPending.requiresAuthentication) {
                if (!hasFreshAuthentication(localPending)) {
                    showPending(form);
                    return Promise.resolve(true);
                }
                return resumeAuthentication(localPending).then(function () {
                    return sendStored(form, localPending, {quiet: true});
                });
            }
            if (navigator.onLine !== false) return sendStored(form, localPending, {quiet: true});
            showPending(form);
            return Promise.resolve(true);
        }
        return Promise.resolve(native.getState()).then(function (state) {
            var nativePending = state && state.pendingDriverShiftClose;
            if (!nativePending) {
                if (localPending && (localPending.state === "attention" || localPending.requiresAttention)) {
                    if (isRecoverableClockAttention(localPending)) {
                        return resumeClockAttention(form, localPending);
                    }
                    showAttention(form, localPending);
                    return true;
                }
                if (localPending) removeLocal(localPending.clientActionId);
                return false;
            }
            var pending = nativePending || localPending;
            if (!pending || String(form.dataset.nativeShiftId || "") !== String(pending.shiftId || "")) {
                return false;
            }
            if (pending.requiresAttention) {
                pending.state = "attention";
                pending.confirmationRequired = pending.confirmationRequired === true;
                writeLocal(pending);
            }
            if (pending.state === "auth_required" || pending.requiresAuthentication) {
                if (!hasFreshAuthentication(pending)) {
                    writeLocal(pending);
                    showPending(form);
                    return true;
                }
                return resumeAuthentication(pending).then(function () {
                    showPending(form);
                    return true;
                });
            }
            fillForm(form, pending);
            if (pending.state === "attention" || pending.requiresAttention) {
                if (isRecoverableClockAttention(pending)) {
                    return resumeClockAttention(form, pending);
                }
                showAttention(form, pending);
                return true;
            }
            showPending(form);
            return true;
        }).catch(function () {
            if (localPending && (localPending.state === "attention" || localPending.requiresAttention)) {
                showAttention(form, localPending);
            } else if (localPending) {
                showPending(form);
            }
            return !!localPending;
        });
    }

    return {
        submit: submit,
        restore: restore,
        readLocal: readLocal,
        showPending: showPending,
        acknowledge: acknowledge,
        showAttention: showAttention,
        bindInvalidation: bindInvalidation
    };
})();
window.addEventListener("online", function () {
    if (window.DriverShiftCloseOutbox) {
        window.DriverShiftCloseOutbox.restore(
            document.querySelector("[data-driver-shift-close-form]")
        );
    }
});
/* DRIVER_SHIFT_CLOSE_OUTBOX_END */

function bindDriverShiftHoldAction(form, button, options) {
    options = options || {};
    if (!form || !button || !window.MobileShiftHold) {
        return null;
    }
    return window.MobileShiftHold.bind(button, {
        holdMs: options.holdMs || 2000,
        readyLabel: options.readyLabel || button.textContent.trim(),
        onShortPress: function () {
            if (typeof window.showDriverToast === "function") window.showDriverToast("Удерживайте кнопку");
        },
        onComplete: function () {
            form.dataset.driverShiftHoldComplete = "true";
            form.requestSubmit(button);
        }
    });
}

function bindDriverShiftOpeningForm(shell) {
    var openShiftForm = shell.querySelector(".driver-shift-opening-form");
    var openShiftButton = shell.querySelector("[data-driver-shift-open-button]");
    if (!openShiftForm || !openShiftButton || openShiftForm.dataset.driverShiftOpeningBound === "true") {
        return;
    }
    openShiftForm.dataset.driverShiftOpeningBound = "true";
    openShiftForm.dataset.driverShiftOpeningPending = "false";
    function driverOpeningRoleIsReadonly() {
        return (
            typeof window.isAppRoleReadonly === "function"
            && window.isAppRoleReadonly()
        );
    }
    var openReadings = Array.from(openShiftForm.querySelectorAll("input[type='number']"));
    var initialReadings = openReadings.map(function (input) {
        return input.value;
    });

    function requestDeferredRefresh(reason) {
        if (window.AppRealtime && typeof window.AppRealtime.wake === "function") {
            window.AppRealtime.wake(reason);
        }
    }

    function syncOpenShiftFormState(options) {
        options = options || {};
        var wasDirty = openShiftForm.dataset.driverShiftOpeningDirty === "true";
        var isDirty = openReadings.some(function (input, index) {
            return input.value !== initialReadings[index];
        });
        openShiftForm.dataset.driverShiftOpeningDirty = isDirty ? "true" : "false";
        openShiftButton.disabled = !openReadings.length || openReadings.some(function (input) {
            return input.value.trim() === "" || !input.checkValidity();
        });
        if (
            !isDirty &&
            openShiftForm.dataset.driverShiftOpeningPending !== "true" &&
            (wasDirty || options.explicitReset)
        ) {
            requestDeferredRefresh(
                options.explicitReset ? "driver_shift_open_reset" : "driver_shift_open_restored"
            );
        }
    }

    openReadings.forEach(function (input) {
        input.addEventListener("input", syncOpenShiftFormState);
    });
    openShiftForm.addEventListener("reset", function () {
        openShiftForm.dataset.driverShiftOpeningPending = "false";
        openShiftButton.classList.remove("is-pending");
        window.setTimeout(function () {
            syncOpenShiftFormState({explicitReset: true});
        }, 0);
    });
    openShiftForm.addEventListener("submit", function (event) {
        if (
            openShiftButton.disabled
            || driverOpeningRoleIsReadonly()
            || openShiftForm.dataset.driverShiftHoldComplete !== "true"
        ) {
            event.preventDefault();
            if (
                !openShiftButton.disabled
                && openShiftForm.dataset.driverShiftHoldComplete !== "true"
                && typeof window.showDriverToast === "function"
            ) {
                window.showDriverToast("Удерживайте кнопку, чтобы начать смену");
            }
            return;
        }
        /* Одна открытая смена на сотрудника: если в другой роли смена ещё
           открыта, после удержания спрашиваем «Завершить её и начать?» — как
           у экскаваторщика, диспетчера и мастера. Ответ «да» добавляет в
           форму подтверждение, и сервер закрывает ту смену служебно. */
        var otherRoleQuestion = openShiftButton.dataset.otherRoleShiftQuestion || "";
        if (
            otherRoleQuestion
            && openShiftForm.dataset.otherRoleShiftConfirmed !== "true"
            && typeof window.openAppConfirmDialog === "function"
        ) {
            event.preventDefault();
            delete openShiftForm.dataset.driverShiftHoldComplete;
            window.openAppConfirmDialog(otherRoleQuestion, function () {
                var confirmField = openShiftForm.querySelector("[data-other-role-shift-input]");
                if (!confirmField) {
                    confirmField = document.createElement("input");
                    confirmField.type = "hidden";
                    confirmField.name = openShiftButton.dataset.otherRoleShiftField || "close_other_role_shift";
                    confirmField.setAttribute("data-other-role-shift-input", "");
                    openShiftForm.appendChild(confirmField);
                }
                confirmField.value = "1";
                openShiftForm.dataset.otherRoleShiftConfirmed = "true";
                openShiftForm.dataset.driverShiftHoldComplete = "true";
                openShiftForm.requestSubmit(openShiftButton);
            }, 0, openShiftButton.dataset.otherRoleShiftAccept || "Завершить и начать");
            return;
        }
        delete openShiftForm.dataset.otherRoleShiftConfirmed;
        delete openShiftForm.dataset.driverShiftHoldComplete;
        openShiftForm.dataset.driverShiftOpeningPending = "true";
        openShiftButton.disabled = true;
        openShiftButton.classList.add("is-pending");
        var openShiftLabel = openShiftButton.querySelector("[data-mobile-shift-label]");
        if (openShiftLabel) openShiftLabel.textContent = "Открываем смену";
    });
    bindDriverShiftHoldAction(openShiftForm, openShiftButton, {
        holdMs: 1000,
        readyLabel: "Начать смену",
        progressProperty: "--driver-shift-open-hold"
    });
    syncOpenShiftFormState();
}
