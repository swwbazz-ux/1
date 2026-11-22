(function () {
var excavatorNotice = document.querySelector("[data-eo-notice]");
var excavatorNoticeTimer = null;
var excavatorNoticeLastKey = "";
var excavatorNoticeLastShownAt = 0;

function compactExcavatorNotice(message) {
    var text = String(message || "Действие не выполнено").trim();
    var replacements = {
        "Сначала нужно открыть смену на экскаваторе.": "Сначала откройте смену.",
        "Смена и техника не назначены. Обратитесь к руководителю.": "Смена и техника не назначены.",
        "Техника неактивна. Обратитесь к руководителю.": "Техника неактивна.",
        "Назначенная техника неактивна. Обратитесь к руководителю.": "Назначенная техника неактивна.",
        "Назначенная техника уже занята в открытой смене. Обратитесь к руководителю.": "Техника занята в другой смене.",
        "Выберите породу и хотя бы одну точку разгрузки": "Выберите породу и точку разгрузки.",
        "Failed to fetch": "Сервер недоступен. Действие не сохранено.",
        "Load failed": "Сервер недоступен. Действие не сохранено.",
        "NetworkError when attempting to fetch resource.": "Сервер недоступен. Действие не сохранено."
    };
    return replacements[text] || text;
}

function closeExcavatorNotice() {
    window.clearTimeout(excavatorNoticeTimer);
    excavatorNoticeTimer = null;
    if (excavatorNotice) {
        excavatorNotice.hidden = true;
        excavatorNotice.classList.remove("is-shift-lock");
    }
}

function showExcavatorNotice(message, options) {
    if (!excavatorNotice) return;
    var text = compactExcavatorNotice(message);
    var isShiftLock = !!(options && options.variant === "shift-lock");
    var noticeKey = (isShiftLock ? "shift-lock:" : "default:") + text;
    var now = Date.now();
    if ((!excavatorNotice.hidden && excavatorNotice.textContent === text && excavatorNotice.classList.contains("is-shift-lock") === isShiftLock) || (excavatorNoticeLastKey === noticeKey && now - excavatorNoticeLastShownAt < 2200)) {
        return;
    }
    excavatorNoticeLastKey = noticeKey;
    excavatorNoticeLastShownAt = now;
    excavatorNotice.textContent = text;
    excavatorNotice.classList.toggle("is-shift-lock", isShiftLock);
    excavatorNotice.hidden = false;
    window.clearTimeout(excavatorNoticeTimer);
    excavatorNoticeTimer = window.setTimeout(closeExcavatorNotice, 3600);
}

window.showExcavatorNotice = showExcavatorNotice;

function playExcavatorSound(name) {
    if (!window.MobileOperationalSounds || typeof window.MobileOperationalSounds.play !== "function") {
        return Promise.resolve(false);
    }
    return window.MobileOperationalSounds.play(name);
}

function playExcavatorVoice(cue, voice, options) {
    options = options || {};
    if (!window.MobileOperationalSounds || typeof window.MobileOperationalSounds.announceOperational !== "function") {
        return playExcavatorSound(cue);
    }
    return window.MobileOperationalSounds.announceOperational({
        cue: cue,
        voice: voice,
        eventVersion: Number(options.eventVersion || 0),
        eventKey: String(options.eventKey || "")
    });
}

function playExcavatorEquipmentVoice(action, equipmentNumber, options) {
    options = options || {};
    if (!window.MobileOperationalSounds || typeof window.MobileOperationalSounds.announceEquipment !== "function") {
        return playExcavatorVoice(
            String(options.cue || "truck_assigned"),
            String(options.fallbackVoice || "voice_truck_assigned"),
            options
        );
    }
    return window.MobileOperationalSounds.announceEquipment({
        cue: String(options.cue || "truck_assigned"),
        action: String(action || ""),
        equipmentNumber: String(equipmentNumber || ""),
        dumpPointId: Number(options.dumpPointId || 0),
        dumpPointName: String(options.dumpPointName || ""),
        fallbackVoice: String(options.fallbackVoice || "voice_truck_assigned"),
        eventVersion: Number(options.eventVersion || 0),
        eventKey: String(options.eventKey || ""),
        operationKey: String(options.operationKey || "")
    });
}

/* EXCAVATOR_VOICE_GUARD_START */
window.ExcavatorVoiceGuard = (function () {
    var LIMIT = 64;
    var order = [];
    var states = Object.create(null);

    function forget(operationKey) {
        delete states[operationKey];
        var position = order.indexOf(operationKey);
        if (position !== -1) order.splice(position, 1);
    }

    function claim(operationKey) {
        if (!operationKey || states[operationKey]) return false;
        states[operationKey] = "claimed";
        order.push(operationKey);
        while (order.length > LIMIT) {
            delete states[order.shift()];
        }
        return true;
    }

    function finalize(operationKey) {
        if (operationKey && states[operationKey]) states[operationKey] = "announced";
    }

    function release(operationKey) {
        if (operationKey && states[operationKey] === "claimed") forget(operationKey);
    }

    function state(operationKey) {
        return (operationKey && states[operationKey]) || "";
    }

    return {claim: claim, finalize: finalize, release: release, state: state};
})();

/* Сроки ожидания голоса.

   Та же семантика, что на экране водителя (static/js/driver-shift-voice-v1.js):
   голосовой ответ подтверждает общий курсор realtime, поэтому ожидание
   ограничено. Звуковой слой отвечает за 1500 мс
   (static/js/mobile-operational-sounds-v1.js); 2500 мс — рубеж на случай, если
   он окажется старее этого экрана и зависнет (T-016, e2e §10.1). */
var EXCAVATOR_VOICE_DEADLINE_MS = 2500;
var EXCAVATOR_VOICE_TIMEOUT_REASONS = {audio_timeout: true, bridge_timeout: true};

function excavatorVoiceTimedOut(result) {
    return !!(result && EXCAVATOR_VOICE_TIMEOUT_REASONS[String(result.reason || "")]);
}

function excavatorVoiceWithoutChannel(result) {
    return !!result && typeof result === "object" && result.supported === false && result.announced !== true;
}

function clearExcavatorVoiceTimer(timer) {
    if (typeof window !== "undefined" && typeof window.clearTimeout === "function") {
        window.clearTimeout(timer);
    }
}

function withExcavatorVoiceDeadline(operationKeys, pending) {
    return new Promise(function (resolve) {
        var settled = false;
        var timer = window.setTimeout(function () {
            if (settled) return;
            settled = true;
            (operationKeys || []).forEach(function (operationKey) {
                window.ExcavatorVoiceGuard.release(operationKey);
            });
            resolve(true);
        }, EXCAVATOR_VOICE_DEADLINE_MS);
        Promise.resolve(pending).then(function (value) {
            if (settled) return;
            settled = true;
            clearExcavatorVoiceTimer(timer);
            resolve(value === true);
        }, function () {
            if (settled) return;
            settled = true;
            clearExcavatorVoiceTimer(timer);
            resolve(false);
        });
    });
}

function settleExcavatorVoiceClaims(operationKeys, result) {
    var announced = result === true || !!(result && result.announced === true);
    var ownedElsewhere = !!(result && String(result.reason || "") === "already_announced");
    (operationKeys || []).forEach(function (operationKey) {
        if (announced || ownedElsewhere) {
            window.ExcavatorVoiceGuard.finalize(operationKey);
        } else {
            window.ExcavatorVoiceGuard.release(operationKey);
        }
    });
    /* Ответа за срок нет или канала озвучки нет: заявки освобождены, поэтому
       следующая доставка попробует снова, но общий курсор таким событием не
       держим — иначе недоступный звук останавливает экран (T-016, e2e §10.1). */
    return announced || ownedElsewhere || excavatorVoiceTimedOut(result) || excavatorVoiceWithoutChannel(result);
}

function excavatorAssignmentOperationKey(kind, assignmentId, excavatorId) {
    var normalizedKind = String(kind || "").trim();
    var normalizedAssignmentId = String(assignmentId || "").trim();
    var normalizedExcavatorId = String(excavatorId || "").trim();
    if (!normalizedKind || !normalizedAssignmentId || !normalizedExcavatorId) return "";
    return ["excavator-assignment", normalizedKind, normalizedAssignmentId, normalizedExcavatorId].join(":");
}

function currentExcavatorVoiceEquipmentId(root) {
    var shell = root || document.querySelector("[data-eo-shell]");
    return shell
        ? String(shell.dataset.eoCurrentExcavatorId || shell.dataset.eoShiftExcavatorId || "").trim()
        : "";
}

function excavatorEventContainsId(values, expected) {
    var normalizedExpected = String(expected || "");
    return Array.isArray(values) && values.some(function (value) {
        return String(value || "") === normalizedExpected;
    });
}

function latestExcavatorAssignmentOperations(context) {
    var excavatorId = currentExcavatorVoiceEquipmentId();
    var events = context && Array.isArray(context.events) ? context.events : [];
    var latestByTruck = Object.create(null);
    if (!excavatorId) return [];

    events.forEach(function (event) {
        var payload = event && event.payload ? event.payload : null;
        var version = Number(event && event.version || 0);
        if (!payload || event.type !== "assignment_changed" || version <= 0) return;
        if (String(event.object_type || "") !== "HaulAssignment") return;
        var assignmentId = String(event.object_id || "").trim();
        var truckId = String(
            payload.truck_id
            || (Array.isArray(payload.truck_ids) ? payload.truck_ids[0] : "")
            || ""
        ).trim();
        var truckNumber = String(payload.truck_number || "").trim();
        if (!assignmentId || (!truckId && !truckNumber)) return;

        var action = String(payload.action || "");
        var targetExcavatorId = String(payload.target_excavator_id || "").trim();
        var wasRelated = excavatorEventContainsId(payload.excavator_ids, excavatorId);
        var kind = "";
        if (action === "assignment_applied" && targetExcavatorId === excavatorId) {
            kind = "assign";
        } else if (
            (action === "assignment_applied" || action === "release_applied")
            && wasRelated
        ) {
            kind = "remove";
        }
        if (!kind) return;

        var truckKey = truckId || "number:" + truckNumber;
        var previous = latestByTruck[truckKey];
        if (previous && previous.eventVersion >= version) return;
        latestByTruck[truckKey] = {
            kind: kind,
            action: kind === "assign" ? "excavator_truck_assigned" : "excavator_truck_removed",
            truckId: truckId,
            truckNumber: truckNumber,
            fallbackVoice: kind === "assign" ? "voice_truck_assigned" : "voice_truck_removed",
            eventVersion: version,
            operationKey: excavatorAssignmentOperationKey(kind, assignmentId, excavatorId)
        };
    });

    return Object.keys(latestByTruck).map(function (truckKey) {
        return latestByTruck[truckKey];
    }).sort(function (left, right) {
        return left.eventVersion - right.eventVersion;
    });
}

function playExcavatorAssignmentAlerts(operations) {
    var blocked = false;
    var claimed = (operations || []).filter(function (operation) {
        if (!operation || !operation.operationKey) return false;
        if (window.ExcavatorVoiceGuard.claim(operation.operationKey)) return true;
        if (window.ExcavatorVoiceGuard.state(operation.operationKey) !== "announced") blocked = true;
        return false;
    });
    if (!claimed.length) return Promise.resolve(!blocked);

    if (typeof window.excavatorHaptic === "function") {
        window.excavatorHaptic([180, 90, 180], 255);
    }

    var operationKeys = claimed.map(function (operation) { return operation.operationKey; });
    var sounds = window.MobileOperationalSounds;
    var pending;
    try {
        if (sounds && typeof sounds.announceEquipmentBatch === "function") {
            pending = sounds.announceEquipmentBatch({
                cue: "truck_assigned",
                eventKey: "excavator_operator_assignment",
                items: claimed.map(function (operation) {
                    return {
                        action: operation.action,
                        equipmentNumber: operation.truckNumber,
                        fallbackVoice: operation.fallbackVoice,
                        eventVersion: Number(operation.eventVersion || 0),
                        operationKey: operation.operationKey
                    };
                })
            });
        } else {
            var first = claimed[0];
            pending = playExcavatorEquipmentVoice(first.action, first.truckNumber, {
                cue: "truck_assigned",
                fallbackVoice: first.fallbackVoice,
                eventVersion: Number(first.eventVersion || 0),
                eventKey: "excavator_operator_assignment",
                operationKey: first.operationKey
            });
        }
    } catch (error) {
        operationKeys.forEach(window.ExcavatorVoiceGuard.release);
        return Promise.resolve(false);
    }
    return withExcavatorVoiceDeadline(operationKeys, Promise.resolve(pending).then(function (result) {
        return settleExcavatorVoiceClaims(operationKeys, result) && !blocked;
    }, function () {
        operationKeys.forEach(window.ExcavatorVoiceGuard.release);
        return false;
    }));
}

function playExcavatorAssignmentAlert(action, truckNumber, voice, eventVersion, options) {
    options = options || {};
    playExcavatorAssignmentAlerts([{
        action: action,
        truckNumber: String(truckNumber || ""),
        fallbackVoice: voice || "voice_truck_assigned",
        eventVersion: Number(eventVersion || 0),
        operationKey: String(options.operationKey || "")
    }]);
}

window.handleOperationalStateSignals = function (context) {
    /* Признак «принято» нужен общему курсору realtime: пока роль не подтвердила
       голосовую работу, версия дельты остаётся неподтверждённой (realtime-client.js).
       Пакетная озвучка ограничена сроком (withExcavatorVoiceDeadline), поэтому
       этот промис разрешается всегда: зависший звук не держит экран. */
    return playExcavatorAssignmentAlerts(latestExcavatorAssignmentOperations(context)).then(function (accepted) {
        return {accepted: accepted === true};
    });
};
/* EXCAVATOR_VOICE_GUARD_END */

function readExcavatorAssignmentSnapshot(root) {
    var snapshot = {};
    if (!root) return snapshot;
    root.querySelectorAll("[data-eo-assignment-snapshot] [data-truck-id]").forEach(function (item) {
        var truckId = String(item.dataset.truckId || "");
        if (!truckId) return;
        snapshot[truckId] = {
            assignmentId: String(item.dataset.assignmentId || ""),
            truckNumber: String(item.dataset.truckNumber || truckId)
        };
    });
    return snapshot;
}

function excavatorAssignmentStorageKey(root) {
    var equipmentId = root && (root.dataset.eoShiftExcavatorId || root.dataset.eoCurrentExcavatorId);
    return equipmentId ? "excavator-known-trucks:" + equipmentId : "";
}

function readStoredExcavatorAssignments(root) {
    var key = excavatorAssignmentStorageKey(root);
    if (!key) return null;
    try {
        var stored = JSON.parse(window.localStorage.getItem(key) || "null");
        return stored && typeof stored === "object" ? stored : null;
    } catch (error) {
        return null;
    }
}

function storeExcavatorAssignments(root, snapshot) {
    var key = excavatorAssignmentStorageKey(root);
    if (!key) return;
    try {
        window.localStorage.setItem(key, JSON.stringify(snapshot || {}));
    } catch (error) {}
}

function normalizeExcavatorAssignmentSnapshotItem(value, truckId) {
    if (value && typeof value === "object") {
        return {
            assignmentId: String(value.assignmentId || ""),
            truckNumber: String(value.truckNumber || truckId)
        };
    }
    return {
        assignmentId: "",
        truckNumber: String(value || truckId)
    };
}

function announceExcavatorAssignmentChanges(previousSnapshot, nextSnapshot, eventVersion) {
    var previous = previousSnapshot || {};
    var next = nextSnapshot || {};
    var addedTruckIds = Object.keys(next).filter(function (truckId) {
        return !previousSnapshot || !Object.prototype.hasOwnProperty.call(previous, truckId);
    });
    /* Снятый самосвал раньше исчезал молча, и машинист мог ждать машину,
       которую у него уже забрали. Об этом надо сообщать так же, как о
       назначении. */
    var removedTruckIds = !previousSnapshot ? [] : Object.keys(previous).filter(function (truckId) {
        return !Object.prototype.hasOwnProperty.call(next, truckId);
    });
    var addedNumbers = addedTruckIds.map(function (truckId) {
        return normalizeExcavatorAssignmentSnapshotItem(next[truckId], truckId).truckNumber;
    });
    var removedNumbers = removedTruckIds.map(function (truckId) {
        return normalizeExcavatorAssignmentSnapshotItem(previous[truckId], truckId).truckNumber;
    });
    if (!addedNumbers.length && !removedNumbers.length) return;

    var parts = [];
    if (addedNumbers.length) {
        parts.push(
            addedNumbers.length === 1
                ? "Назначен самосвал " + addedNumbers[0] + "."
                : "Назначены самосвалы: " + addedNumbers.join(", ") + "."
        );
    }
    if (removedNumbers.length) {
        parts.push(
            removedNumbers.length === 1
                ? "Снят самосвал " + removedNumbers[0] + "."
                : "Сняты самосвалы: " + removedNumbers.join(", ") + "."
        );
    }
    var excavatorId = currentExcavatorVoiceEquipmentId();
    var fallbackOperations = addedTruckIds.map(function (truckId) {
        var item = normalizeExcavatorAssignmentSnapshotItem(next[truckId], truckId);
        return {
            kind: "assign",
            action: "excavator_truck_assigned",
            truckId: truckId,
            truckNumber: item.truckNumber,
            fallbackVoice: "voice_truck_assigned",
            eventVersion: Number(eventVersion || 0),
            operationKey: excavatorAssignmentOperationKey("assign", item.assignmentId, excavatorId)
        };
    });
    /* Снятие принадлежит точному release_applied из раннего сигнала или
       heartbeat. В DOM уже нет id снимающей операции, поэтому state-based
       вызов здесь создал бы второй, несовпадающий ключ. */
    playExcavatorAssignmentAlerts(fallbackOperations);
    showExcavatorNotice(parts.join(" "));
}

function syncExcavatorAssignmentSnapshot(root, previousSnapshot, eventVersion, options) {
    options = options || {};
    if (!root) return;
    var nextSnapshot = readExcavatorAssignmentSnapshot(root);
    var baseline = previousSnapshot;
    if (baseline === undefined) {
        baseline = readStoredExcavatorAssignments(root);
    }
    if (baseline && options.announce !== false) {
        announceExcavatorAssignmentChanges(baseline, nextSnapshot, eventVersion);
    }
    storeExcavatorAssignments(root, nextSnapshot);
}

if (typeof window.bindAchievementPrizeUnlock === "function") {
    window.bindAchievementPrizeUnlock();
}


window.ExcavatorWorkFeedback = {
    compactExcavatorNotice: compactExcavatorNotice,
    showExcavatorNotice: showExcavatorNotice,
    playExcavatorSound: playExcavatorSound,
    playExcavatorVoice: playExcavatorVoice,
    playExcavatorEquipmentVoice: playExcavatorEquipmentVoice,
    readExcavatorAssignmentSnapshot: readExcavatorAssignmentSnapshot,
    storeExcavatorAssignments: storeExcavatorAssignments,
    syncExcavatorAssignmentSnapshot: syncExcavatorAssignmentSnapshot
};
})();
