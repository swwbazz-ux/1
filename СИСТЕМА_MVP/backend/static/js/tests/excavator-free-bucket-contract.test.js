const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {confirmedAcceptanceIsAbsent} = require('../excavator-free-bucket-v1.js');

const backend = path.resolve(__dirname, '..', '..', '..');
const template = fs.readFileSync(path.join(backend, 'templates', 'trips', 'excavator_work.html'), 'utf8');
const source = fs.readFileSync(path.join(backend, 'static', 'js', 'excavator-free-bucket-v1.js'), 'utf8');
const dispatcherSource = fs.readFileSync(path.join(backend, 'static', 'js', 'dispatcher-control-v1.js'), 'utf8');
const dispatcherTemplate = fs.readFileSync(path.join(backend, 'templates', 'trips', 'dispatcher_control.html'), 'utf8');
const css = fs.readFileSync(path.join(backend, 'static', 'css', 'excavator-free-bucket-v1.css'), 'utf8');

test('bucket entry stays beside the existing hourly report ring', () => {
    const widget = template.match(/<div class="eo-dashboard-plan-widget">([\s\S]*?)<\/div>\s*<\/div>/);
    assert.ok(widget);
    assert.ok(widget[1].indexOf('data-eo-free-bucket-open') < widget[1].indexOf('data-eo-hourly-report-open'));
    assert.equal((template.match(/data-eo-hourly-report-open/g) || []).length, 1);
    assert.match(template, /data-eo-free-bucket-open aria-label="Свободный ковш"/);
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
    assert.match(source, /function itemCanBeAccepted\(item\)/);
    assert.match(source, /item\.can_accept_free_bucket !== false/);
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
    assert.match(template, /excavator-free-bucket-v1\.css[^\n]+excavator-mobile-shell-v248/);
    assert.match(template, /excavator-free-bucket-v1\.js[^\n]+excavator-mobile-shell-v248/);
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
