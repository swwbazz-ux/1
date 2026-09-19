/* Звук и голосовые события экрана водителя, а также признак «экран занят»
   (window.driverHasPendingWork), по которому обновление откладывается,
   чтобы не оборвать начатое водителем действие. Вынесено из driver-shift-v1.js без изменений. */
if (typeof window.bindAchievementPrizeUnlock === "function") {
    window.bindAchievementPrizeUnlock();
}

function isDriverOperationalRefreshUnsafe(shell) {
    if (document.hidden) return true;
    shell = shell || document.querySelector("[data-driver-shell]");
    if (!shell) return false;
    var active = document.activeElement;
    var activeTag = active && active.tagName ? active.tagName.toLowerCase() : "";
    if (
        active &&
        shell.contains(active) &&
        (active.isContentEditable || activeTag === "input" || activeTag === "textarea" || activeTag === "select")
    ) {
        return true;
    }
    var openingShiftForm = shell.querySelector(".driver-shift-opening-form");
    if (openingShiftForm && (
        openingShiftForm.dataset.driverShiftOpeningDirty === "true" ||
        openingShiftForm.dataset.driverShiftOpeningPending === "true" ||
        openingShiftForm.querySelector(".errorlist")
    )) {
        return true;
    }
    var activeShiftForm = shell.querySelector("[data-driver-shift-close-form]");
    if (activeShiftForm && (
        activeShiftForm.dataset.driverShiftDirty === "true" ||
        activeShiftForm.querySelector(".errorlist")
    )) {
        return true;
    }
    /* Всё, что выше, — жёсткие причины: прервать их значит потерять то, что
       водитель уже ввёл руками. Ниже — мягкие: залипший признак начатого
       жеста, открытая шторка, неотправленное действие. Любой из них может
       залипнуть и остановить экран навсегда, поэтому самовосстановление
       (driver-self-heal-v1.js) после минуты отставания от сервера открывает
       окно, в котором мягкие причины обновление больше не держат. */
    if (
        typeof window !== "undefined" &&
        Number(window.driverRefreshBypassBusyUntil || 0) > Date.now()
    ) return false;
    if (
        typeof window !== "undefined" &&
        Number(window.driverOfflinePendingCount || 0) > 0
    ) return true;
    return !!(
        shell.querySelector(".is-touch-armed, .is-holding, .is-pending, .is-dragging, .is-lifting, .is-dropping, .is-snapping, .driver-drum-ghost, [data-driver-point-sheet]:not([hidden]), [data-driver-free-bucket-sheet]:not([hidden])") ||
        document.querySelector("[data-driver-pwa-update-modal]:not([hidden]), .app-confirm-modal:not([hidden])")
    );
}
window.driverHasPendingWork = isDriverOperationalRefreshUnsafe;
if (
    window.AppPwaContractGuard
    && typeof window.AppPwaContractGuard.registerUnsafeCheck === "function"
) {
    window.AppPwaContractGuard.registerUnsafeCheck(isDriverOperationalRefreshUnsafe);
}

/* DRIVER_VOICE_GUARD_START */
/* Одно рабочее событие приходит на экран по трём путям: ранним сигналом сразу
   после ответа сервера, слушателем «operational-state-refresh-applied» и
   сравнением DOM после подмены. Раньше каждый путь решал сам, и отметка о
   произнесённом ставилась уже после возврата из моста — то есть два
   синхронных вызова подряд успевали пройти проверку оба.

   Владелец решения теперь один, и заявка ставится синхронно, до обращения к
   мосту. Ключ — идентификатор самой операции (назначение или рейс), а не
   версия состояния: его одинаково знают и событие сервера, и разметка. */
