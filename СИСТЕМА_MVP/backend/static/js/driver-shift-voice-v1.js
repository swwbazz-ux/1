/* Звук и голосовые события экрана водителя, а также признак «экран занят»
   (window.driverHasPendingWork), по которому обновление откладывается,
   чтобы не оборвать начатое водителем действие. Вынесено из driver-shift-v1.js без изменений. */
if (typeof window.bindAchievementPrizeUnlock === "function") {
    window.bindAchievementPrizeUnlock();
}

function isDriverOperationalRefreshUnsafe(shell) {
    /* Причина последнего отказа кладётся в window.driverRefreshBusyReason: на
       боевом телефоне её пишет в журнал driver-self-heal-v1.js. Сама функция
       остаётся булевой — её сигнатуру и селектор закрепляют тесты. */
    function busy(reason) {
        if (typeof window !== "undefined") window.driverRefreshBusyReason = reason;
        return true;
    }
    if (document.hidden) return busy("hidden");
    shell = shell || document.querySelector("[data-driver-shell]");
    if (!shell) return false;
    var active = document.activeElement;
    var activeTag = active && active.tagName ? active.tagName.toLowerCase() : "";
    if (
        active &&
        shell.contains(active) &&
        (active.isContentEditable || activeTag === "input" || activeTag === "textarea" || activeTag === "select")
    ) {
        return busy("focus:" + activeTag + (active.name ? ":" + active.name : ""));
    }
    /* Скрытая форма смены — не набор: водитель физически не может в неё
       вводить. На Infinix (v372, 30.09.2026) поздний input клавиатуры Android
       после местного открытия помечал уже скрытую форму открытия «в наборе», и
       каждое обновление с сервера откладывалось навсегда (busy=opening_form). */
    var openingShiftForm = shell.querySelector(".driver-shift-opening-form");
    if (openingShiftForm && !openingShiftForm.hidden && (
        openingShiftForm.dataset.driverShiftOpeningDirty === "true" ||
        openingShiftForm.dataset.driverShiftOpeningPending === "true" ||
        openingShiftForm.querySelector(".errorlist")
    )) {
        return busy("opening_form");
    }
    var activeShiftForm = shell.querySelector("[data-driver-shift-close-form]");
    if (activeShiftForm && !activeShiftForm.hidden && (
        activeShiftForm.dataset.driverShiftDirty === "true" ||
        activeShiftForm.querySelector(".errorlist")
    )) {
        return busy("close_form");
    }
    /* Палец прямо сейчас держит круг — кольцо разгрузки набирается (момент
       начала ставит unloadHoldGuard в driver-shift-v1.js). Подмена экрана
       посреди удержания снимает кнопку вместе с удержанием, и разгрузка молча
       не засчитывается (бой 03.10.2026, самосвал 22: связь «recovering»,
       самовосстановление открыло обход, удержания уходили впустую). Удержание
       длится полсекунды, поэтому это жёсткая причина; навсегда залипнуть не
       может — признак старше 5 с обновление не держит. */
    var dialHoldStartedAt = typeof window !== "undefined" ? Number(window.driverDialHoldStartedAt || 0) : 0;
    if (dialHoldStartedAt > 0 && Date.now() - dialHoldStartedAt < 5000) return busy("dial_hold");
    /* То же для пальца на барабане точек или простоев: подмена посреди
       протяжки снимает барабан под пальцем, и отправка точки в круг или
       подъём причины не засчитываются. Начало жеста отдают сами барабаны
       (driver-point-drum-v1.js, driver-downtime-drum-v1.js), 0 — пальца нет.
       Вращение бывает дольше удержания, поэтому срок 10 с. */
    function drumGestureStartedAt(name) {
        var get = typeof window !== "undefined" ? window[name] : null;
        return typeof get === "function" ? Number(get() || 0) : 0;
    }
    var drumGestureAt = Math.max(
        drumGestureStartedAt("driverPointDrumGestureStartedAt"),
        drumGestureStartedAt("driverDowntimeDrumGestureStartedAt")
    );
    if (drumGestureAt > 0 && Date.now() - drumGestureAt < 10000) return busy("drum_gesture");
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
        Number(window.driverOfflineBlockingCount || 0) > 0
    ) return busy("outbox:" + String(window.driverOfflineBlockingCount) + "/" + String(window.driverOfflinePendingCount || 0));
    var gestureNode = shell.querySelector(".is-touch-armed, .is-holding, .is-pending, .is-dragging, .is-lifting, .is-dropping, .is-snapping, .driver-drum-ghost, [data-driver-point-sheet]:not([hidden]), [data-driver-free-bucket-sheet]:not([hidden])")
        || document.querySelector("[data-driver-pwa-update-modal]:not([hidden]), .app-confirm-modal:not([hidden])");
    if (!gestureNode) {
        if (typeof window !== "undefined") window.driverRefreshBusyReason = "";
        return false;
    }
    var gestureTag = gestureNode.tagName ? String(gestureNode.tagName).toLowerCase() : "node";
    var gestureClasses = typeof gestureNode.className === "string"
        ? gestureNode.className.split(/\s+/).filter(function (name) { return /^is-|drum-ghost|sheet|modal/.test(name); }).join(".")
        : "";
    var gestureId = gestureNode.id ? "#" + gestureNode.id : "";
    return busy("gesture:" + gestureTag + gestureId + (gestureClasses ? "." + gestureClasses : ""));
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
    var waiters = Object.create(null);

    function forget(opKey) {
        delete states[opKey];
        var position = order.indexOf(opKey);
        if (position !== -1) order.splice(position, 1);
    }

    /* Ожидающие решения по уже идущей заявке. Нужны, когда одно событие видят
       два пути сразу (ранний сигнал и DOM-резерв): проигравший не должен
       отвечать «не подтверждено» — иначе общий курсор откладывает применение
       экрана до backoff, хотя операция вот-вот завершится (T-016, e2e-2 §2). */
    function answerWaiters(opKey, accepted) {
        var waiter = waiters[opKey];
        if (!waiter) return;
        delete waiters[opKey];
        waiter.resolve(accepted === true);
    }

    function claim(opKey) {
        if (!opKey) return false;
        if (states[opKey]) return false;
        states[opKey] = "claimed";
        order.push(opKey);
        while (order.length > LIMIT) {
            var dropped = order.shift();
            delete states[dropped];
            answerWaiters(dropped, false);
        }
        var resolver = null;
        var promise = new Promise(function (resolve) { resolver = resolve; });
        waiters[opKey] = {promise: promise, resolve: resolver};
        return true;
    }

    /* Заявка закрывается, когда звук фактически пошёл либо когда событием уже
       владеет другой канал. Освобождать можно только полную тишину, иначе
       следующий путь повторит то, что человек уже услышал. */
    function finalize(opKey) {
        if (opKey && states[opKey]) states[opKey] = "announced";
        if (opKey) answerWaiters(opKey, true);
    }

    function release(opKey) {
        if (opKey && states[opKey] === "claimed") forget(opKey);
        if (opKey) answerWaiters(opKey, false);
    }

    /* Операция завершилась без звука, но и без явного отказа: заявку отпускаем
       (следующая доставка попробует снова), а ждущим сообщаем решение «экран не
       держим». */
    function abandon(opKey, accepted) {
        if (opKey && states[opKey] === "claimed") forget(opKey);
        if (opKey) answerWaiters(opKey, accepted === true);
    }

    function wait(opKey) {
        if (!opKey) return Promise.resolve(false);
        if (states[opKey] === "announced") return Promise.resolve(true);
        var waiter = waiters[opKey];
        /* Заявки уже нет вовсе — следующая доставка попробует заново. */
        return waiter ? waiter.promise : Promise.resolve(false);
    }

    function state(opKey) {
        return (opKey && states[opKey]) || "";
    }

    return {
        claim: claim,
        finalize: finalize,
        release: release,
        abandon: abandon,
        wait: wait,
        state: state
    };
})();

