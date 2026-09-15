const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const template = fs.readFileSync(path.join(__dirname, '../../../templates/trips/excavator_work.html'), 'utf8');

function fixture(manual) {
    const handlers = {}, classes = new Set(), timers = new Map();
    let timerId = 0, vibrations = 0, pickupTones = 0, audioPrepares = 0, details = 0, sends = 0;
    const delays = [], haptics = [];
    const card = {
        disabled: false, style: {},
        dataset: {eoManualAvailable: manual ? '1' : '0', eoTruckInactive: manual ? '1' : '', eoCanLoad: manual ? '0' : '1'},
        classList: {add: (...names) => names.forEach(n => classes.add(n)), remove: (...names) => names.forEach(n => classes.delete(n)), contains: n => classes.has(n)},
        addEventListener: (name, fn) => { handlers[name] = fn; },
        setPointerCapture() {}, releasePointerCapture() {},
        getBoundingClientRect: () => ({left: 0, top: 0, width: 90, height: 90}),
    };
    const context = {
        activeTruckDrag: null, Math, card,
        shell: {querySelectorAll: () => [card]},
        window: {setTimeout: (fn, delay) => {delays.push(delay); timers.set(++timerId, fn); return timerId;}, clearTimeout: id => timers.delete(id)},
        navigator: {vibrate: duration => {haptics.push(duration); vibrations++;}},
        prepareExcavatorPickupAudio: () => {audioPrepares++;},
        playExcavatorTruckGrabFeedback: () => {haptics.push(70); vibrations++; pickupTones++;},
        isInactiveTruck: c => c.dataset.eoTruckInactive === '1',
        isTruckLoadBlocked: c => c.dataset.eoCanLoad === '0',
        selectTruck() {}, selectDump() {},
        openTruckDetailCard: () => {details++; return true;},
        createTruckDragPreview() {}, removeTruckDragPreview() {}, clearDropReady() {},
        postTruckLoaded: () => {sends++;}, updateTruckDrag() {},
    };
    vm.createContext(context);
    const canStart = template.indexOf('    function canTruckLoad(card)');
    vm.runInContext(template.slice(canStart, template.indexOf('    function generateClientActionId', canStart)), context);
    const finishStart = template.indexOf('    function finishTruckDrag(event)');
    vm.runInContext(template.slice(finishStart, template.indexOf('    shell.addEventListener("dragstart"', finishStart)), context);
    const start = template.indexOf('    shell.querySelectorAll("[data-eo-truck-card]").forEach(function (card)');
    vm.runInContext(template.slice(start, template.indexOf('    shell.querySelectorAll("[data-eo-dump-select]")', start)), context);
    function fire(name, extra = {}) {
        handlers[name]({pointerId: 1, button: 0, clientX: 10, clientY: 20, preventDefault() {}, stopPropagation() {}, ...extra});
    }
    function hold() {
        const current = [...timers.values()]; timers.clear(); current.forEach(fn => fn());
    }
    return {card, classes, context, fire, hold, delays, haptics, counts: () => ({vibrations, pickupTones, audioPrepares, details, sends})};
}

test('passive pickup needs hold, glows and vibrates without opening a modal', () => {
    const f = fixture(true);
    f.fire('pointerdown');
    assert.equal(f.delays[0], 290);
    assert.equal(f.context.canTruckLoad(f.card), false);
    f.hold();
    assert.equal(f.context.canTruckLoad(f.card), true);
    assert.equal(f.classes.has('is-picked-up'), true);
    assert.deepEqual(f.counts(), {vibrations: 1, pickupTones: 1, audioPrepares: 1, details: 0, sends: 0});
    assert.deepEqual(f.haptics, [70]);
});

test('release without sending restores passive icon without changing permissions', () => {
    const f = fixture(true);
    f.fire('pointerdown'); f.hold(); f.fire('pointerup');
    assert.equal(f.classes.has('is-picked-up'), false);
    assert.equal(f.context.canTruckLoad(f.card), false);
    assert.equal(f.card.dataset.eoCanLoad, '0');
    assert.equal(f.counts().sends, 0);
});