window.DriverVoiceGuard = (function () {
    var LIMIT = 32;
    var order = [];
    var states = Object.create(null);

    function forget(opKey) {
        delete states[opKey];
        var position = order.indexOf(opKey);
        if (position !== -1) order.splice(position, 1);
    }

    function claim(opKey) {
        if (!opKey) return false;
        if (states[opKey]) return false;
        states[opKey] = "claimed";
        order.push(opKey);
        while (order.length > LIMIT) {
            delete states[order.shift()];
        }
        return true;
    }

    /* Заявка закрывается, когда звук фактически пошёл либо когда событием уже
       владеет другой канал. Освобождать можно только полную тишину, иначе
       следующий путь повторит то, что человек уже услышал. */
    function finalize(opKey) {
        if (opKey && states[opKey]) states[opKey] = "announced";
    }

    function release(opKey) {
        if (opKey && states[opKey] === "claimed") forget(opKey);
    }

    function state(opKey) {
        return (opKey && states[opKey]) || "";
    }

    return {claim: claim, finalize: finalize, release: release, state: state};
})();

function settleDriverVoiceClaim(opKey, result) {
    if (!opKey) return;
    /* playDriverSound и резервный веб-звук отвечают простым признаком
       «сыграло», нативный мост — объектом. Прозвучавшее в любой форме
       закрывает заявку. */
    var announced = result === true || !!(result && result.announced === true);
    var ownedElsewhere = !!(result && String(result.reason || "") === "already_announced");
    if (announced || ownedElsewhere) {
        window.DriverVoiceGuard.finalize(opKey);
        return;
    }
    window.DriverVoiceGuard.release(opKey);
}

/* Единственная точка обращения к мосту под заявкой.

   Мост может не только вернуть отказ, но и бросить исключение — синхронно при
   вызове или отклонением промиса. Без освобождения заявка осталась бы в
   состоянии «принято» до перезагрузки страницы, и событие замолчало бы
   навсегда: ни резервный путь по DOM, ни повторный опрос его уже не подняли
   бы. Любой сбой возвращает операцию следующему источнику. */
function announceDriverVoiceUnderClaim(opKey, produceAnnouncement) {
    var pending;
    try {
        pending = produceAnnouncement();
    } catch (error) {
        window.DriverVoiceGuard.release(opKey);
        return;
    }
    Promise.resolve(pending).then(function (result) {
        settleDriverVoiceClaim(opKey, result);
    }, function () {
        window.DriverVoiceGuard.release(opKey);
    });
}

/* Повторяет правило нативного heartbeat: водителю озвучиваются выданное
   назначение и снятое назначение. Из дельты берётся событие с наибольшей
   версией — если назначение успели перевыдать, прозвучит последнее, а не
   первое. Актуальность определяется состоянием, а не возрастом события. */
function latestDriverAssignmentEvent(context) {
    var selected = null;
    var events = context && Array.isArray(context.events) ? context.events : [];
    events.forEach(function (event) {
        var payload = event && event.payload ? event.payload : null;
        var version = Number(event && event.version || 0);
        if (!payload || !event || event.type !== "assignment_changed") return;
        if (selected && version <= selected.eventVersion) return;
        if (String(event.object_type || "") !== "HaulAssignment") return;
        var assignmentId = String(event.object_id || "").trim();
        if (!assignmentId) return;
        var action = String(payload.action || "");
        if (action === "assignment_pending") {
            selected = {
                eventVersion: version,
                assignmentId: assignmentId,
                kind: "assign",
                excavatorNumber: String(payload.target_excavator_number || "").trim()
            };
            return;
        }
        if (action === "release_applied") {
            selected = {
                eventVersion: version,
                assignmentId: assignmentId,
                kind: "release",
                excavatorNumber: ""
            };
        }
    });
    return selected;
}

window.handleOperationalStateSignals = function (context) {
    playDriverDumpPointAlert(context);
    var assignment = latestDriverAssignmentEvent(context);
    if (!assignment) return;
    playDriverAssignmentAlert(
        assignment.eventVersion,
        assignment.kind,
        assignment.excavatorNumber,
        {opKey: "assign:" + assignment.assignmentId}
    );
};
/* DRIVER_VOICE_GUARD_END */

function playDriverSound(name) {
    if (!window.MobileOperationalSounds || typeof window.MobileOperationalSounds.play !== "function") {
        return Promise.resolve(false);
    }
    return window.MobileOperationalSounds.play(name);
}

function playDriverVoice(cue, voice, options) {
    options = options || {};
    if (!window.MobileOperationalSounds || typeof window.MobileOperationalSounds.announceOperational !== "function") {
        return playDriverSound(cue);
    }
    return window.MobileOperationalSounds.announceOperational({
        cue: cue,
        voice: voice,
        eventVersion: Number(options.eventVersion || 0),
        eventKey: String(options.eventKey || "")
    });
}

