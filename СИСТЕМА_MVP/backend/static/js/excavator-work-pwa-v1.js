(function () {
var shell = document.querySelector("[data-eo-shell]");
var excavatorShellVersion = shell.dataset.eoPwaVersion;
var excavatorServiceWorkerScope = shell.dataset.eoSwScope;

function formatExcavatorVersion(version) {
    var normalized = String(version || "").trim();
    if (!normalized || normalized === "new" || normalized === "unknown") {
        return "...";
    }
    if (/mobile-shell-v\d+$/.test(normalized) && normalized.indexOf("excavator-mobile-shell-") !== 0) {
        return "...";
    }
    return normalized.replace("excavator-mobile-shell-", "");
}

function normalizeExcavatorVersion(version) {
    var normalized = String(version || "").trim();
    return /^excavator-mobile-shell-v\d+$/.test(normalized) ? normalized : "";
}

function getExcavatorVersionNumber(version) {
    var match = String(version || "").match(/(?:shell-v|v)(\d+)$/);
    return match ? parseInt(match[1], 10) : 0;
}

function isExcavatorVersionNewer(newVersion, currentVersion) {
    return getExcavatorVersionNumber(newVersion) > getExcavatorVersionNumber(currentVersion || excavatorShellVersion);
}

function requestExcavatorWorkerVersion(worker) {
    if (!worker || !window.MessageChannel) return Promise.resolve("");
    return new Promise(function (resolve) {
        var channel = new MessageChannel();
        var timer = window.setTimeout(function () { resolve(""); }, 900);
        channel.port1.onmessage = function (event) {
            window.clearTimeout(timer);
            resolve(normalizeExcavatorVersion((event.data || {}).version));
        };
        try {
            worker.postMessage({ type: "GET_VERSION" }, [channel.port2]);
        } catch (error) {
            window.clearTimeout(timer);
            resolve("");
        }
    });
}

function initExcavatorPwaUpdates() {
    if (!("serviceWorker" in navigator)) return;
    var modal = document.querySelector("[data-eo-pwa-update-modal]");
    var currentNode = document.querySelector("[data-eo-pwa-current-version]");
    var newNode = document.querySelector("[data-eo-pwa-new-version]");
    var statusNode = document.querySelector("[data-eo-pwa-update-status]");
    var textNode = document.querySelector("[data-eo-pwa-update-text]");
    var laterButton = document.querySelector("[data-eo-pwa-update-later]");
    var applyButton = document.querySelector("[data-eo-pwa-update-apply]");
    var registrationRef = null;
    var waitingWorker = null;
    var waitingInspectionGeneration = 0;
    var contractSyncGeneration = 0;
    var storageKey = "excavator-pwa-update-available";

    function readStoredUpdate() {
        try {
            return JSON.parse(localStorage.getItem(storageKey) || "{}") || {};
        } catch (error) {
            return {};
        }
    }

    function rememberUpdate(newVersion, currentVersion) {
        try {
            localStorage.setItem(storageKey, JSON.stringify({
                newVersion: newVersion || "",
                currentVersion: currentVersion || excavatorShellVersion,
                savedAt: Date.now()
            }));
        } catch (error) {}
    }

    function clearUpdate() {
        try {
            localStorage.removeItem(storageKey);
        } catch (error) {}
    }

    function setIndicator(isAvailable, newVersion, currentVersion) {
        var visible = !!(isAvailable && isExcavatorVersionNewer(newVersion, currentVersion || excavatorShellVersion));
        document.querySelectorAll("[data-eo-pwa-update-badge]").forEach(function (badge) {
            badge.hidden = !visible;
        });
        document.querySelectorAll("[data-eo-pwa-update-nav-target]").forEach(function (item) {
            item.classList.toggle("has-pwa-update", visible);
            item.setAttribute("aria-label", visible ? "Смена, доступно обновление" : "Смена");
            if (visible) {
                item.title = "Доступна новая версия приложения";
            } else {
                item.removeAttribute("title");
            }
        });
        if (visible) {
            rememberUpdate(newVersion, currentVersion);
        } else {
            clearUpdate();
        }
    }

    function refreshIndicatorFromStorage(currentVersion) {
        var stored = readStoredUpdate();
        if (stored.newVersion && isExcavatorVersionNewer(stored.newVersion, currentVersion || stored.currentVersion || excavatorShellVersion)) {
            setIndicator(true, stored.newVersion, currentVersion || stored.currentVersion || excavatorShellVersion);
        } else if (stored.newVersion) {
            setIndicator(false);
        }
    }

    function serverContractVersion() {
        var guard = window.AppPwaContractGuard;
        var state = guard && typeof guard.getState === "function"
            ? guard.getState()
            : null;
        return normalizeExcavatorVersion(
            state && state.server ? state.server.shellVersion : ""
        );
    }

    function getCurrentVersion() {
        var registrationPromise = registrationRef ? Promise.resolve(registrationRef) : navigator.serviceWorker.getRegistration(excavatorServiceWorkerScope);
        return registrationPromise.then(function (registration) {
            if (registration) registrationRef = registration;
            return requestExcavatorWorkerVersion(registration && registration.active);
        }).then(function (version) {
            return version || excavatorShellVersion;
        }).catch(function () {
            return excavatorShellVersion;
        });
    }

    function resolveExcavatorUpdateVersion(worker, newVersion, currentVersion) {
        var baselineVersion = currentVersion || excavatorShellVersion;
        if (isExcavatorVersionNewer(newVersion, baselineVersion)) {
            return Promise.resolve(newVersion);
        }
        return requestExcavatorWorkerVersion(worker).then(function (workerVersion) {
            if (isExcavatorVersionNewer(workerVersion, baselineVersion)) {
                return workerVersion;
            }
            return serverContractVersion();
        }).then(function (serverVersion) {
            return isExcavatorVersionNewer(serverVersion, baselineVersion) ? serverVersion : "";
        }).catch(function () {
            return "";
        });
    }

    function showUpdateModal(worker, newVersion, currentVersion) {
        if (!modal) return;
        var knownNewVersion = isExcavatorVersionNewer(newVersion, currentVersion || excavatorShellVersion) ? newVersion : "";
        waitingWorker = worker || null;

        function renderUpdateModal(version) {
            setIndicator(true, version, currentVersion || excavatorShellVersion);
            if (currentNode) currentNode.textContent = formatExcavatorVersion(currentVersion || excavatorShellVersion);
            if (newNode) newNode.textContent = formatExcavatorVersion(version);
            if (textNode) textNode.textContent = "Сервер загрузил новую оболочку Экскаваторщика. Установите ее, когда можно обновить экран.";
            if (statusNode) statusNode.textContent = worker ? "Обновление установится только после нажатия кнопки." : "Браузер еще готовит новую версию. Проверьте позже.";
            if (applyButton) applyButton.disabled = !worker;
            modal.hidden = false;
        }

        if (knownNewVersion) {
            renderUpdateModal(knownNewVersion);
            return;
        }

        resolveExcavatorUpdateVersion(worker, "", currentVersion || excavatorShellVersion).then(function (resolvedVersion) {
            if (resolvedVersion) {
                renderUpdateModal(resolvedVersion);
            } else {
                modal.hidden = true;
                setIndicator(false);
            }
        });
    }

    function inspectWaitingWorker(registration, worker) {
        if (
            !registration
            || !worker
            || registration.waiting !== worker
            || worker === navigator.serviceWorker.controller
            || worker.state === "activated"
        ) {
            return Promise.resolve(false);
        }
        var inspectionGeneration = ++waitingInspectionGeneration;
        function isCurrentInspection() {
            return (
                inspectionGeneration === waitingInspectionGeneration
                && registrationRef === registration
                && registration.waiting === worker
                && worker !== navigator.serviceWorker.controller
                && worker.state !== "activated"
            );
        }
        return Promise.all([
            requestExcavatorWorkerVersion(registration.active),
            requestExcavatorWorkerVersion(worker)
        ]).then(function (versions) {
            if (!isCurrentInspection()) return false;
            return resolveExcavatorUpdateVersion(
                worker,
                versions[1],
                versions[0]
            ).then(function (resolvedVersion) {
                if (!isCurrentInspection() || !resolvedVersion) return false;
                showUpdateModal(worker, resolvedVersion, versions[0]);
                return true;
            });
        });
    }

    function syncContractState() {
        var syncGeneration = ++contractSyncGeneration;
        if (
            waitingWorker
            && (
                !registrationRef
                || registrationRef.waiting !== waitingWorker
                || waitingWorker === navigator.serviceWorker.controller
                || waitingWorker.state === "activated"
            )
        ) {
            waitingInspectionGeneration += 1;
            waitingWorker = null;
            if (modal) modal.hidden = true;
            setIndicator(false);
        }
        refreshIndicatorFromStorage();
        var controllerSnapshot = navigator.serviceWorker.controller;
        var registrationSnapshot = registrationRef;
        var activeWorkerSnapshot = registrationSnapshot && registrationSnapshot.active;
        function isCurrentSync() {
            return (
                syncGeneration === contractSyncGeneration
                && navigator.serviceWorker.controller === controllerSnapshot
                && registrationRef === registrationSnapshot
                && (
                    !registrationSnapshot
                    || registrationSnapshot.active === activeWorkerSnapshot
                )
            );
        }
        return getCurrentVersion().then(function (currentVersion) {
            if (!isCurrentSync()) return false;
            currentVersion = currentVersion || excavatorShellVersion;
            var serverVersion = serverContractVersion();
            if (serverVersion && isExcavatorVersionNewer(serverVersion, currentVersion)) {
                setIndicator(true, serverVersion, currentVersion);
                return true;
            }
            if (serverVersion && formatExcavatorVersion(serverVersion) === formatExcavatorVersion(currentVersion)) {
                setIndicator(false);
            }
            return false;
        }).catch(function () {
            if (!isCurrentSync()) return false;
            refreshIndicatorFromStorage();
            return false;
        });
    }

    function applyUpdate() {
        var worker = waitingWorker;
        if (!worker) {
            if (modal) modal.hidden = true;
            return;
        }
        if (
            !registrationRef
            || registrationRef.waiting !== worker
            || worker === navigator.serviceWorker.controller
            || worker.state === "activated"
        ) {
            waitingWorker = null;
            if (modal) modal.hidden = true;
            syncContractState();
            return;
        }
        waitingWorker = null;
        if (statusNode) statusNode.textContent = "Устанавливаем новую версию...";
        if (applyButton) applyButton.disabled = true;
        if (laterButton) laterButton.disabled = true;
        if (modal) modal.hidden = true;
        try {
            worker.postMessage({ type: "SKIP_WAITING" });
        } catch (error) {
            if (
                registrationRef
                && registrationRef.waiting === worker
                && worker !== navigator.serviceWorker.controller
                && worker.state !== "activated"
            ) {
                waitingWorker = worker;
            }
            if (statusNode) statusNode.textContent = "Не удалось запустить обновление. Повторите позже.";
            if (applyButton) applyButton.disabled = false;
            if (laterButton) laterButton.disabled = false;
        }
    }

    if (laterButton) {
        laterButton.addEventListener("click", function () {
            if (modal) modal.hidden = true;
        });
    }
    if (applyButton) {
        applyButton.addEventListener("click", applyUpdate);
    }
    if (modal) {
        modal.addEventListener("click", function (event) {
            if (event.target === modal) modal.hidden = true;
        });
    }
    function watchRegistration(registration) {
        if (!registration) return;
        if (registration.waiting) {
            inspectWaitingWorker(registration, registration.waiting);
        }
        registration.addEventListener("updatefound", function () {
            var installing = registration.installing;
            if (!installing) return;
            installing.addEventListener("statechange", function () {
                if (installing.state === "installed" && navigator.serviceWorker.controller) {
                    inspectWaitingWorker(registration, registration.waiting || installing);
                }
            });
        });
    }

    var excavatorGuard = window.AppPwaContractGuard;
    var excavatorRegistrationSource = excavatorGuard
        && typeof excavatorGuard.getRegistration === "function"
        ? excavatorGuard.getRegistration()
        : navigator.serviceWorker.getRegistration(excavatorServiceWorkerScope);
    Promise.resolve(excavatorRegistrationSource).then(function (registration) {
        registrationRef = registration;
        watchRegistration(registration);
        syncContractState();
    }).catch(function () {});
    window.addEventListener("app-pwa-contract-state", syncContractState);
    navigator.serviceWorker.addEventListener("controllerchange", syncContractState);
}


window.ExcavatorWorkPwa = {
    initExcavatorPwaUpdates: initExcavatorPwaUpdates
};
})();