test('early release, movement and pointer cancellation cannot dispatch', () => {
    for (const action of ['pointerup', 'pointercancel', 'lostpointercapture', 'pointermove']) {
        const f = fixture(true);
        f.fire('pointerdown'); f.fire(action, {clientX: 70}); f.hold();
        assert.equal(f.classes.has('is-picked-up'), false, action);
        assert.equal(f.counts().sends, 0);
        assert.equal(f.counts().pickupTones, 0, action);
        assert.equal(f.counts().vibrations, 0, action);
    }
});

test('active icons respond immediately with no hold requirement', () => {
    const f = fixture(false);
    f.fire('pointerdown');
    assert.equal(f.classes.has('is-picked-up'), true);
    assert.equal(f.context.canTruckLoad(f.card), true);
    assert.equal(f.counts().pickupTones, 1);
    assert.equal(f.counts().vibrations, 1);
    f.fire('pointerup');
    assert.equal(f.classes.has('is-picked-up'), false);
});

test('pickup tone is local, rising and lasts about 170 ms without using the voice player', () => {
    const start = template.indexOf('function playExcavatorPickupTone()');
    const end = template.indexOf('function playExcavatorTruckGrabFeedback()', start);
    const source = template.slice(start, end);
    assert.match(source, /var duration = \.17;/);
    assert.match(source, /frequency\.setValueAtTime\(620/);
    assert.match(source, /frequency\.exponentialRampToValueAtTime\(1120/);
    assert.match(source, /context\.createOscillator\(\)/);
    assert.doesNotMatch(source, /fetch\(|new Audio\(|MobileOperationalSounds|playExcavatorVoice/);
});

test('held passive icon uses the existing dispatch once when dropped on destination', () => {
    const f = fixture(true);
    f.fire('pointerdown'); f.hold();
    f.context.activeTruckDrag.started = true;
    f.context.activeTruckDrag.target = {dataset: {eoDumpTarget: '2'}};
    f.fire('pointerup');
    assert.equal(f.counts().sends, 1);
    assert.equal(f.classes.has('is-picked-up'), false);
});

function effectsFixture(reducedMotion = false) {
    const frames = new Map(), nodes = [];
    let nextFrame = 0;
    const makeNode = () => ({
        style: {}, children: [], attributes: {}, setAttribute(key, value) { this.attributes[key] = String(value); },
        appendChild(node) { this.children.push(node); },
        remove() { nodes.splice(nodes.indexOf(this), 1); },
    });
    const context = {
        Math, String,
        window: {
            matchMedia: () => ({matches: reducedMotion}),
            requestAnimationFrame: fn => { frames.set(++nextFrame, fn); return nextFrame; },
            cancelAnimationFrame: id => frames.delete(id),
        },
        document: {createElement: makeNode, createElementNS: makeNode, body: {appendChild: node => nodes.push(node)}},
    };
    vm.createContext(context);
    const start = template.indexOf('    function moveTruckDragPreview(state, dx, dy)');
    const end = template.indexOf('    function findDumpTargetIntersectingPreview', start);
    vm.runInContext(template.slice(start, end), context);
    const preview = makeNode(); nodes.push(preview);
    const pickupFlare = makeNode(); nodes.push(pickupFlare);
    const state = {preview, pickupFlare, originRect: {left: 10, top: 20, width: 100, height: 100}};
    function tick(now) {
        const pending = [...frames.values()]; frames.clear(); pending.forEach(fn => fn(now));
    }
    return {context, state, frames, nodes, tick};
}

test('comet follows one continuous bounded path, fades and releases its RAF and DOM', () => {
    const f = effectsFixture();
    f.context.createTruckComet(f.state);
    const comet = f.state.comet;
    for (let n = 1; n <= 100; n++) {
        f.context.moveTruckDragPreview(f.state, n * 8, n * 4);
        f.tick(n * 16);
    }
    assert.ok(comet.points.length <= 48);
    const d = comet.paths[0].attributes.d;
    assert.equal((d.match(/M /g) || []).length, 1);
    assert.ok(comet.paths.every(p => p.attributes.d === d));
    let length = 0;
    for (let i = 1; i < comet.points.length; i++) length += Math.hypot(comet.points[i].x - comet.points[i-1].x, comet.points[i].y - comet.points[i-1].y);
    assert.ok(length <= 240.001);
    assert.equal(f.nodes.length, 3);
    assert.ok(Number(comet.layer.style.opacity) > 0);
    f.tick(2000);
    assert.ok(Number(comet.layer.style.opacity) > 0);
    f.tick(2400);
    assert.equal(Number(comet.layer.style.opacity), 0);
    f.context.removeTruckDragPreview(f.state);
    assert.equal(f.frames.size, 0);
    assert.equal(f.nodes.length, 0);
    assert.equal(f.state.comet, null);
    assert.equal(f.state.pickupFlare, null);
});

test('larger visual preview preserves the nominal drop hitbox', () => {
    const f = effectsFixture();
    f.context.moveTruckDragPreview(f.state, 30, 40);
    assert.match(f.state.preview.style.transform, /scale\(1\.18\)/);
    assert.deepEqual({...f.state.previewHitRect}, {left: 45, right: 135, top: 65, bottom: 155});
});

test('slow movement emits the tail outside the card instead of hiding it underneath', () => {
    const f = effectsFixture();
    f.context.createTruckComet(f.state);
    f.context.moveTruckDragPreview(f.state, 6, 0);
    f.tick(16);
    const tail = f.state.comet.points[0];
    const trailingEdge = 60 + 6 - 100 * 1.18 / 2;
    assert.ok(tail.x < trailingEdge);
    f.context.removeTruckDragPreview(f.state);
});

test('reduced motion keeps static feedback without a comet or pending animation', () => {
    const f = effectsFixture(true);
    f.context.createTruckComet(f.state);
    f.context.moveTruckDragPreview(f.state, 30, 40);
    assert.equal(f.state.comet, undefined);
    assert.equal(f.frames.size, 0);
    assert.match(f.state.preview.style.transform, /scale\(1\)/);
    f.context.removeTruckDragPreview(f.state);
    assert.equal(f.nodes.length, 0);
});

test('drag copy sheds blocking and transfer decoration while the real truck retains its state and plan', () => {
    const sourceClasses = ['eo-dashboard-truck-card', 'is-load-blocked', 'is-shift-pending', 'is-manual-passive', 'is-transfer-incoming', 'is-plan-overrun'];
    const cloneClasses = new Set(sourceClasses);
    const preview = {
        classList: {add: (...names) => names.forEach(n => cloneClasses.add(n)), remove: (...names) => names.forEach(n => cloneClasses.delete(n))},
        dataset: {eoTransferDirection: 'incoming', eoTransferCreated: 'start', eoTransferDeadline: 'end'},
        style: {setProperty() {}}, removeAttribute() {}, setAttribute() {}, appendChild() {},
        querySelectorAll: () => [{remove() {}}, {remove() {}}],
    };
    const card = {classes: sourceClasses, cloneNode: () => preview};
    const context = {
        window: {matchMedia: () => ({matches: true}), getComputedStyle: () => ({borderRadius: '12px'})},
        document: {body: {appendChild() {}}, createElement: () => ({})},
        moveTruckDragPreview() {},
    };
    vm.createContext(context);
    const start = template.indexOf('    function createTruckDragPreview(state)');
    vm.runInContext(template.slice(start, template.indexOf('    function moveTruckDragPreview', start)), context);
    context.createTruckDragPreview({card, originRect: {left: 0, top: 0, width: 100, height: 100}});
    assert.ok(card.classes.includes('is-load-blocked'));
    assert.ok(card.classes.includes('is-shift-pending'));
    assert.ok(cloneClasses.has('is-plan-overrun'));
    assert.ok(cloneClasses.has('is-drag-preview'));
    assert.equal(cloneClasses.has('is-load-blocked'), false);
    assert.equal(cloneClasses.has('is-shift-pending'), false);
    assert.equal(cloneClasses.has('is-transfer-incoming'), false);
    assert.equal(preview.dataset.eoTransferDirection, undefined);
});

test('transfer countdown uses server time and never grants rights at local zero', () => {
    const start = template.indexOf('function bindExcavatorTransferCountdowns(shell)');
    const end = template.indexOf('window.excavatorHasPendingWork', start);
    assert.ok(start >= 0 && end > start, 'The countdown source must have explicit valid bounds');
    const source = template.slice(start, end);
    assert.match(source, /shell\.dataset\.eoServerNow/);
    assert.match(source, /data-eo-transfer-deadline|eoTransferDeadline/);
    assert.match(source, /haul_transfer_deadline/);
    assert.match(source, /refreshExcavatorWorkFromServer/);
    assert.match(source, /Ожидает подтверждения/);
    assert.match(source, /is-transfer-awaiting-confirmation/);
    assert.match(source, /eoTransferDeadlineRefreshInFlight/);
    assert.match(source, /eoTransferDeadlineRefreshRetryAt/);
    assert.match(source, /eoTransferDeadlineRefreshFailures/);
    assert.match(source, /Math\.min\(\s*30000/);
    assert.doesNotMatch(source, /eoTransferLastWake/);
    assert.doesNotMatch(source, /dataset\.eoCanLoad\s*=/);
    assert.doesNotMatch(source, /\.remove\(\)/);
});

test('fragment refresh rejects older versions and late earlier requests', () => {
    const start = template.indexOf('var excavatorWorkMutationGeneration = 0;');
    const end = template.indexOf('window.refreshExcavatorWorkFromServer', start);
    const source = template.slice(start, end);
    assert.match(source, /excavatorWorkRefreshRequestGeneration/);
    assert.match(source, /excavatorWorkAppliedRequestGeneration/);
    assert.match(source, /payloadVersion < appliedVersion/);
    assert.match(source, /requestGeneration < excavatorWorkAppliedRequestGeneration/);
    assert.match(source, /storeExcavatorRealtimeVersion\(payloadVersion\)/);
    assert.match(source, /Number\(document\.body\.dataset\.operationalStateVersion \|\| 0\)/);
    assert.match(source, /window\.eoExcavatorWorkRefreshPromise/);
    assert.match(source, /return window\.eoExcavatorWorkRefreshPromise/);
    assert.match(source, /window\.eoExcavatorWorkRefreshPromise = null/);
    assert.match(source, /eoExcavatorWorkRefreshMinimumVersion/);
    assert.match(source, /eoExcavatorWorkQueuedMinimumVersion/);
    assert.match(source, /eoExcavatorWorkQueuedRefreshPromise/);
    assert.match(source, /eoExcavatorWorkRefreshMinimumVersion = -1/);
});

test('mutation queues a higher-version fragment behind an invalidated in-flight request', async () => {
    const start = template.indexOf('var excavatorWorkMutationGeneration = 0;');
    const end = template.indexOf('function bindExcavatorTransferCountdowns', start);
    const requests = [];
    let currentShell = {
        dataset: {eoActiveTab: 'trucks'},
        replaceWith(next) { currentShell = next; },
    };
    const context = {
        window: {},
        document: {
            body: {dataset: {operationalStateVersion: '10'}},
            querySelector: selector => selector === '[data-eo-shell]' ? currentShell : null,
            getElementById: () => null,
        },
        isExcavatorRefreshUnsafe: () => false,
        readExcavatorAssignmentSnapshot: () => [],
        storeExcavatorRealtimeVersion(version) {
            context.document.body.dataset.operationalStateVersion = String(version);
        },
        syncExcavatorAssignmentSnapshot() {},
        storeExcavatorAssignments() {},
        scheduleExcavatorViewportHeightSync() {},
        Promise,
        Number,
        Math,
        Object,
    };
    context.window = context;
    context.AppOperationalFragment = {
        request(name, minimumVersion) {
            let resolve;
            const promise = new Promise(done => { resolve = done; });
            requests.push({name, minimumVersion, resolve});
            return promise;
        },
        parseRoot: () => ({dataset: {eoActiveTab: 'trucks'}}),
    };
    vm.createContext(context);
    vm.runInContext(template.slice(start, end), context);

    const first = context.refreshExcavatorWorkFromServer({version: 10});
    assert.equal(requests.length, 1);
    context.invalidateExcavatorWorkRefresh();
    const second = context.refreshExcavatorWorkFromServer({version: 11});
    assert.equal(requests.length, 1);
    requests[0].resolve({version: 10, html: '<section></section>'});
    assert.equal(await first, false);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(requests.length, 2);
    assert.equal(requests[1].minimumVersion, 11);
    requests[1].resolve({version: 11, html: '<section></section>'});
    assert.equal(await second, true);
    assert.equal(context.document.body.dataset.operationalStateVersion, '11');
});

test('truck loaded outbox event carries exact assignment state and immutable context', () => {
    const start = template.indexOf('    function postTruckLoaded(card, dumpTarget)');
    const end = template.indexOf('    function clearDropReady()', start);
    const source = template.slice(start, end);
    assert.match(source, /assignment_id:\s*card\.dataset\.assignmentId/);
    assert.match(source, /event_type:\s*"excavator\.trip\.loaded"/);
    assert.match(source, /format_version:\s*1/);
    assert.match(source, /occurred_at:\s*new Date\(\)\.toISOString\(\)/);
    assert.match(source, /local_trip_id:\s*localTripId/);
    assert.match(source, /depends_on:\s*previous \? \[previous\.event_id\] : \[\]/);
    assert.match(source, /fieldOutbox\.queue\(event\)\.then/);
    assert.match(source, /fieldOutbox\.allocateSequence\(legacyExcavatorFieldSequence/);
    assert.match(source, /rock_type_id:/);
    assert.match(source, /invalidateExcavatorWorkRefresh\(\)/);
    assert.doesNotMatch(template, /nextExcavatorFieldSequence/);
});

test('successful outgoing load removes only the source card and keeps exact trip badge', () => {
    const confirmStart = template.indexOf('    function confirmTruckLoaded(card, payload)');
    const confirmEnd = template.indexOf('    function restoreTruckCard', confirmStart);
    const confirmSource = template.slice(confirmStart, confirmEnd);
    assert.match(confirmSource, /eoTransferDirection === "outgoing"/);
    assert.match(confirmSource, /card\.remove\(\)/);

    const badgeStart = template.indexOf('    function removePendingTruckBadge');
    const badgeEnd = template.indexOf('    function updatePendingTruckBadgeTrip', badgeStart);
    const badgeSource = template.slice(badgeStart, badgeEnd);
    assert.match(badgeSource, /tripId \? sameTrip : sameTruck/);
    assert.doesNotMatch(badgeSource, /sameTruck \|\| sameTrip/);
    assert.match(template, /data-eo-transfer-preview-deadline/);
});

test('manual dump preview expiry is trip-specific and independent from transfer countdown', () => {
    const start = template.indexOf('    function bindManualDumpCardExpiry(shellRoot)');
    const end = template.indexOf('    function restoreTruckAfterLoadedCancel', start);
    const source = template.slice(start, end);
    assert.match(source, /window\.eoManualQueuePreviewTimer/);
    assert.match(source, /shellRoot\.dataset\.eoServerNow/);
    assert.match(source, /entry\.badge\.isConnected/);
    assert.match(source, /String\(badge\.dataset\.tripId \|\| ""\) !== entry\.tripId/);
    assert.doesNotMatch(source, /removePendingTruckBadge\(/);
    assert.match(template, /bindExcavatorTransferCountdowns\(shell\);/);
    assert.match(template, /bindManualDumpCardExpiry\(shell\);/);
});

test('offline review records do not block authoritative refresh forever', () => {
    assert.match(template, /eoHasPendingFieldEvents = summary\.pending > 0/);
    const guardStart = template.indexOf('function isExcavatorRefreshUnsafe(options)');
    const guardEnd = template.indexOf('function invalidateExcavatorWorkRefresh()', guardStart);
    const guardSource = template.slice(guardStart, guardEnd);
    assert.doesNotMatch(guardSource, /is-saved-on-device/);
    assert.doesNotMatch(guardSource, /data-eo-offline-event-id/);
    const start = template.indexOf('    window.applyExcavatorPendingFieldEvents = function (events)');
    const end = template.indexOf('    window.confirmExcavatorFieldEvent', start);
    const source = template.slice(start, end);
    assert.match(source, /\["pending", "syncing"\]\.indexOf\(event\.sync_state\)/);
    assert.doesNotMatch(source, /\["pending", "syncing", "conflict"/);
    const attentionStart = template.indexOf('    window.attendExcavatorFieldEvent');
    const attentionEnd = template.indexOf('    if (fieldOutbox)', attentionStart);
    assert.match(template.slice(attentionStart, attentionEnd), /refreshExcavatorWorkFromServer\(\{preserveTab: true\}\)/);
});

test('downtime end keeps the active server id and depends on a local start', () => {
    const start = template.indexOf('    function postDowntimeAction(payload)');
    const end = template.indexOf('    var downtimeStatusSyncGeneration', start);
    const source = template.slice(start, end);
    assert.match(source, /serverDowntimeId = \/\^\\d\+\$\//);
    assert.match(source, /downtime_id:\s*serverDowntimeId/);
    assert.match(source, /local_downtime_id:\s*localDowntimeId \|\| null/);
    assert.match(source, /localDowntimeIdFromEvent:\s*action !== "close"/);
    assert.match(source, /dependencyTypes:\s*\["excavator\.downtime\.started", "excavator\.downtime\.ended"\]/);
    assert.match(template, /local_downtime_id:\s*options\.localDowntimeIdFromEvent[\s\S]*?\? eventId/);
    const confirmStart = template.indexOf('    window.confirmExcavatorFieldEvent');
    const confirmEnd = template.indexOf('    window.attendExcavatorFieldEvent', confirmStart);
    const confirmSource = template.slice(confirmStart, confirmEnd);
    assert.match(confirmSource, /result\.server_ids\.downtime_event_id/);
    assert.match(confirmSource, /downtimeCard\.dataset\.eoActiveDowntimeId = String\(downtimeId\)/);
});

test('confirmed server trip cancellation also uses the durable outbox', () => {
    const start = template.indexOf('    function cancelLoadedTripFromQueue(item, options)');
    const end = template.indexOf('    function returnLastTruckFromDump', start);
    const source = template.slice(start, end);
    assert.match(source, /item\.dataset\.eoOfflineEventId \|\| item\.dataset\.tripId/);
    assert.match(source, /queueExcavatorFieldEvent\("excavator\.trip\.loaded\.cancelled"/);
    assert.match(source, /tripId:\s*Number\(item\.dataset\.tripId/);
});

test('confirmed load keeps the server trip id for the next offline load', () => {
    const start = template.indexOf('    window.confirmExcavatorFieldEvent = function (event, result)');
    const end = template.indexOf('    window.attendExcavatorFieldEvent', start);
    const source = template.slice(start, end);
    assert.match(source, /cardByTruckId\(event\.payload && event\.payload\.truck_id\)/);
    assert.match(source, /confirmedCard\.dataset\.eoOpenTripId = String\(tripId\)/);
    assert.match(source, /delete confirmedCard\.dataset\.eoLocalOpenTripId/);
});

test('offline restart restores a valid tab from URL or access-scoped storage', () => {
    const start = template.indexOf('    var excavatorTabPreferenceKey');
    const end = template.indexOf('    /* EXCAVATOR_TAB_SETTLE_START */', start);
    const source = template.slice(start, end);
    assert.match(source, /"excavator-active-tab-v1:"/);
    assert.match(source, /shell\.dataset\.eoAccessId/);
    assert.match(source, /searchParams\.get\("tab"\)/);
    assert.match(source, /localStorage\.getItem\(excavatorTabPreferenceKey\)/);
    assert.match(source, /localStorage\.setItem\(excavatorTabPreferenceKey, name\)/);
    assert.match(template, /activateTab\(restoredExcavatorTab\(shell\.dataset\.eoActiveTab \|\| "trucks"\)\)/);
});

test('fragment replacement rebinds outbox dispatch to the new shell without duplicate lifecycle listeners', async () => {
    const start = template.indexOf('function ensureExcavatorFieldOutbox(shell)');
    const end = template.indexOf('window.initExcavatorWorkShell = function ()', start);
    const source = template.slice(start, end);
    const oldShell = {
        name: 'old',
        dataset: {eoAccessId: '7', eoEmployeeId: '17'},
        querySelector: () => ({value: 'old-token'}),
    };
    const newShell = {
        name: 'new',
        dataset: {eoAccessId: '7', eoEmployeeId: '17'},
        querySelector: () => ({value: 'new-token'}),
    };
    let currentShell = oldShell;
    let factoryCalls = 0;
    let windowListenerCount = 0;
    let documentListenerCount = 0;
    let callbacks;
    const outbox = {
        ready: () => Promise.resolve([]),
        flush: () => Promise.resolve([]),
        retryNow: () => Promise.resolve([]),
        pending: () => Promise.resolve([]),
    };
    const context = {
        navigator: {onLine: false},
        excavatorInstallDeviceId: () => 'device-1',
        renderExcavatorOfflineStatus() {},
        document: {
            hidden: false,
            querySelector: selector => selector === '[data-eo-shell]' ? currentShell : null,
            addEventListener: () => { documentListenerCount += 1; },
        },
        window: {
            indexedDB: {},
            localStorage: {},
            createExcavatorFieldOutbox: options => {
                factoryCalls += 1;
                callbacks = options;
                return outbox;
            },
            addEventListener: () => { windowListenerCount += 1; },
        },
    };
    vm.createContext(context);
    vm.runInContext(source, context);
    assert.equal(context.ensureExcavatorFieldOutbox(oldShell), outbox);
    const oldApplications = [];
    context.window.applyExcavatorPendingFieldEvents = events => oldApplications.push(events);

    currentShell = newShell;
    const newApplications = [];
    const confirmations = [];
    context.window.applyExcavatorPendingFieldEvents = events => newApplications.push(events);
    context.window.confirmExcavatorFieldEvent = event => confirmations.push(event.event_type);
    assert.equal(context.ensureExcavatorFieldOutbox(newShell), outbox);
    callbacks.onChange(
        {total: 2, pending: 2, syncing: 0, attention: 0},
        [{event_type: 'excavator.trip.loaded'}, {event_type: 'excavator.downtime.started'}],
        {}
    );
    await callbacks.onConfirmed({event_type: 'excavator.trip.loaded'}, {status: 'accepted'});

    assert.equal(factoryCalls, 1);
    assert.equal(windowListenerCount, 3);
    assert.equal(documentListenerCount, 1);
    assert.deepEqual(oldApplications, []);
    assert.deepEqual(newApplications[0].map(event => event.event_type), [
        'excavator.trip.loaded',
        'excavator.downtime.started',
    ]);
    assert.deepEqual(confirmations, ['excavator.trip.loaded']);
});