function playDriverAssignmentAlert(eventVersion, assignmentKind, excavatorNumber, options) {
    options = options || {};
    var opKey = String(options.opKey || "");
    /* Заявка ставится до вибрации и до моста: иначе два синхронных вызова
       подряд успевают обратиться к проигрывателю оба. */
    if (opKey && !window.DriverVoiceGuard.claim(opKey)) {
        return;
    }
    if (navigator.vibrate) {
        try { navigator.vibrate([180, 90, 180]); } catch (error) {}
    }
    if (assignmentKind !== "assign") {
        announceDriverVoiceUnderClaim(opKey, function () {
            return playDriverVoice("truck_assigned", "voice_assignment_removed", {
                eventVersion: Number(eventVersion || 0),
                eventKey: "driver_assignment"
            });
        });
        return;
    }
    if (window.MobileOperationalSounds && typeof window.MobileOperationalSounds.announceEquipment === "function") {
        announceDriverVoiceUnderClaim(opKey, function () {
            return window.MobileOperationalSounds.announceEquipment({
                cue: "truck_assigned",
                action: "driver_excavator_assigned",
                equipmentNumber: String(excavatorNumber || ""),
                fallbackVoice: "voice_excavator_assigned",
                eventVersion: Number(eventVersion || 0),
                eventKey: "driver_assignment"
            });
        });
        return;
    }
    announceDriverVoiceUnderClaim(opKey, function () {
        return playDriverVoice("truck_assigned", "voice_excavator_assigned", {
            eventVersion: Number(eventVersion || 0),
            eventKey: "driver_assignment"
        });
    });
}

/* Предложение снять назначение только привлекает внимание коротким пиком.

   Голосовая фраза «Назначение снято» принадлежит событию «release_applied»:
   его одинаково видят и ранний обработчик, и нативный heartbeat, и оба
   передают одну и ту же версию события, поэтому фраза звучит ровно один раз.
   Произнести её здесь, на «release_pending», значило бы записать нативный
   маркер на меньшей версии — «release_applied» пришёл бы с большей и
   прозвучал бы вторым, с опозданием.

   Пик идёт мимо аннонсера, через обычный проигрыватель сигналов, и никакого
   маркера не пишет. Пространство ключа отдельное: ключ «assign:<номер>»
   принадлежит самому снятию, и пик не имеет права его закрывать. */
function playDriverReleaseOfferCue(assignmentId) {
    var opKey = assignmentId ? "release-cue:" + String(assignmentId) : "";
    if (opKey && !window.DriverVoiceGuard.claim(opKey)) {
        return;
    }
    if (navigator.vibrate) {
        try { navigator.vibrate([180, 90, 180]); } catch (error) {}
    }
    announceDriverVoiceUnderClaim(opKey, function () {
        return playDriverSound("assignment_removed_notice");
    });
}

function reportDriverAudioDiagnostic(stage, details, extra) {
    if (!document.body || document.body.dataset.nativeApp !== "true" || !window.fetch) return;
    var payload = {
        message: "driver_audio:" + String(stage || "unknown"),
        source: "driver-audio",
        stack: JSON.stringify({details: details || {}, extra: extra || {}}),
        screen: "driver",
        role: "driver",
        appVersion: document.body.dataset.appShellVersion || ""
    };
    try {
        window.fetch("/client-error/", {
            method: "POST",
            credentials: "same-origin",
            cache: "no-store",
            keepalive: true,
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(payload)
        }).catch(function () {});
    } catch (error) {}
}

/* Отдельной отметки о произнесённом здесь больше нет: она обновлялась только
   после возврата из моста и от синхронного двойного вызова не защищала.
   Владелец решения — DriverVoiceGuard, заявка по ключу рейса. */
