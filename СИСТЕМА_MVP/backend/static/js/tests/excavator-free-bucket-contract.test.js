const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const backend = path.resolve(__dirname, '..', '..', '..');
const template = fs.readFileSync(path.join(backend, 'templates', 'trips', 'excavator_work.html'), 'utf8');
const source = fs.readFileSync(path.join(backend, 'static', 'js', 'excavator-free-bucket-v1.js'), 'utf8');
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
    assert.match(source, /value\.replace\(\/\\D\+\/g, ""\)\.indexOf\(digits\)/);
    assert.match(source, /catalogMeta\.updated_at/);
    assert.match(source, /availability_label/);
});

test('accept cancel and load use the durable field outbox', () => {
    assert.match(source, /queueEvent\("excavator\.free_bucket\.accepted"/);
    assert.match(source, /queueEvent\("excavator\.free_bucket\.cancelled"/);
    assert.match(template, /event_type: isFreeBucketLoad \? "excavator\.free_bucket\.loaded"/);
    assert.match(template, /free_bucket_acceptance_local_id: freeBucketAcceptanceLocalId/);
    assert.match(template, /isFreeBucketLoad \? freeBucketAcceptanceLocalId/);
    assert.match(template, /manual_control: card\.dataset\.eoManualAvailable === "1"/);
    assert.doesNotMatch(template, /manual_control: isFreeBucketLoad \|\|/);
    assert.match(template, /function bindExcavatorTruckCard\(card\)/);
    assert.match(template, /bindTruckCard: bindExcavatorTruckCard/);
    assert.match(source, /eoFreeBucketUsed/);
});

test('cancel and failed load do not restore an actionable stale card', () => {
    assert.match(source, /if \(\["pending", "syncing"\]\.indexOf\(event\.sync_state\) >= 0\) card\.remove\(\)/);
    assert.match(source, /event\.event_type !== "excavator\.free_bucket\.loaded"/);
    assert.match(source, /if \(terminalAttention\(event\)\) markAttention\(event, \{\}\)/);
    assert.match(source, /card\.dataset\.eoCanLoad = "0"/);
    assert.match(source, /card\.setAttribute\("draggable", "false"\)/);
    assert.match(source, /if \(item\.is_used\) \{/);
    assert.match(source, /markLoaded\(card, \{dump_point: text\(item\.dump_point\)\}\)/);
});

test('all free bucket assets share the v238 shell marker', () => {
    assert.match(template, /excavator-free-bucket-v1\.css[^\n]+excavator-mobile-shell-v238/);
    assert.match(template, /excavator-free-bucket-v1\.js[^\n]+excavator-mobile-shell-v238/);
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