/* Сроки ожидания голоса.

   Голосовой путь подтверждает общий курсор realtime (realtime-client.js): пока
   роль не ответила «принято», версия дельты не подтверждается. В браузере без
   нативного моста AudioContext может остаться «suspended» навсегда, мост —
   промолчать, и тогда экран замирал: клиент опрашивал состояние без конца
   (T-016, e2e §10.1). Поэтому у ожидания есть предел, а у звукового слоя —
   свои пределы (mobile-operational-sounds-v1.js, 1500 мс).

   Ответа нет — это не отказ: заявка освобождается (следующая доставка события
   попробует снова), но общий курсор таким событием не держим. Явный отказ
   устройства или тишина веб-фолбэка остаются поводом для повтора. */
var DRIVER_VOICE_DEADLINE_MS = 2500;
var DRIVER_VOICE_TIMEOUT_REASONS = {audio_timeout: true, bridge_timeout: true};

function driverVoiceTimedOut(result) {
    return !!(result && DRIVER_VOICE_TIMEOUT_REASONS[String(result.reason || "")]);
}

/* Операция без канала озвучки: нативного моста нет и веб-звук не сыграл.
   Держать из-за этого экран бессмысленно — звука всё равно не будет. */
function driverVoiceWithoutChannel(result) {
    return !!result && typeof result === "object" && result.supported === false && result.announced !== true;
}