function latestDriverDumpPointEvent(context) {
    var selected = null;
    var events = context && Array.isArray(context.events) ? context.events : [];
    events.forEach(function (event) {
        var payload = event && event.payload ? event.payload : null;
        var version = Number(event && event.version || 0);
        if (
            !payload
            || event.type !== "trip_changed"
            || payload.action !== "truck_loaded"
            || (selected && version <= selected.eventVersion)
        ) {
            return;
        }
        var tripId = Number(payload.trip_id || 0);
        var dumpPointId = Number(payload.assigned_dump_point_id || payload.dump_point_id || 0);
        var dumpPointName = String(payload.dump_point_name || "").trim();
        if (!tripId || (!dumpPointId && !dumpPointName)) return;
        selected = {
            eventVersion: version,
            tripId: tripId,
            dumpPointId: dumpPointId,
            dumpPointName: dumpPointName
        };
    });
    return selected;
}

function playDriverDumpPointAlert(context) {
    var details = latestDriverDumpPointEvent(context);
    if (!details) return;
    var opKey = "dump:" + String(details.tripId || "");
    if (!window.DriverVoiceGuard.claim(opKey)) {
        return;
    }
    reportDriverAudioDiagnostic("trigger", details, {
        capacitor: !!window.Capacitor,
        nativeSound: !!(window.Capacitor && window.Capacitor.Plugins && window.Capacitor.Plugins.NativeSound)
    });
    var sounds = window.MobileOperationalSounds;
    var announce;
    try {
        announce = sounds && typeof sounds.announceDumpPoint === "function"
            ? sounds.announceDumpPoint(details)
            : Promise.resolve({supported: false, announced: false});
    } catch (error) {
        /* Синхронный бросок моста не должен запирать рейс до перезагрузки. */
        window.DriverVoiceGuard.release(opKey);
        return;
    }
    /* Резервный сигнал тоже закрывает заявку: он уже прозвучал, и повторять
       его следующим путём нельзя. Освобождается заявка только тогда, когда не
       прозвучало ничего. */
    function settleWithFallback(nativeResult, withVibration) {
        if (withVibration && navigator.vibrate) {
            try { navigator.vibrate([180, 90, 180]); } catch (error) {}
        }
        return Promise.resolve(playDriverSound("truck_assigned")).then(function (played) {
            settleDriverVoiceClaim(opKey, played === true ? true : nativeResult);
        });
    }
    Promise.resolve(announce).then(function (nativeResult) {
        nativeResult = nativeResult || {supported: false, announced: false};
        reportDriverAudioDiagnostic("result", details, nativeResult);
        if (sounds && typeof sounds.diagnostics === "function") {
            window.setTimeout(function () {
                sounds.diagnostics().then(function (diagnostics) {
                    reportDriverAudioDiagnostic("playback", details, diagnostics);
                });
            }, 1600);
        }
        if (nativeResult.supported && nativeResult.announced) {
            settleDriverVoiceClaim(opKey, nativeResult);
            return;
        }
        if (nativeResult.supported && nativeResult.reason === "already_announced") {
            settleDriverVoiceClaim(opKey, nativeResult);
            return;
        }
        if (nativeResult.supported) {
            return settleWithFallback(nativeResult, false);
        }
        if (document.body && document.body.dataset.nativeApp === "true" && !nativeResult.supported) {
            return settleWithFallback(nativeResult, false);
        }
        return settleWithFallback(nativeResult, true);
    }).catch(function () {
        window.DriverVoiceGuard.release(opKey);
    });
}

window.addEventListener("operational-state-refresh-applied", function (event) {
    playDriverDumpPointAlert(event && event.detail ? event.detail : null);
});

function driverAppliedActionVoice(actionKind, freshShell) {
    if (actionKind === "shift-open") {
        return freshShell.querySelector("[data-driver-shift-close-button]")
            ? {cue: "shift_start", voice: "voice_shift_opened"}
            : {cue: "action_error", voice: "voice_action_failed"};
    }
    if (actionKind === "shift-close") {
        return freshShell.querySelector("[data-driver-shift-close-button]")
            ? {cue: "action_error", voice: "voice_action_failed"}
            : {cue: "shift_end", voice: "voice_shift_closed"};
    }
    if (actionKind === "complete-trip") {
        return {cue: "action_ok", voice: "voice_trip_finished"};
    }
    return {cue: "action_ok", voice: ""};
}
