(function () {
var readExcavatorAssignmentSnapshot = window.ExcavatorWorkFeedback.readExcavatorAssignmentSnapshot;
var storeExcavatorAssignments = window.ExcavatorWorkFeedback.storeExcavatorAssignments;
var syncExcavatorAssignmentSnapshot = window.ExcavatorWorkFeedback.syncExcavatorAssignmentSnapshot;
var viewportSyncFrame = null;
var excavatorStableViewportHeight = 0;
var excavatorStableViewportWidth = 0;
document.body.dataset.realtimeVisibilityOnly = "true";

function readExcavatorViewportMetrics() {
    var viewport = window.visualViewport;
    var height = viewport && Number(viewport.height) > 0 ? Number(viewport.height) : Number(window.innerHeight);
    var width = viewport && Number(viewport.width) > 0 ? Number(viewport.width) : Number(window.innerWidth);
    return {
        height: Math.round(height || document.documentElement.clientHeight || 0),
        width: Math.round(width || document.documentElement.clientWidth || 0)
    };
}

function excavatorKeyboardFieldIsFocused() {
    var active = document.activeElement;
    return !!(
        active
        && active.matches
        && active.matches("[data-eo-shift-fuel], [data-eo-shift-engine-hours], [data-eo-face-horizon], [data-eo-face-block]")
    );
}

function syncExcavatorKeyboardState(metrics) {
    var shell = document.querySelector("[data-eo-shell]");
    if (!shell || !metrics.height || !metrics.width) return;

    var wasOpen = shell.dataset.eoImeOpen === "true";
    var fieldFocused = excavatorKeyboardFieldIsFocused();
    var widthChanged = excavatorStableViewportWidth > 0
        && Math.abs(metrics.width - excavatorStableViewportWidth) > 80;

    if (widthChanged || !(excavatorStableViewportHeight > 0)) {
        excavatorStableViewportHeight = metrics.height;
        excavatorStableViewportWidth = metrics.width;
        wasOpen = false;
    }

    var sameWidth = Math.abs(metrics.width - excavatorStableViewportWidth) <= 24;
    var keyboardGap = Math.max(120, Math.round(excavatorStableViewportHeight * .22));
    var viewportReduced = sameWidth
        && metrics.height < excavatorStableViewportHeight - keyboardGap;

    /* Изменение высоты без фокуса — split-screen или системные панели, а не
       клавиатура. Принимаем его как новую стабильную высоту, чтобы следующий
       фокус поля не дал ложное состояние IME. */
    if (viewportReduced && !fieldFocused && !wasOpen) {
        excavatorStableViewportHeight = metrics.height;
        excavatorStableViewportWidth = metrics.width;
        viewportReduced = false;
    }

    /* После открытия держим состояние до восстановления visualViewport. Так
       blur между полями не возвращает строку действий поверх клавиатуры. */
    var isOpen = viewportReduced && (fieldFocused || wasOpen);
    if (isOpen) {
        shell.dataset.eoImeOpen = "true";
        shell.style.setProperty("--eo-visible-height", metrics.height + "px");
    } else {
        delete shell.dataset.eoImeOpen;
        shell.style.removeProperty("--eo-visible-height");
        if (!fieldFocused) {
            excavatorStableViewportHeight = metrics.height;
            excavatorStableViewportWidth = metrics.width;
        } else if (metrics.height > excavatorStableViewportHeight) {
            excavatorStableViewportHeight = metrics.height;
            excavatorStableViewportWidth = metrics.width;
        }
    }
}

function syncExcavatorViewportHeight() {
    var metrics = readExcavatorViewportMetrics();
    syncExcavatorKeyboardState(metrics);
    if (!metrics.height || metrics.height < 320) return;
    var shell = document.querySelector("[data-eo-shell]");
    var height = (
        shell
        && shell.dataset.eoImeOpen === "true"
        && excavatorStableViewportHeight > 0
    ) ? excavatorStableViewportHeight : metrics.height;
    var value = height + "px";
    document.documentElement.style.setProperty("--app-height", value);
    document.documentElement.style.setProperty("--eo-app-height", value);
}

function scheduleExcavatorViewportHeightSync() {
    syncExcavatorViewportHeight();
    if (viewportSyncFrame) {
        window.cancelAnimationFrame(viewportSyncFrame);
    }
    viewportSyncFrame = window.requestAnimationFrame(function () {
        viewportSyncFrame = null;
        syncExcavatorViewportHeight();
    });
    window.setTimeout(syncExcavatorViewportHeight, 180);
}

syncExcavatorViewportHeight();
window.syncExcavatorViewportHeight = syncExcavatorViewportHeight;
window.addEventListener("resize", scheduleExcavatorViewportHeightSync, { passive: true });
window.addEventListener("orientationchange", scheduleExcavatorViewportHeightSync, { passive: true });
window.addEventListener("pageshow", scheduleExcavatorViewportHeightSync);
window.addEventListener("load", scheduleExcavatorViewportHeightSync);
if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", scheduleExcavatorViewportHeightSync, { passive: true });
    window.visualViewport.addEventListener("scroll", scheduleExcavatorViewportHeightSync, { passive: true });
}
document.addEventListener("focusin", function (event) {
    if (event.target && event.target.matches && event.target.matches("[data-eo-shift-fuel], [data-eo-shift-engine-hours], [data-eo-face-horizon], [data-eo-face-block]")) {
        scheduleExcavatorViewportHeightSync();
    }
});
document.addEventListener("focusout", function (event) {
    if (event.target && event.target.matches && event.target.matches("[data-eo-shift-fuel], [data-eo-shift-engine-hours], [data-eo-face-horizon], [data-eo-face-block]")) {
        scheduleExcavatorViewportHeightSync();
    }
});
document.addEventListener("visibilitychange", function () {
    if (!document.hidden) {
        scheduleExcavatorViewportHeightSync();
    }
});

function isExcavatorRefreshUnsafe(options) {
    options = options || {};
    if (document.hidden) return true;
    var pendingOwner = options.pendingOwner === "face" || options.pendingOwner === "shift"
        ? options.pendingOwner
        : "";
    var shell = document.querySelector("[data-eo-shell]");
    if (!shell) return false;
    if (shell.dataset.eoDowntimePreview !== undefined) return true;
    if (shell.dataset.eoHasPendingFieldEvents === "true") return true;
    var active = document.activeElement;
    var activeTag = active && active.tagName ? active.tagName.toLowerCase() : "";
    if (active && shell.contains(active) && (active.isContentEditable || activeTag === "input" || activeTag === "textarea" || activeTag === "select")) {
        return true;
    }
    var shiftScreen = shell.querySelector('[data-eo-screen="shift"]');
    if (shiftScreen && (
        shiftScreen.dataset.eoShiftDirty === "true" ||
        shiftScreen.querySelector("[data-eo-shift-form-error]:not([hidden])") ||
        shiftScreen.querySelector("[data-eo-shift-field].has-error")
    )) {
        return true;
    }
    var faceScreen = shell.querySelector('[data-eo-screen="face"]');
    if (faceScreen && (
        faceScreen.dataset.eoFaceDirty === "true"
        || (pendingOwner !== "face" && faceScreen.dataset.eoFacePending === "true")
    )) {
        return true;
    }
    if (document.querySelector(".eo-truck-card.is-pending, .eo-truck-card.is-picked-up, .eo-truck-card.is-pickup-waiting, .eo-truck-card.is-dragging, .eo-dashboard-unload-card.is-return-swiping, .eo-dashboard-unload-card.is-return-rebounding, .eo-dashboard-unload-card.is-return-pending, .eo-reason-action.is-pending, [data-eo-close-event].is-pending, .mobile-shift__action.is-holding, .eo-truck-detail-modal:not([hidden]), .eo-mobile-update-modal:not([hidden]), .eo-reading-confirmation:not([hidden]), [data-eo-dump-queue-modal]:not([hidden])")) {
        return true;
    }
    return Array.prototype.some.call(
        document.querySelectorAll(".mobile-shift__action.is-pending"),
        function (pendingAction) {
            return !(
                (pendingOwner === "face" && pendingAction.matches("[data-eo-apply-settings]"))
                || (pendingOwner === "shift" && pendingAction.matches("[data-eo-shift-button]"))
            );
        }
    );
}

function storeExcavatorRealtimeVersion(version) {
    if (!version) return;
    var appliedVersion = Math.max(Number(document.body.dataset.operationalStateVersion || 0), Number(version));
    document.body.dataset.operationalStateVersion = String(appliedVersion);
    try {
        window.sessionStorage.setItem("operational-state-version", String(appliedVersion));
    } catch (error) {}
    if (window.AppRealtime && typeof window.AppRealtime.markApplied === "function") {
        window.AppRealtime.markApplied(appliedVersion);
    }
    if (window.AppRealtime && typeof window.AppRealtime.reportTransportSuccess === "function") {
        window.AppRealtime.reportTransportSuccess({
            channel: "fragment", occurredAtMs: Date.now(), serverVersion: appliedVersion
        });
    }
}

function hasExcavatorRelevantEvents(events) {
    return Array.isArray(events) && events.length > 0;
}

var excavatorWorkMutationGeneration = 0;
var excavatorWorkRefreshRequestGeneration = 0;
var excavatorWorkAppliedRequestGeneration = 0;
function invalidateExcavatorWorkRefresh() {
    excavatorWorkMutationGeneration += 1;
    if (window.eoExcavatorWorkRefreshPromise) {
        window.eoExcavatorWorkRefreshMinimumVersion = -1;
    }
}

function refreshExcavatorWorkFromServer(options) {
    options = options || {};
    if (isExcavatorRefreshUnsafe(options)) {
        return Promise.resolve(false);
    }
    var requestedMinimumVersion = Math.max(
        Number(options.version || 0),
        Number(document.body.dataset.operationalStateVersion || 0)
    );
    if (window.eoExcavatorWorkRefreshPromise) {
        if (Number(window.eoExcavatorWorkRefreshMinimumVersion || 0) >= requestedMinimumVersion) {
            return window.eoExcavatorWorkRefreshPromise;
        }
        window.eoExcavatorWorkQueuedMinimumVersion = Math.max(
            Number(window.eoExcavatorWorkQueuedMinimumVersion || 0),
            requestedMinimumVersion
        );
        window.eoExcavatorWorkQueuedOptions = Object.assign({}, options, {
            version: window.eoExcavatorWorkQueuedMinimumVersion
        });
        if (!window.eoExcavatorWorkQueuedRefreshPromise) {
            var inFlightPromise = window.eoExcavatorWorkRefreshPromise;
            window.eoExcavatorWorkQueuedRefreshPromise = inFlightPromise.then(
                function () { return true; },
                function () { return false; }
            ).then(function () {
                var queuedOptions = window.eoExcavatorWorkQueuedOptions || {};
                window.eoExcavatorWorkQueuedOptions = null;
                window.eoExcavatorWorkQueuedMinimumVersion = 0;
                window.eoExcavatorWorkQueuedRefreshPromise = null;
                return refreshExcavatorWorkFromServer(queuedOptions);
            });
        }
        return window.eoExcavatorWorkQueuedRefreshPromise;
    }
    var oldShell = document.querySelector("[data-eo-shell]");
    if (!oldShell || !window.AppOperationalFragment) {
        return Promise.resolve(false);
    }
    var previousAssignments = readExcavatorAssignmentSnapshot(oldShell);
    var activeTab = oldShell.dataset.eoActiveTab || "trucks";
    var mutationGeneration = excavatorWorkMutationGeneration;
    var requestGeneration = ++excavatorWorkRefreshRequestGeneration;
    var minimumVersion = requestedMinimumVersion;
    var refreshPromise = window.AppOperationalFragment.request(
        "excavator",
        minimumVersion,
        {
            snapshot: options.snapshot === true,
            snapshotUrl: options.snapshotUrl ? String(options.snapshotUrl) : ""
        }
    ).then(function (payload) {
        if (mutationGeneration !== excavatorWorkMutationGeneration || isExcavatorRefreshUnsafe(options)) return false;
        var payloadVersion = Number(payload.version || 0);
        var appliedVersion = Number(document.body.dataset.operationalStateVersion || 0);
        if (payloadVersion < appliedVersion) return false;
        if (requestGeneration < excavatorWorkAppliedRequestGeneration) return false;
        var newShell = window.AppOperationalFragment.parseRoot(
            payload.html,
            "[data-eo-shell]"
        );
        var currentShell = document.querySelector("[data-eo-shell]");
        if (!newShell || !currentShell) return false;
        var currentTruckDetailCards = document.getElementById("eo-truck-detail-cards-data");
        if (payload.equipment_cards && currentTruckDetailCards) {
            currentTruckDetailCards.textContent = JSON.stringify(payload.equipment_cards);
        }
        [
            ['eo-free-bucket-directory-data', payload.free_bucket_truck_directory],
            ['eo-free-bucket-cards-data', payload.free_bucket_cards]
        ].forEach(function (entry) {
            if (entry[1] === undefined) return;
            var node = newShell.querySelector('#' + entry[0]);
            if (!node) {
                node = document.createElement('script');
                node.type = 'application/json';
                node.id = entry[0];
                newShell.appendChild(node);
            }
            node.textContent = JSON.stringify(entry[1]);
        });
        var latestActiveTab = currentShell.dataset.eoActiveTab || activeTab;
        newShell.dataset.eoActiveTab = options.preserveTab === false
            ? (newShell.dataset.eoActiveTab || "trucks")
            : latestActiveTab;
        var viewState = window.AppOperationalFragment.captureView
            ? window.AppOperationalFragment.captureView(currentShell, "[data-eo-screen]", "data-eo-screen")
            : null;
        var faceScroll = viewState && latestActiveTab === "face" && newShell.dataset.eoActiveTab === "face"
            ? window.AppOperationalFragment.captureView(currentShell, "[data-eo-face-scroll]", "data-eo-face-scroll")
            : null;
        currentShell.replaceWith(newShell);
        if (typeof window.bindMobileShiftScreens === "function") {
            window.bindMobileShiftScreens();
        }
        if (typeof window.initExcavatorWorkShell === "function") {
            window.initExcavatorWorkShell();
        }
        /* Operational fragments can be fetched by the service worker without
           the native User-Agent suffix. In that case Django correctly keeps
           the APK version node hidden in the replacement HTML. Ask the
           Android bridge to restore both the installed version and the
           update-button state on the new shell immediately after replacement. */
        if (
            window.CopperResourcesUpdate
            && typeof window.CopperResourcesUpdate.refreshIndicator === "function"
        ) {
            window.CopperResourcesUpdate.refreshIndicator();
        }
        if (typeof window.checkAchievementPrize === "function") {
            window.checkAchievementPrize(options.achievementContext);
        }
        if (options.suppressAssignmentAlert === true) {
            /* Открытие и закрытие смены меняют видимый список карточек, но это
               не является реальным назначением или снятием самосвала. */
            storeExcavatorAssignments(newShell, readExcavatorAssignmentSnapshot(newShell));
        } else {
            var snapshotIsCurrent = !options.snapshot
                || !options.achievementContext
                || typeof options.achievementContext.isCurrentSnapshot !== "function"
                || options.achievementContext.isCurrentSnapshot(Number(payload.snapshot_version || payload.version || 0));
            syncExcavatorAssignmentSnapshot(
                newShell,
                previousAssignments,
                Number(options.version || 0),
                {announce: snapshotIsCurrent}
            );
        }
        scheduleExcavatorViewportHeightSync();
        if (window.AppOperationalFragment.restoreView) {
            window.AppOperationalFragment.restoreView(newShell, viewState);
            if (faceScroll) window.AppOperationalFragment.restoreView(newShell, faceScroll);
        }
        // Версия подтверждена только после установки и инициализации server truth.
        excavatorWorkAppliedRequestGeneration = requestGeneration;
        storeExcavatorRealtimeVersion(payloadVersion);
        return true;
    }).catch(function () {
        return false;
    });
    window.eoExcavatorWorkRefreshMinimumVersion = minimumVersion;
    window.eoExcavatorWorkRefreshPromise = refreshPromise.finally(function () {
        if (window.eoExcavatorWorkRefreshPromise === refreshPromiseWithCleanup) {
            window.eoExcavatorWorkRefreshPromise = null;
            window.eoExcavatorWorkRefreshMinimumVersion = 0;
        }
    });
    var refreshPromiseWithCleanup = window.eoExcavatorWorkRefreshPromise;
    return refreshPromiseWithCleanup;
}

window.refreshExcavatorWorkFromServer = refreshExcavatorWorkFromServer;

function bindExcavatorTransferCountdowns(shell) {
    if (window.eoTransferCountdownTimer) {
        window.clearInterval(window.eoTransferCountdownTimer);
        window.eoTransferCountdownTimer = null;
    }
    if (!shell) return;
    var cards = Array.prototype.slice.call(shell.querySelectorAll("[data-eo-transfer-deadline]"));
    var previewBadges = Array.prototype.slice.call(
        shell.querySelectorAll("[data-eo-queue-truck][data-eo-transfer-preview-deadline]")
    );
    var freeBucketDeadline = Date.parse(shell.dataset.eoFreeBucketRefreshAt || "");
    if (!cards.length && !previewBadges.length && !Number.isFinite(freeBucketDeadline)) return;
    var serverEpoch = Date.parse(shell.dataset.eoServerNow || "");
    var clientEpoch = Date.now();
    if (!Number.isFinite(serverEpoch)) serverEpoch = clientEpoch;

    function tick() {
        var authoritativeNow = serverEpoch + (Date.now() - clientEpoch);
        var freeBucketExpired = Number.isFinite(freeBucketDeadline) && freeBucketDeadline <= authoritativeNow;
        var needsServerConfirmation = freeBucketExpired;
        cards.forEach(function (card) {
            if (!card.isConnected) return;
            var createdAt = Date.parse(card.dataset.eoTransferCreated || "");
            var deadline = Date.parse(card.dataset.eoTransferDeadline || "");
            if (!Number.isFinite(deadline)) return;
            var remainingMs = Math.max(0, deadline - authoritativeNow);
            var remainingSeconds = Math.ceil(remainingMs / 1000);
            var minutes = Math.floor(remainingSeconds / 60);
            var seconds = remainingSeconds % 60;
            var counter = card.querySelector("[data-eo-transfer-countdown]");
            if (counter) counter.textContent = minutes + ":" + String(seconds).padStart(2, "0");
            card.classList.remove("is-transfer-awaiting-confirmation");
            if (Number.isFinite(createdAt) && deadline > createdAt) {
                var progress = Math.max(0, Math.min(1, (authoritativeNow - createdAt) / (deadline - createdAt)));
                card.style.setProperty("--eo-transfer-progress", progress.toFixed(4));
                if (card.dataset.eoTransferDirection === "outgoing") {
                    card.style.setProperty("--eo-transfer-veil-opacity", (.08 + (.52 * progress)).toFixed(4));
                    card.style.setProperty("--eo-transfer-media-opacity", (1 - (.55 * progress)).toFixed(4));
                } else {
                    card.style.setProperty("--eo-transfer-veil-opacity", (.48 * (1 - progress)).toFixed(4));
                    card.style.setProperty("--eo-transfer-media-opacity", (.45 + (.55 * progress)).toFixed(4));
                }
            }
            if (remainingMs > 0) return;
            // Ноль на клиентском таймере не меняет права и не удаляет карточку.
            // Просим сервер применить срок и ждём подтверждённый фрагмент.
            if (counter) counter.textContent = "Ожидает подтверждения";
            card.classList.add("is-transfer-awaiting-confirmation");
            needsServerConfirmation = true;
        });
        previewBadges.forEach(function (badge) {
            if (!badge.isConnected) return;
            var deadline = Date.parse(badge.dataset.eoTransferPreviewDeadline || "");
            if (!Number.isFinite(deadline) || deadline > authoritativeNow) return;
            badge.classList.add("is-transfer-awaiting-confirmation");
            badge.setAttribute("aria-label", "Самосвал " + (badge.dataset.eoTruckNumber || badge.textContent || "") + ". Ожидает подтверждения перевода");
            needsServerConfirmation = true;
        });
        if (!needsServerConfirmation || document.hidden || navigator.onLine === false) return;
        var retryAt = Number(window.eoTransferDeadlineRefreshRetryAt || 0);
        var lastWake = Number(window.eoTransferDeadlineLastWake || 0);
        if (
            Date.now() < retryAt
            || Date.now() - lastWake < 5000
            || window.eoTransferDeadlineRefreshInFlight
        ) return;
        window.eoTransferDeadlineLastWake = Date.now();
        if (window.AppRealtime && typeof window.AppRealtime.wake === "function") {
            try {
                window.AppRealtime.wake(freeBucketExpired ? "free_bucket_deadline" : "haul_transfer_deadline");
            } catch (error) {
                /* Прямой серверный refresh всё равно должен продолжиться. */
            }
        }
        window.eoTransferDeadlineRefreshInFlight = true;
        refreshExcavatorWorkFromServer({preserveTab: true}).then(function (applied) {
            if (applied) {
                window.eoTransferDeadlineRefreshFailures = 0;
                window.eoTransferDeadlineRefreshRetryAt = Date.now() + 5000;
                return;
            }
            var failures = Math.min(
                4,
                Number(window.eoTransferDeadlineRefreshFailures || 0) + 1
            );
            window.eoTransferDeadlineRefreshFailures = failures;
            window.eoTransferDeadlineRefreshRetryAt = Date.now() + Math.min(
                30000,
                5000 * Math.pow(2, failures - 1)
            );
        }).finally(function () {
            window.eoTransferDeadlineRefreshInFlight = false;
        });
    }
    tick();
    window.eoTransferCountdownTimer = window.setInterval(tick, 1000);
}

// Пассивной сверкой обеих ролей раз в 20 секунд владеет AppRealtime.
// Собственный таймер здесь создавал второй независимый поток fragments.
window.excavatorHasPendingWork = isExcavatorRefreshUnsafe;
if (
    window.AppPwaContractGuard
    && typeof window.AppPwaContractGuard.registerUnsafeCheck === "function"
) {
    window.AppPwaContractGuard.registerUnsafeCheck(isExcavatorRefreshUnsafe);
}
window.applyOperationalStateRefresh = function (context) {
    if (isExcavatorRefreshUnsafe()) {
        return Promise.resolve({deferred: true, reason: "excavator_busy"});
    }
    var targetVersion = Number(context && context.version || 0);
    // Даже пустая/усечённая дельта не доказывает, что текущий DOM отражает сервер.
    return refreshExcavatorWorkFromServer({
        preserveTab: true,
        version: targetVersion,
        achievementContext: context,
        snapshot: !!(context && context.snapshotRequired),
        snapshotUrl: context && context.snapshotUrl ? String(context.snapshotUrl) : ""
    }).then(function (applied) {
        if (!applied) return {deferred: true, reason: "excavator_refresh_failed"};
        return {applied: true, version: Number(document.body.dataset.operationalStateVersion || 0)};
    }).catch(function () {
        return {deferred: true, reason: "excavator_refresh_error"};
    });
};

function excavatorInstallDeviceId() {
    var key = "copper-field-device-id-v1";
    try {
        var existing = window.localStorage.getItem(key);
        if (existing) return existing;
        var generated = window.crypto && typeof window.crypto.randomUUID === "function"
            ? window.crypto.randomUUID()
            : "device-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
        window.localStorage.setItem(key, generated);
        return generated;
    } catch (error) {
        return "";
    }
}

function newExcavatorFieldId(prefix) {
    var value = window.crypto && typeof window.crypto.randomUUID === "function"
        ? window.crypto.randomUUID()
        : Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
    return String(prefix || "event") + "-" + value;
}

function legacyExcavatorFieldSequence(accessId, deviceId) {
    var key = "excavator-field-sequence-v1:" + accessId + ":" + deviceId;
    try {
        return Math.max(0, Number(window.localStorage.getItem(key) || 0));
    } catch (error) {
        return 0;
    }
}

function renderExcavatorOfflineStatus(summary) {
    summary = summary || {};
    var previousPending = Number(window.operationalOutboxPendingCount || 0);
    window.operationalOutboxPendingCount = Math.max(0, Number(summary.pending || 0));
    window.dispatchEvent(new CustomEvent("operational-outbox-state", {detail: {
        role: "excavator", pendingCount: window.operationalOutboxPendingCount,
        reviewCount: Math.max(0, Number(summary.attention || 0))
    }}));
    if (previousPending > 0 && window.operationalOutboxPendingCount === 0 && window.AppRealtime && typeof window.AppRealtime.requestReconcile === "function") {
        window.AppRealtime.requestReconcile("excavator_offline_queue_drained");
    }
    var indicator = document.querySelector("[data-eo-offline-sync]");
    if (!indicator) return;
    var shell = document.querySelector("[data-eo-shell]");
    summary = summary || {};
    var label = indicator.querySelector("[data-eo-offline-sync-label]");
    var count = indicator.querySelector("[data-eo-offline-sync-count]");
    var offline = navigator.onLine === false;
    var text = "Все действия отправлены";
    var stateClass = "is-synced";
    if (summary.storage_failed) {
        text = "Действие не сохранено";
        stateClass = "is-storage-error";
    } else if (summary.attention > 0) {
        text = "Нужна сверка";
        stateClass = "is-attention";
    } else if (summary.syncing > 0) {
        text = "Идёт отправка";
        stateClass = "is-syncing";
    } else if (summary.total > 0 && offline) {
        text = "Нет сети · сохранено";
        stateClass = "is-offline";
    } else if (summary.total > 0) {
        text = "Ожидает отправки";
        stateClass = "is-offline";
    } else if (offline) {
        text = "Нет сети · очередь пуста";
        stateClass = "is-offline";
    }
    indicator.className = "eo-offline-sync " + stateClass;
    if (shell) shell.dataset.eoHasPendingFieldEvents = summary.pending > 0 ? "true" : "false";
    indicator.hidden = false;
    if (label) label.textContent = text;
    if (count) {
        count.textContent = String(Number(summary.total || 0));
        count.hidden = !(summary.total > 0);
    }
}

function ensureExcavatorFieldOutbox(shell) {
    if (!shell || !window.createExcavatorFieldOutbox) return null;
    var accessId = String(shell.dataset.eoAccessId || "");
    var employeeId = String(shell.dataset.eoEmployeeId || "");
    var deviceId = excavatorInstallDeviceId();
    if (!accessId || !employeeId || !deviceId) {
        renderExcavatorOfflineStatus({storage_failed: true, total: 0});
        return null;
    }
    var queueKey = ["excavator", accessId, deviceId].join(":");
    if (window.eoExcavatorFieldOutbox && window.eoExcavatorFieldOutboxKey === queueKey) {
        return window.eoExcavatorFieldOutbox;
    }
    window.eoExcavatorFieldOutboxKey = queueKey;
    window.eoExcavatorFieldOutbox = window.createExcavatorFieldOutbox({
        indexedDB: window.indexedDB,
        localStorage: window.localStorage,
        queueKey: queueKey,
        onChange: function (summary, events, extra) {
            renderExcavatorOfflineStatus(summary);
            if (typeof window.applyExcavatorPendingFieldEvents === "function") {
                window.applyExcavatorPendingFieldEvents(events, extra || {});
            }
        },
        onConfirmed: function (event, result) {
            if (typeof window.confirmExcavatorFieldEvent === "function") {
                return window.confirmExcavatorFieldEvent(event, result);
            }
        },
        onAttention: function (event, result) {
            if (typeof window.attendExcavatorFieldEvent === "function") {
                window.attendExcavatorFieldEvent(event, result);
            }
        },
        send: function (events) {
            var current = document.querySelector("[data-eo-shell]");
            var url = current ? current.dataset.eoOfflineSyncUrl || "" : "";
            var tokenNode = current ? current.querySelector("input[name='csrfmiddlewaretoken']") : null;
            if (!url || !tokenNode) return Promise.reject(new Error("Синхронизация сейчас недоступна."));
            var controller = window.AbortController ? new AbortController() : null;
            var timeout = controller ? window.setTimeout(function () { controller.abort(); }, 12000) : null;
            return fetch(url, {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "X-CSRFToken": tokenNode.value,
                    "X-Requested-With": "XMLHttpRequest"
                },
                body: JSON.stringify({
                    protocol_version: 1,
                    role_code: "excavator_operator",
                    device_id: deviceId,
                    events: events
                }),
                signal: controller ? controller.signal : undefined
            }).then(function (response) {
                return response.json().catch(function () { return {}; }).then(function (payload) {
                    if (response.status === 401 || response.status === 403) {
                        return {ok: false, results: events.map(function (event) {
                            return {event_id: event.event_id, status: "auth_required", message: payload.error || "Требуется повторный вход."};
                        })};
                    }
                    if (response.status >= 500 || response.status === 429) throw new Error("Сервер временно недоступен.");
                    if (!response.ok && !Array.isArray(payload.results)) throw new Error(payload.error || "События не приняты.");
                    return payload;
                });
            }).finally(function () {
                if (timeout) window.clearTimeout(timeout);
            });
        }
    });
    window.eoExcavatorFieldOutbox.ready({resumeAuthRequired: navigator.onLine !== false}).then(function () {
        if (navigator.onLine !== false) window.eoExcavatorFieldOutbox.flush();
    }).catch(function () {
        renderExcavatorOfflineStatus({storage_failed: true, total: 0});
    });
    if (!window.eoExcavatorOutboxRetryBound) {
        window.eoExcavatorOutboxRetryBound = true;
        window.addEventListener("online", function () {
            if (window.eoExcavatorFieldOutbox) window.eoExcavatorFieldOutbox.retryNow();
        });
        window.addEventListener("offline", function () {
            if (window.eoExcavatorFieldOutbox) {
                window.eoExcavatorFieldOutbox.pending().then(function (events) {
                    renderExcavatorOfflineStatus({total: events.length, pending: events.length, syncing: 0, attention: events.filter(function (event) { return ["conflict", "invalid", "auth_required"].indexOf(event.sync_state) >= 0; }).length});
                });
            }
        });
        document.addEventListener("visibilitychange", function () {
            if (!document.hidden && window.eoExcavatorFieldOutbox) window.eoExcavatorFieldOutbox.flush();
        });
        window.addEventListener("native-connectivity-resume", function () {
            if (window.eoExcavatorFieldOutbox) window.eoExcavatorFieldOutbox.flush();
        });
    }
    return window.eoExcavatorFieldOutbox;
}


window.ExcavatorWorkRuntime = {
    scheduleExcavatorViewportHeightSync: scheduleExcavatorViewportHeightSync,
    refreshExcavatorWorkFromServer: refreshExcavatorWorkFromServer,
    invalidateExcavatorWorkRefresh: invalidateExcavatorWorkRefresh,
    bindExcavatorTransferCountdowns: bindExcavatorTransferCountdowns,
    excavatorInstallDeviceId: excavatorInstallDeviceId,
    newExcavatorFieldId: newExcavatorFieldId,
    legacyExcavatorFieldSequence: legacyExcavatorFieldSequence,
    renderExcavatorOfflineStatus: renderExcavatorOfflineStatus,
    ensureExcavatorFieldOutbox: ensureExcavatorFieldOutbox
};
})();