function clearDriverVoiceTimer(timer) {
    if (typeof window !== "undefined" && typeof window.clearTimeout === "function") {
        window.clearTimeout(timer);
    }
}

/* Последний рубеж: даже если звуковой слой окажется старее этого файла и
   зависнет, заявка освобождается, а handoff разрешается. */
function withDriverVoiceDeadline(opKey, pending) {
    return new Promise(function (resolve) {
        var settled = false;
        var timer = window.setTimeout(function () {
            if (settled) return;
            settled = true;
            /* Решение то же, что у владельца заявки: экран не держим, попытка
               остаётся следующей доставке. Ожидающие получают тот же ответ. */
            window.DriverVoiceGuard.abandon(opKey, true);
            resolve(true);
        }, DRIVER_VOICE_DEADLINE_MS);
        Promise.resolve(pending).then(function (value) {
            if (settled) return;
            settled = true;
            clearDriverVoiceTimer(timer);
            resolve(value === true);
        }, function () {
            if (settled) return;
            settled = true;
            clearDriverVoiceTimer(timer);
            resolve(false);
        });
    });
}

function settleDriverVoiceClaim(opKey, result) {
    if (!opKey) return false;
    /* playDriverSound и резервный веб-звук отвечают простым признаком
       «сыграло», нативный мост — объектом. Прозвучавшее в любой форме
       закрывает заявку. */
    var announced = result === true || !!(result && result.announced === true);
    var ownedElsewhere = !!(result && String(result.reason || "") === "already_announced");
    if (announced || ownedElsewhere) {
        window.DriverVoiceGuard.finalize(opKey);
        return true;
    }
    /* Срок истёк или канала озвучки нет: заявка освобождается, следующая доставка
       попробует снова, но экран на этом не стоит. Ожидающие по этому ключу
       получают то же решение, а не ложный отказ. */
    var acceptedWithoutVoice = driverVoiceTimedOut(result) || driverVoiceWithoutChannel(result);
    window.DriverVoiceGuard.abandon(opKey, acceptedWithoutVoice);
    return acceptedWithoutVoice;
}

/* Единственная точка обращения к мосту под заявкой.

   Мост может не только вернуть отказ, но и бросить исключение — синхронно при
   вызове или отклонением промиса. Без освобождения заявка осталась бы в
   состоянии «принято» до перезагрузки страницы, и событие замолчало бы
   навсегда: ни резервный путь по DOM, ни повторный опрос его уже не подняли
   бы. Любой сбой возвращает операцию следующему источнику.

   Возвращает промис с решением об общем курсоре: «можно подтверждать» —
   операция прозвучала, уже озвучена другим каналом, ответа не было за срок или
   канала озвучки нет; «нельзя» — устройство явно отказало, и нужен повтор. */
