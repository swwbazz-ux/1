const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {
    acceptanceExpiresAt,
    confirmedAcceptanceIsAbsent,
    confirmedCancellationIsAbsent,
    itemCanBeAccepted,
    pruneExpiredCards,
    reconcileConfirmed,
    restoreLoadedCancellation,
    snapshotContainsAcceptance,
    scheduleExpiryTimer,
} = require('../excavator-free-bucket-v1.js');
const {dispatcherScreenSource} = require('./dispatcher-screen-source');

const backend = path.resolve(__dirname, '..', '..', '..');
const template = [
    'templates/trips/excavator_work.html',
    'templates/includes/excavator_dashboard_workspace.html',
    'templates/includes/excavator_dashboard_source_card.html',
    'templates/includes/excavator_dashboard_dump_card.html',
].map(file => fs.readFileSync(path.join(backend, file), 'utf8')).join('\n');
const source = fs.readFileSync(path.join(backend, 'static', 'js', 'excavator-free-bucket-v1.js'), 'utf8');
const dispatcherSource = fs.readFileSync(path.join(backend, 'static', 'js', 'dispatcher-control-v1.js'), 'utf8');
const dispatcherTemplate = dispatcherScreenSource();
const css = fs.readFileSync(path.join(backend, 'static', 'css', 'excavator-free-bucket-v1.css'), 'utf8');

test('bucket entry stays beside the existing hourly report ring', () => {
    const widget = template.match(/<div class="eo-dashboard-plan-widget">([\s\S]*?)<\/div>\s*<\/div>/);
    assert.ok(widget);
    assert.ok(widget[1].indexOf('data-eo-free-bucket-open') < widget[1].indexOf('data-eo-hourly-report-open'));
    assert.equal((template.match(/data-eo-hourly-report-open/g) || []).length, 1);
    assert.match(template, /<button class="eo-free-bucket-button"[\s\S]*?data-eo-free-bucket-open[\s\S]*?aria-label="Свободный ковш"/);
    assert.match(css, /grid-template-columns:\s*46px auto/);
    assert.match(css, /\.eo-free-bucket-button[\s\S]*min-width:\s*46px[\s\S]*min-height:\s*46px/);
});

