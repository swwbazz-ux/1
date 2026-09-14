const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const backend = path.resolve(__dirname, '..', '..', '..');
const source = fs.readFileSync(path.join(backend, 'static', 'js', 'excavator-hourly-report-v1.js'), 'utf8');
const template = fs.readFileSync(path.join(backend, 'templates', 'trips', 'excavator_work.html'), 'utf8');
const css = fs.readFileSync(path.join(backend, 'static', 'css', 'excavator-hourly-report-v1.css'), 'utf8');

test('existing plan ring is the only accessible hourly-report entry', () => {
    assert.match(template, /<div class="eo-dashboard-plan-ring[^>]+role="button" tabindex="0"[^>]+data-eo-hourly-report-open/);
    assert.match(template, /aria-label="Открыть почасовой отчёт./);
    assert.equal((template.match(/data-eo-hourly-report-open/g) || []).length, 1);
    assert.match(source, /event\.key === "Enter" \|\| event\.key === " "/);
});

test('report remains outside replaceable shell without blocking operational refresh', () => {
    const mainEnd = template.indexOf('</main>');
    const modal = template.indexOf('data-eo-hourly-report-modal');
    const unsafeStart = template.indexOf('function isExcavatorRefreshUnsafe');
    const unsafeEnd = template.indexOf('function parseOperationalPayloadVersion', unsafeStart);
    assert.ok(mainEnd > 0 && modal > mainEnd);
    assert.doesNotMatch(template.slice(unsafeStart, unsafeEnd), /data-eo-hourly-report-modal/);
    assert.match(source, /setUnderlyingBlocked\(true\);\s*requestReport\("live-update"\)/);
});

test('server schema renders current then previous as independent three-column blocks', () => {
    assert.match(source, /payload\.schema_version !== 2/);
    assert.match(source, /payload\.hours\.forEach/);
    assert.match(source, /"Куда отправлены"/);
    assert.match(source, /"БелАЗ"/);
    assert.match(source, /"NHL"/);
    assert.match(source, /"Итого"/);
    assert.match(source, /hour\.totals\.trip_count/);
    assert.match(source, /hourRow\.belaz \|\| "—"/);
    assert.match(source, /hourRow\.nhl \|\| "—"/);
});

test('report lifecycle is coalesced, stale guarded and closes only once', () => {
    assert.match(source, /if \(inFlight\) \{\s*refreshQueued = true;/);
    assert.match(source, /generation !== requestGeneration/);
    assert.match(source, /AbortController/);
    assert.match(source, /if \(!modal \|\| modal\.hidden \|\| closing\) return;/);
    assert.match(source, /closing = true;\s*history\.back\(\)/);
    assert.match(source, /window\.addEventListener\("popstate"/);
    assert.match(template, /eo-hourly-report__backdrop" type="button" tabindex="-1"/);
});

test('offline cache is explicit and unresolved local loads are not claimed', () => {
    assert.match(source, /eo-hourly-report-v2:/);
    assert.match(source, /"Нет связи · " \+ cachedAt/);
    assert.match(source, /Локально сохранённые, но ещё не синхронизированные погрузки/);
    assert.match(source, /Сохранённого отчёта пока нет/);
});

test('layout matches mobile block structure without horizontal overflow', () => {
    assert.match(css, /width:\s*min\(100%, 430px\)/);
    assert.match(css, /height:\s*calc\(100dvh[^;]+88px\)/);
    assert.match(css, /table-layout:\s*fixed/);
    assert.match(css, /col:first-child \{ width: 52%; \}/);
    assert.match(css, /col:nth-child\(3\) \{ width: 24%; \}/);
    assert.match(css, /overflow-x:\s*hidden/);
    assert.match(css, /overflow-y:\s*auto/);
    assert.match(css, /@media \(prefers-reduced-motion: reduce\)/);
    assert.match(css, /\.eo-hourly-report__close::before,[\s\S]*\.eo-hourly-report__close::after/);
    assert.match(css, /transform:\s*translate\(-50%, -50%\) rotate\(45deg\)/);
});