function announceDriverVoiceUnderClaim(opKey, produceAnnouncement) {
    var pending;
    try {
        pending = produceAnnouncement();
    } catch (error) {
        window.DriverVoiceGuard.release(opKey);
        return Promise.resolve(false);
    }
    return withDriverVoiceDeadline(opKey, Promise.resolve(pending).then(function (result) {
        return settleDriverVoiceClaim(opKey, result);
    }, function () {
        window.DriverVoiceGuard.release(opKey);
        return false;
    }));
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
    var handoffs = [playDriverDumpPointAlert(context)];
    var assignment = latestDriverAssignmentEvent(context);
    if (assignment) {
        handoffs.push(playDriverAssignmentAlert(
            assignment.eventVersion,
            assignment.kind,
            assignment.excavatorNumber,
            {opKey: "assign:" + assignment.assignmentId}
        ));
    }
    /* Признак «принято» нужен общему курсору realtime: пока роль не подтвердила
       голосовую работу, версия дельты остаётся неподтверждённой (realtime-client.js).
       Каждый handoff ограничен сроком (withDriverVoiceDeadline), поэтому этот
       Promise.all разрешается всегда: зависший звук больше не держит экран. */
    return Promise.all(handoffs).then(function (accepted) {
        return {accepted: accepted.every(function (value) { return value === true; })};
    });
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
        eventKey: String(options.eventKey || ""),
        operationKey: String(options.operationKey || "")
    });
}