test('selector modal is external, keypad driven and Android Back aware', () => {
    assert.ok(template.indexOf('data-eo-free-bucket-modal') > template.indexOf('</main>'));
    assert.match(template, /inputmode="none"[^>]+readonly/);
    assert.equal((template.match(/data-eo-free-bucket-key="\d"/g) || []).length, 10);
    assert.match(source, /history\.pushState/);
    assert.match(source, /window\.addEventListener\("popstate"/);
    assert.match(source, /setAttribute\("inert", ""\)/);
    const unsafeStart = template.indexOf('function isExcavatorRefreshUnsafe(options) {');
    const unsafeEnd = template.indexOf('function storeExcavatorRealtimeVersion', unsafeStart);
    assert.ok(unsafeStart >= 0 && unsafeEnd > unsafeStart);
    assert.doesNotMatch(template.slice(unsafeStart, unsafeEnd), /data-eo-free-bucket-modal/);
    assert.match(source, /operational-state-refresh-applied[\s\S]*setUnderlyingBlocked\(true\)/);
});

test('catalog and accepted cards survive offline shell lifecycle', () => {
    assert.match(template, /free_bucket_truck_directory\|json_script:"eo-free-bucket-directory-data"/);
    assert.match(template, /free_bucket_cards\|json_script:"eo-free-bucket-cards-data"/);
    assert.match(source, /indexedDB\.open\(DB_NAME, DB_VERSION\)/);
    assert.match(source, /excavator-free-bucket-catalog-v1:/);
    assert.match(source, /operational-state-refresh-applied/);
    assert.match(source, /fieldOutbox\.pending\(\)\.then\(reconcileEvents\)/);
    assert.match(source, /renderEmbeddedCards\(shell\)/);
    assert.ok(source.indexOf('renderEmbeddedCards(shell);') < source.indexOf('hydrateCatalog(shell);'));
    assert.match(source, /value\.replace\(\/\\D\+\/g, ""\)\.indexOf\(digits\)/);
    assert.match(source, /catalogMeta\.updated_at/);
    assert.match(source, /availability_label/);
    assert.match(template, /payload\.free_bucket_truck_directory/);
    assert.match(template, /payload\.free_bucket_cards/);
    assert.match(template, /node\.type = 'application\/json'/);
    assert.match(source, /var selectedTruckId = truckIdOf\(selectedTruck\)/);
    assert.match(source, /catalog\.find\(function \(item\) \{ return truckIdOf\(item\) === selectedTruckId; \}\)/);
    assert.match(source, /if \(embedded\) \{[\s\S]*localStorageWrite\(scope, embedded\)/);
    assert.doesNotMatch(source, /embedded && embedded\.trucks\.length/);
    assert.match(source, /fieldOutbox\.confirmed\(\)/);
    assert.match(source, /confirmed\.then\(function \(records\) \{ return reconcileConfirmed\(records, snapshot\); \}\)/);
});

test('ten-minute reservation deadline uses the correct local or server clock basis', () => {
    const serverEpoch = Date.parse('2026-09-27T00:00:00Z');
    const clientEpoch = serverEpoch + 20 * 60 * 1000;
    const clock = {serverEpoch, clientCapturedAt: clientEpoch};
    const serverDeadline = new Date(serverEpoch + 10 * 60 * 1000).toISOString();
    assert.equal(
        acceptanceExpiresAt({free_bucket_expires_at: serverDeadline}, null, clock),
        clientEpoch + 10 * 60 * 1000,
    );
    assert.equal(
        acceptanceExpiresAt(
            {free_bucket_expires_at: serverDeadline},
            {occurred_at: new Date(clientEpoch + 9 * 60 * 1000).toISOString()},
            clock,
        ),
        clientEpoch + 10 * 60 * 1000,
        'accepting on minute nine must not extend the original reservation deadline',
    );

    const staleDeadline = new Date(serverEpoch + 10 * 60 * 1000).toISOString();
    const freshTap = new Date(clientEpoch + 11 * 60 * 1000).toISOString();
    assert.equal(
        acceptanceExpiresAt(
            {free_bucket_expires_at: staleDeadline},
            {occurred_at: freshTap},
            clock,
        ),
        Date.parse(freshTap) + 10 * 60 * 1000,
        'an expired catalog cycle must not instantly expire a new local acceptance',
    );

    const localTap = new Date(clientEpoch).toISOString();
    assert.equal(
        acceptanceExpiresAt({}, {occurred_at: localTap}, clock),
        clientEpoch + 10 * 60 * 1000,
    );
    assert.equal(acceptanceExpiresAt({}, {}, clock), 0);

    const reserved = {is_active: true, can_accept_free_bucket: false, free_bucket_expires_at: serverDeadline};
    assert.equal(itemCanBeAccepted(reserved, clientEpoch + 10 * 60 * 1000 - 1, clock), false);
    assert.equal(itemCanBeAccepted(reserved, clientEpoch + 10 * 60 * 1000, clock), true);
    assert.equal(itemCanBeAccepted({...reserved, is_active: false}, clientEpoch + 20 * 60 * 1000, clock), false);
});

test('expiry cleanup removes the boundary card and scheduler chooses the nearest future deadline', () => {
    const previousDocument = global.document;
    const previousSetTimeout = global.setTimeout;
    const previousClearTimeout = global.clearTimeout;
    const now = Date.now();
    const cards = [
        {dataset: {eoFreeBucketExpiresAt: String(now)}, removed: false, remove() { this.removed = true; }},
        {dataset: {eoFreeBucketExpiresAt: String(now + 1200)}, removed: false, remove() { this.removed = true; }},
    ];
    let scheduledDelay = null;
    global.document = {
        querySelectorAll() { return cards.filter(card => !card.removed); },
        querySelector() { return null; },
    };
    global.setTimeout = (_callback, delay) => {
        scheduledDelay = delay;
        return {unref() {}};
    };
    global.clearTimeout = () => {};
    try {
        assert.equal(pruneExpiredCards(now), true);
        assert.equal(cards[0].removed, true);
        assert.equal(cards[1].removed, false);
        scheduleExpiryTimer();
        assert.ok(scheduledDelay >= 1 && scheduledDelay <= 1300, scheduledDelay);
    } finally {
        if (previousDocument === undefined) delete global.document;
        else global.document = previousDocument;
        global.setTimeout = previousSetTimeout;
        global.clearTimeout = previousClearTimeout;
    }
});

test('expired offline acceptance cannot be resurrected after restart or resume', () => {
    assert.match(source, /if \(expiresAt && Date\.now\(\) >= expiresAt\) \{/);
    assert.match(source, /reconcileDurableState\(snapshot\)\.then\(function \(\) \{[\s\S]*?pruneExpiredCards\(\);[\s\S]*?scheduleExpiryTimer\(\)/);
    assert.match(source, /document\.addEventListener\("visibilitychange", resumeExpiryClock\)/);
    assert.match(source, /document\.addEventListener\("resume", resumeExpiryClock\)/);
    assert.match(source, /window\.addEventListener\("pageshow", resumeExpiryClock\)/);
    assert.match(source, /captureServerClock\(shell\)/);
});

test('accept cancel and load use the durable field outbox', () => {
    assert.match(source, /queueEvent\("excavator\.free_bucket\.accepted"/);
    assert.match(source, /queueEvent\("excavator\.free_bucket\.cancelled"/);
    assert.match(template, /event_type: isFreeBucketLoad \? "excavator\.free_bucket\.loaded"/);
    assert.match(template, /freeBucketAcceptanceReference = freeBucketAcceptanceId \|\| freeBucketAcceptanceLocalId/);
    assert.match(template, /free_bucket_acceptance_id: freeBucketAcceptanceId/);
    assert.match(template, /free_bucket_acceptance_local_id: freeBucketAcceptanceId \? "" : freeBucketAcceptanceLocalId/);
    assert.match(template, /freeBucketAcceptanceId \? "" : freeBucketAcceptanceLocalId/);
    assert.match(template, /manual_control: card\.dataset\.eoManualAvailable === "1"/);
    assert.doesNotMatch(template, /manual_control: isFreeBucketLoad \|\|/);
    assert.match(template, /dump_points_snapshot: Array\.prototype\.map\.call/);
    assert.match(template, /function bindExcavatorTruckCard\(card\)/);
    assert.match(template, /bindTruckCard: bindExcavatorTruckCard/);
    assert.match(template, /freeBucketController\.markLoaded\(card\)/);
});

test('cancel and failed load do not restore an actionable stale card', () => {
    assert.match(source, /var card = reference \? cardForAcceptance\(reference\) : cardForTruck\(payload\.truck_id\)/);
    assert.match(source, /var current = cardForAcceptance\(reference\)/);
    assert.match(source, /var cancelled = cancelledReference \? cardForAcceptance\(cancelledReference\) : cardForTruck\(payload\.truck_id\)/);
    assert.doesNotMatch(source, /cardForAcceptance\([^\n]+\)\s*\|\|\s*cardForTruck/);
    assert.match(source, /if \(\["pending", "syncing"\]\.indexOf\(event\.sync_state\) >= 0\) card\.remove\(\)/);
    assert.match(source, /event\.event_type !== "excavator\.free_bucket\.loaded"/);
    assert.match(source, /if \(terminalAttention\(event\)\) markAttention\(event, \{\}\)/);
    assert.match(source, /function removeLoadedCard\(card\)[\s\S]*card\.remove\(\)/);
    assert.match(source, /if \(item\.is_used \|\| itemWasConsumed\(item\)\) return/);
    assert.match(source, /loadedReference \? cardForAcceptance\(loadedReference\) : cardForTruck/);
    assert.match(source, /eoFreeBucketAcceptanceId\) === reference/);
});

test('a rejected repeat acceptance is removed instead of becoming a dead card', () => {
    assert.match(source, /function rejectedAcceptance\(event, result\)/);
    assert.match(source, /\["conflict", "invalid"\]\.indexOf\(status\)/);
    assert.match(source, /if \(rejectedAcceptance\(event\)\) \{[\s\S]*cardForAcceptance\(event\.event_id\)[\s\S]*rejectedCard\.remove\(\)/);
    assert.match(source, /if \(rejectedAcceptance\(event, result\)\) \{[\s\S]*cardForAcceptance\(event\.event_id\)[\s\S]*renderSearch\(\)/);
    assert.match(source, /function itemCanBeAccepted\(item, nowMs, clock\)/);
    assert.match(source, /item\.can_accept_free_bucket !== false/);
});

test('cancelling a temporary load restores the same card until its original deadline', () => {
    const snapshot = {
        cards: [{id: 17, client_acceptance_id: 'free-accept-local'}],
    };
    assert.equal(snapshotContainsAcceptance(snapshot, {
        free_bucket_acceptance_id: 17,
    }), true);
    assert.equal(snapshotContainsAcceptance(snapshot, {
        free_bucket_acceptance_local_id: 'free-accept-local',
    }), true);
    assert.equal(snapshotContainsAcceptance(snapshot, {
        free_bucket_acceptance_id: 99,
    }), false);

    assert.match(source, /function restoreLoadedCancellation\(info\)/);
    assert.match(source, /storeConsumedReferences\(references, false\)/);
    assert.match(source, /deadline - FREE_BUCKET_REQUEST_TTL_MS/);
    assert.match(source, /event\.event_type === "excavator\.trip\.loaded\.cancelled"[\s\S]*restoreLoadedCancellation/);
    assert.match(source, /snapshotContainsAcceptance\(snapshot, payload\)[\s\S]*storeConsumedReferences\(eventAcceptanceReferences\(payload\), false\)/);

    const embeddedStart = source.indexOf('function renderEmbeddedCards(currentShell)');
    const embeddedEnd = source.indexOf('function handleConfirmed(event, result)', embeddedStart);
    const embeddedSource = source.slice(embeddedStart, embeddedEnd);
    assert.ok(embeddedSource.indexOf('storeConsumedReferences([') < embeddedSource.indexOf('itemWasConsumed(item)'));

    assert.match(template, /fieldOutbox\.pending\(\)\.then\(function \(events\)[\s\S]*fieldOutbox\.discardUnsent\(loadEventId\)/);
    assert.match(template, /free_bucket_reservation_expires_at_ms/);
    assert.match(template, /occurred_at: cancelOccurredAt/);
    assert.match(template, /freeBucketController\.restoreLoadedCancellation/);
    assert.match(template, /data-eo-free-bucket-reservation-expires-at/);
});

test('failed temporary-load restore retires only the matching old acceptance', () => {
    const previousDocument = global.document;
    const previousLocalStorage = global.localStorage;
    const stored = new Map();
    const oldCard = {
        dataset: {
            eoFreeBucket: '1',
            eoFreeBucketAcceptanceId: '17',
            eoFreeBucketAcceptanceLocalId: 'old-local',
            truckId: '21',
        },
        removed: false,
        remove() { this.removed = true; },
    };
    const newerCard = {
        dataset: {
            eoFreeBucket: '1',
            eoFreeBucketAcceptanceId: '99',
            eoFreeBucketAcceptanceLocalId: 'new-local',
            truckId: '21',
        },
        removed: false,
        remove() { this.removed = true; },
    };
    const cards = [oldCard, newerCard];
    const shell = {
        dataset: {eoAccessId: '7', eoCurrentExcavatorId: '8'},
        querySelector() { return null; },
    };
    global.localStorage = {
        getItem(key) { return stored.has(key) ? stored.get(key) : null; },
        setItem(key, value) { stored.set(key, value); },
    };
    global.document = {
        querySelector(selector) { return selector === '[data-eo-shell]' ? shell : null; },
        querySelectorAll() { return cards.filter(card => !card.removed); },
    };
    try {
        restoreLoadedCancellation({
            payload: {
                truck_id: 21,
                free_bucket_acceptance_id: 17,
                free_bucket_acceptance_local_id: 'old-local',
            },
            result: {free_bucket_restored: false},
        });

        assert.equal(oldCard.removed, true);
        assert.equal(newerCard.removed, false);
        const consumed = JSON.parse(Array.from(stored.values())[0] || '{}');
        assert.ok(consumed['17']);
        assert.ok(consumed['old-local']);
        assert.equal(consumed['99'], undefined);
        assert.equal(consumed['new-local'], undefined);
    } finally {
        if (previousDocument === undefined) delete global.document;
        else global.document = previousDocument;
        if (previousLocalStorage === undefined) delete global.localStorage;
        else global.localStorage = previousLocalStorage;
    }
});

test('fresh server snapshot prevents an old confirmed cancellation from resurrecting an acceptance', () => {
    const record = {
        event: {
            event_type: 'excavator.trip.loaded.cancelled',
            payload: {
                free_bucket_acceptance_id: 17,
                free_bucket_acceptance_local_id: 'old-local',
            },
        },
        result: {
            free_bucket_restored: true,
            version: 40,
            server_ids: {free_bucket_acceptance_id: 17},
            free_bucket_client_acceptance_id: 'old-local',
        },
    };

    assert.equal(confirmedCancellationIsAbsent(record, {version: 41, cards: []}), true);
    assert.equal(confirmedCancellationIsAbsent(record, {
        version: 41,
        cards: [{id: 17, client_acceptance_id: 'old-local'}],
    }), false);
    assert.equal(
        confirmedCancellationIsAbsent(record, {version: 39, cards: []}),
        false,
        'an older snapshot cannot override the newer confirmed cancellation result',
    );
});

test('confirmed cancellation replay respects a newer acceptance for the same truck', () => {
    const previousDocument = global.document;
    const previousLocalStorage = global.localStorage;
    const stored = new Map();
    const newerCard = {
        dataset: {
            eoFreeBucket: '1',
            eoFreeBucketAcceptanceId: '99',
            eoFreeBucketAcceptanceLocalId: 'new-local',
            truckId: '21',
        },
        removed: false,
        remove() { this.removed = true; },
    };
    const shell = {
        dataset: {eoAccessId: '7', eoCurrentExcavatorId: '8'},
        querySelector(selector) {
            return selector.includes('data-truck-id="21"') ? newerCard : null;
        },
    };
    global.localStorage = {
        getItem(key) { return stored.has(key) ? stored.get(key) : null; },
        setItem(key, value) { stored.set(key, value); },
    };
    global.document = {
        querySelector(selector) { return selector === '[data-eo-shell]' ? shell : null; },
        querySelectorAll() { return newerCard.removed ? [] : [newerCard]; },
    };
    const record = {
        event: {
            event_type: 'excavator.trip.loaded.cancelled',
            sequence: 3,
            payload: {
                truck_id: 21,
                free_bucket_acceptance_id: 17,
                free_bucket_acceptance_local_id: 'old-local',
                free_bucket_reservation_expires_at_ms: Date.now() + 60_000,
            },
        },
        result: {
            free_bucket_restored: true,
            version: 40,
            server_ids: {free_bucket_acceptance_id: 17},
            free_bucket_client_acceptance_id: 'old-local',
        },
    };
    try {
        reconcileConfirmed([record], {
            version: 41,
            cards: [{id: 99, client_acceptance_id: 'new-local', truck_id: 21}],
        });

        assert.equal(newerCard.removed, false);
        assert.equal(newerCard.dataset.eoFreeBucketAcceptanceId, '99');
        assert.equal(newerCard.dataset.eoFreeBucketAcceptanceLocalId, 'new-local');
        const consumed = JSON.parse(Array.from(stored.values())[0] || '{}');
        assert.ok(consumed['17']);
        assert.ok(consumed['old-local']);
        assert.equal(consumed['99'], undefined);
    } finally {
        if (previousDocument === undefined) delete global.document;
        else global.document = previousDocument;
        if (previousLocalStorage === undefined) delete global.localStorage;
        else global.localStorage = previousLocalStorage;
    }
});

test('a no-effect acceptance keeps its local reference instead of borrowing the winner id', () => {
    assert.match(source, /eoFreeBucketAcceptanceId = result\.no_effect\s*\? ""/);
    assert.match(source, /eoFreeBucketAcceptanceId = result && result\.no_effect\s*\? ""/);
});

test('technical reconciliation never becomes a blocked field card or review prompt', () => {
    const attentionStart = source.indexOf('function markAttention(event, result) {');
    const attentionEnd = source.indexOf('function attachShell(options) {', attentionStart);
    assert.ok(attentionStart >= 0 && attentionEnd > attentionStart);
    const attentionSource = source.slice(attentionStart, attentionEnd);
    assert.doesNotMatch(attentionSource, /eoCanLoad\s*=\s*"0"/);
    assert.doesNotMatch(attentionSource, /is-free-bucket-conflict/);
    assert.doesNotMatch(attentionSource, /showNotice/);
    assert.doesNotMatch(attentionSource, /сверк|конфликт/i);
    assert.match(attentionSource, /console\.warn/);
    assert.match(attentionSource, /invalidateRefresh/);
    assert.doesNotMatch(source, /Требуется сверка|Свободный ковш · конфликт/);
    assert.doesNotMatch(template, /Нужна сверка|требует сверки/i);
});

test('dispatcher removes the used marker at its server deadline without polling', () => {
    assert.match(dispatcherTemplate, /data-dispatcher-free-bucket-expires-at/);
    assert.match(dispatcherTemplate, /data-server-now=/);
    assert.match(dispatcherSource, /function expireDispatcherFreeBucketMarkers\(\)/);
    assert.match(dispatcherSource, /serverNowClientCapturedAt/);
    assert.match(dispatcherSource, /marker\.remove\(\)/);
    assert.match(dispatcherSource, /setInterval\(expireDispatcherFreeBucketMarkers, 1000\)/);
});

test('one successful free-bucket swipe removes the upper card and keeps a separate five-minute badge', () => {
    assert.match(template, /data-eo-free-bucket-preview-expires-at/);
    assert.match(template, /function bindFreeBucketDumpCardExpiry\(shellRoot\)/);
    assert.match(template, /window\.eoFreeBucketQueuePreviewTimer/);
    assert.match(template, /Date\.parse\(event\.occurred_at\) \+ 5 \* 60 \* 1000/);
    assert.match(template, /freeBucketController\.markLoaded\(card\);\s*bindFreeBucketDumpCardExpiry\(shell\)/);
    assert.doesNotMatch(source, /Свободный ковш · отправлен/);
});

test('all free bucket assets share the current shell marker', () => {
    assert.match(template, /excavator-free-bucket-v1\.css[^\n]+excavator-mobile-shell-v263/);
    assert.match(template, /excavator-free-bucket-v1\.js[^\n]+excavator-mobile-shell-v263/);
});

test('temporary card adds semantics without replacing production status', () => {
    assert.match(source, /status-" \+ statusKey \+ " is-free-bucket/);
    assert.match(source, /eo-free-bucket-card-marker/);
    assert.match(source, /eo-free-bucket-card-accent/);
    assert.match(css, /\.eo-dashboard-truck-card\.is-free-bucket \.eo-free-bucket-card-accent/);
    assert.match(css, /@media \(prefers-reduced-motion: reduce\)/);
    assert.match(source, /is-free-bucket-overflow/);
    assert.match(template, /eo-free-bucket-remote-marker/);
    assert.match(template, /foreign_free_bucket %\} is-free-bucket-remote/);
    assert.match(css, /\.eo-free-bucket-remote-marker/);
    assert.match(css, /is-free-bucket-remote \{[\s\S]*grid-template-areas:\s*"icon" "number" "freebucket" "target"/);
    assert.match(css, /grid-area:\s*freebucket/);
});

test('accept and cancel invalidate an older operational fragment first', () => {
    assert.match(source, /var invalidateRefresh = null;/);
    assert.match(source, /invalidateRefresh\(\);\s*queueEvent\("excavator\.free_bucket\.accepted"/);
    assert.match(source, /invalidateRefresh\(\);\s*return queueEvent\("excavator\.free_bucket\.cancelled"/);
    assert.match(template, /invalidateRefresh:\s*invalidateExcavatorWorkRefresh/);
});

test('an upward swipe cancels only an unused free bucket card through the durable outbox', () => {
    assert.match(template, /function isFreeBucketCancelSwipe\(state, deltaX, deltaY\)/);
    assert.match(template, /state\.card\.dataset\.eoFreeBucket === "1"/);
    assert.match(template, /state\.card\.dataset\.eoFreeBucketUsed !== "1"/);
    assert.match(template, /deltaY <= -56/);
    assert.match(template, /Math\.abs\(deltaY\) >= Math\.max\(56, Math\.abs\(deltaX\) \* 1\.25\)/);
    assert.match(template, /freeBucketController\.cancelAccepted\(state\.card\)/);
    assert.match(source, /function cancelAcceptedCard\(card\)/);
    assert.match(source, /queueEvent\("excavator\.free_bucket\.cancelled"/);
    assert.match(source, /eoFreeBucketCancelPending/);
    assert.match(source, /dependsOn: serverId \? \[\] : \[localId\]/);
    assert.match(source, /cancelAccepted: cancelAcceptedCard/);
    assert.match(css, /is-free-bucket-cancel-armed/);
    assert.match(css, /is-free-bucket-cancel-pending/);
});
