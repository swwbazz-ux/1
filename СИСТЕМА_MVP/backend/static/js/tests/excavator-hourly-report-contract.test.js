const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const backend = path.resolve(__dirname, '..', '..', '..');
const source = fs.readFileSync(path.join(backend, 'static', 'js', 'excavator-hourly-report-v1.js'), 'utf8');
const template = fs.readFileSync(path.join(backend, 'templates', 'trips', 'excavator_work.html'), 'utf8');
const css = fs.readFileSync(path.join(backend, 'static', 'css', 'excavator-hourly-report-v1.css'), 'utf8');

test('existing plan ring is the only accessible hourly-report entry', () => {
    assert.match(template, /<button type="button" class="eo-dashboard-plan-ring[^>]+data-eo-hourly-report-open/);
    assert.match(template, /aria-label="Открыть почасовой отчёт\./);
    assert.equal((template.match(/data-eo-hourly-report-open/g) || []).length, 1);
});

test('report remains outside replaceable shell and blocks unsafe fragment replacement', () => {
    const mainEnd = template.indexOf('</main>');
    const modal = template.indexOf('data-eo-hourly-report-modal');
    assert.ok(mainEnd > 0 && modal > mainEnd);
    assert.match(template, /\[data-eo-hourly-report-modal\]:not\(\[hidden\]\)/);
});

test('report lifecycle is single-flight, stale guarded and cleans open-only listeners', () => {
    assert.match(source, /if \(inFlight\) \{\s*refreshQueued = true;/);
    assert.match(source, /generation !== requestGeneration/);
    assert.match(source, /AbortController/);
    assert.match(source, /bindOpenLifecycle\(\)/);
    assert.match(source, /unbindOpenLifecycle\(\)/);
    assert.match(source, /clearTimeout\(hourTimer\)/);
    assert.match(source, /history\.pushState/);
    assert.match(source, /window\.addEventListener\("popstate"/);
});

test('offline cache is explicit and unresolved local loads are not claimed', () => {
    assert.match(source, /eo-hourly-report-v1:/);
    assert.match(source, /Нет связи · данные/);
    assert.match(source, /Локально сохранённые, но ещё не синхронизированные погрузки/);
    assert.match(source, /Сохранённого отчёта пока нет/);
});

test('layout has fixed table columns, vertical scroll and reduced-motion support', () => {
    assert.match(css, /table-layout:\s*fixed/);
    assert.match(css, /overflow-x:\s*hidden/);
    assert.match(css, /overflow-y:\s*auto/);
    assert.match(css, /@media \(prefers-reduced-motion: reduce\)/);
    assert.match(css, /width:\s*min\(100%, 430px\)/);
});