function playDriverAssignmentAlert(eventVersion, assignmentKind, excavatorNumber, options) {
    options = options || {};
    var opKey = String(options.opKey || "");
    /* Заявка ставится до вибрации и до моста: иначе два синхронных вызова
       подряд успевают обратиться к проигрывателю оба. */
    if (opKey && !window.DriverVoiceGuard.claim(opKey)) {
        /* Операцией уже владеет другой путь (ранний сигнал или DOM-резерв):
           ждём его решения, а не отвечаем ложным отказом — иначе общий курсор
           откладывает применение экрана до backoff (T-016, e2e-2 §2). */
        return window.DriverVoiceGuard.wait(opKey);
    }
    if (navigator.vibrate) {
        try { navigator.vibrate([180, 90, 180]); } catch (error) {}
    }
    if (assignmentKind !== "assign") {
        return announceDriverVoiceUnderClaim(opKey, function () {
            return playDriverVoice("truck_assigned", "voice_assignment_removed", {
                eventVersion: Number(eventVersion || 0),
                eventKey: "driver_assignment",
                operationKey: opKey
            });
        });
    }
    if (window.MobileOperationalSounds && typeof window.MobileOperationalSounds.announceEquipment === "function") {
        return announceDriverVoiceUnderClaim(opKey, function () {
            return window.MobileOperationalSounds.announceEquipment({
                cue: "truck_assigned",
                action: "driver_excavator_assigned",
                equipmentNumber: String(excavatorNumber || ""),
                fallbackVoice: "voice_excavator_assigned",
                eventVersion: Number(eventVersion || 0),
                eventKey: "driver_assignment",
                operationKey: opKey
            });
        });
    }
    return announceDriverVoiceUnderClaim(opKey, function () {
        return playDriverVoice("truck_assigned", "voice_excavator_assigned", {
            eventVersion: Number(eventVersion || 0),
            eventKey: "driver_assignment",
            operationKey: opKey
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
/* Самосвал этого экрана. Бой 03.10.2026 (самосвал 22): сервер присылает водителю
   события и экскаватора его назначения — в том числе погрузки ЧУЖИХ самосвалов
   этим экскаватором. Голос объявлял точку чужой машины («едем на ККД», когда своя
   шла на СКДР). Событие с номером другого самосвала не озвучивается; событие без
   номера (резервный путь по разметке своего экрана) — как раньше. */
function latestDriverDumpPointEvent(context) {
    var selected = null;
    var events = context && Array.isArray(context.events) ? context.events : [];
    var ownTruckId = 0;
    try {
        var ownShell = document.querySelector("[data-driver-shell]");
        ownTruckId = Number(ownShell && ownShell.dataset.driverCurrentTruckId || 0);
    } catch (error) {}
    events.forEach(function (event) {
        var payload = event && event.payload ? event.payload : null;
        var version = Number(event && event.version || 0);
        if (
            !payload
            || event.type !== "trip_changed"
            // Ручная погрузка (свайп точки в круг) озвучивается так же, как погрузка экскаваторщиком.
            || (payload.action !== "truck_loaded" && payload.action !== "driver_manual_loaded")
            || (selected && version <= selected.eventVersion)
        ) {
            return;
        }
        var eventTruckId = Number(payload.truck_id || 0);
        if (ownTruckId && eventTruckId && eventTruckId !== ownTruckId) return;
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
    if (!details) return Promise.resolve(true);

    /* Ключ заявки повторяет нативный, иначе межканальная дедупликация
       промахивается: серверная правда (core/voice_state.py) и нативная сверка
       (ConnectivityForegroundService, announceCurrentDriverTruth) принимают
       только `dump:<рейс>:id-<точка>`, а нативный проигрыватель для точки без
       идентификатора строит ключ по имени (DriverDumpPointAnnouncer).

       Ключ одного рейса на все его точки не годится: смена точки внутри того
       же рейса — новое рабочее событие, и водитель должен услышать её ещё раз.
       Ключ `id-0` тоже не годится: нативная сверка отклоняет такую операцию
       (invalidKnownOperation), то есть вводить заведомо отвергаемый ключ
       нельзя. Поэтому без идентификатора ключ опирается на имя точки. */
    function dumpPointOperationKey(opDetails) {
        var tripId = Number(opDetails.tripId || 0);
        if (!tripId) return "";
        var dumpPointId = Number(opDetails.dumpPointId || 0);
        if (dumpPointId > 0) return "dump:" + tripId + ":id-" + dumpPointId;
        var dumpPointName = String(opDetails.dumpPointName || "").trim().toLowerCase().replace(/\s+/g, "_");
        return dumpPointName ? "dump:" + tripId + ":name-" + dumpPointName : "";
    }

    var opKey = dumpPointOperationKey(details);
    /* Пустого ключа разбор события не даёт (latestDriverDumpPointEvent требует
       рейс и точку), но если он всё же появится, событие лучше озвучить без
       заявки, чем заглушить весь рейс одной отметкой. */
    if (opKey && !window.DriverVoiceGuard.claim(opKey)) {
        /* Событие уже озвучивает другой путь: ждём его решения, чтобы не
           отдавать ложный отказ и не отправлять курсор в backoff. */
        return window.DriverVoiceGuard.wait(opKey);
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
        return Promise.resolve(false);
    }
    /* Резервный сигнал тоже закрывает заявку: он уже прозвучал, и повторять
       его следующим путём нельзя. Освобождается заявка только тогда, когда не
       прозвучало ничего. */
    function settleWithFallback(nativeResult, withVibration) {
        if (withVibration && navigator.vibrate) {
            try { navigator.vibrate([180, 90, 180]); } catch (error) {}
        }
        return Promise.resolve(playDriverSound("truck_assigned")).then(function (played) {
            if (played === true) return settleDriverVoiceClaim(opKey, true);
            if (!nativeResult || nativeResult.supported !== true) {
                /* Ни моста, ни веб-звука: озвучивать нечем. Заявку освобождаем,
                   но экран не держим — иначе событие повиснет навсегда. */
                window.DriverVoiceGuard.abandon(opKey, true);
                return true;
            }
            return settleDriverVoiceClaim(opKey, nativeResult);
        });
    }
    return withDriverVoiceDeadline(opKey, Promise.resolve(announce).then(function (nativeResult) {
        nativeResult = nativeResult || {supported: false, announced: false};
        reportDriverAudioDiagnostic("result", details, nativeResult);
        if (sounds && typeof sounds.diagnostics === "function") {
            window.setTimeout(function () {
                sounds.diagnostics().then(function (diagnostics) {
                    reportDriverAudioDiagnostic("playback", details, diagnostics);
                });
            }, 1600);
        }
        if (driverVoiceTimedOut(nativeResult)) {
            /* Мост не ответил за срок: звук мог прозвучать, мог и нет. Ждать
               дальше и держать экран нельзя; заявку отпускаем, следующая
               доставка события попробует снова. */
            window.DriverVoiceGuard.abandon(opKey, true);
            return true;
        }
        if (nativeResult.announced) {
            return settleDriverVoiceClaim(opKey, nativeResult);
        }
        if (nativeResult.supported && nativeResult.reason === "already_announced") {
            return settleDriverVoiceClaim(opKey, nativeResult);
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
        return false;
    }));
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
