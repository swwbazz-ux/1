const {excavatorScreenSource} = require("./excavator-screen-source");
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const backend = path.resolve(__dirname, '..', '..', '..');
const template = [excavatorScreenSource(), ...[
    'templates/includes/excavator_dashboard_source_card.html',
].map(file => fs.readFileSync(path.join(backend, file), 'utf8'))].join('\n');
const controllerSource = fs.readFileSync(path.join(backend, 'static', 'js', 'excavator-free-bucket-v1.js'), 'utf8');

function gestureFixture({freeBucket = true, target = null} = {}) {
    const calls = {cancel: 0, load: 0};
    const handlers = {};
    const classes = new Set();
    const card = {
        dataset: {eoFreeBucket: freeBucket ? "1" : "0", eoFreeBucketUsed: "0"},
        classList: {
            add: (...names) => names.forEach(name => classes.add(name)),
            remove: (...names) => names.forEach(name => classes.delete(name)),
            contains: name => classes.has(name),
        },
        style: {},
        addEventListener(name, listener) { handlers[name] = listener; },
        getBoundingClientRect: () => ({top: 130, left: 50, width: 100, height: 100}),
        setPointerCapture() {},
        releasePointerCapture() {},
    };
    const shell = {
        classList: {remove() {}},
        addEventListener() {},
        querySelectorAll: selector => selector === "[data-eo-truck-card]" ? [card] : [],
    };
    const window = {setTimeout(fn) { fn(); }, clearTimeout() {}};
    const context = vm.createContext({window});
    const source = fs.readFileSync(path.join(backend, "static/js/excavator-dashboard-drag-v1.js"), "utf8");
    vm.runInContext(source, context);
    const helperStart = template.indexOf("    function isFreeBucketCancelSwipe(");
    const helperEnd = template.indexOf("    tabs.forEach(function (tab)", helperStart);
    assert.ok(helperStart >= 0 && helperEnd > helperStart);
    vm.runInContext(template.slice(helperStart, helperEnd), context);
    const controller = window.ExcavatorDashboardDrag.attach({
        shell,
        canDrag: () => true,
        isSpecialCancel: context.isFreeBucketCancelSwipe,
        onSpecialCancel() { calls.cancel++; },
        onDrop() { calls.load++; },
        haptic() {},
    });
    const start = {button: 0, pointerId: 7, clientX: 100, clientY: 180, preventDefault() {}};
    handlers.pointerdown(start);
    assert.ok(controller.active());
    Object.assign(controller.active(), {started: true, target});
    return {controller, calls, finish(coordinates) { handlers.pointerup({...start, ...coordinates}); }};

}

test('predominantly upward swipe cancels exactly once and never creates a load', () => {
    const fixture = gestureFixture({target: {dataset: {eoDumpTarget: '9'}}});
    fixture.finish({pointerId: 7, clientX: 104, clientY: 110, preventDefault() {}});
    assert.deepEqual(fixture.calls, {cancel: 1, load: 0});
    assert.equal(fixture.controller.active(), null);
});

test('short, sideways and ordinary truck gestures never cancel free bucket acceptance', () => {
    for (const coordinates of [
        {clientX: 102, clientY: 130},
        {clientX: 175, clientY: 115},
    ]) {
        const fixture = gestureFixture();
        fixture.finish({pointerId: 7, ...coordinates, preventDefault() {}});
        assert.equal(fixture.calls.cancel, 0);
    }
    const ordinary = gestureFixture({freeBucket: false});
    ordinary.finish({pointerId: 7, clientX: 100, clientY: 100, preventDefault() {}});
    assert.equal(ordinary.calls.cancel, 0);
});

test('cancel action is persisted before the card disappears and is protected from duplicates', async () => {
    let resolveQueue;
    const queued = [];
    const classes = new Set();
    const card = {
        dataset: {
            eoFreeBucket: '1',
            eoFreeBucketAcceptanceLocalId: 'accept-local-1',
            eoFreeBucketAcceptanceId: '',
            eoFreeBucketUsed: '0',
            eoCanLoad: '1',
            truckId: '42',
        },
        classList: {
            add: (...names) => names.forEach(name => classes.add(name)),
            remove: (...names) => names.forEach(name => classes.delete(name)),
        },
        removed: false,
        remove() { this.removed = true; },
    };
    const context = {
        Promise,
        Number,
        text: value => String(value == null ? '' : value).trim(),
        queueEvent(type, payload, options) {
            queued.push({type, payload, options});
            return new Promise(resolve => { resolveQueue = resolve; });
        },
        cardForAcceptance: () => card,
        normalizeGrid() {},
        renderSearch() {},
        showNotice() {},
        invalidateRefresh() {},
    };
    vm.createContext(context);
    const start = controllerSource.indexOf('    function cancelAcceptedCard(card)');
    const end = controllerSource.indexOf('    function removeAcceptance(button)', start);
    vm.runInContext(controllerSource.slice(start, end), context);

    const pending = context.cancelAcceptedCard(card);
    assert.equal(card.removed, false);
    assert.equal(card.dataset.eoFreeBucketCancelPending, '1');
    assert.equal(queued.length, 1);
    assert.equal(queued[0].type, 'excavator.free_bucket.cancelled');
    assert.equal(queued[0].payload.free_bucket_acceptance_local_id, 'accept-local-1');
    assert.deepEqual(Array.from(queued[0].options.dependsOn), ['accept-local-1']);

    const duplicate = await context.cancelAcceptedCard(card);
    assert.equal(duplicate, false);
    assert.equal(queued.length, 1);

    resolveQueue({event_id: 'cancel-1'});
    assert.equal(await pending, true);
    assert.equal(card.removed, true);
});
