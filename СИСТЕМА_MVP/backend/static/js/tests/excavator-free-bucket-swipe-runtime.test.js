const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const backend = path.resolve(__dirname, '..', '..', '..');
const template = fs.readFileSync(path.join(backend, 'templates', 'trips', 'excavator_work.html'), 'utf8');
const controllerSource = fs.readFileSync(path.join(backend, 'static', 'js', 'excavator-free-bucket-v1.js'), 'utf8');

function gestureFixture({freeBucket = true, target = null} = {}) {
    const calls = {cancel: 0, load: 0};
    const classes = new Set();
    const card = {
        dataset: {
            eoFreeBucket: freeBucket ? '1' : '0',
            eoFreeBucketUsed: '0',
            eoFreeBucketCancelPending: '0',
            eoSuppressClick: '',
        },
        classList: {
            add: (...names) => names.forEach(name => classes.add(name)),
            remove: (...names) => names.forEach(name => classes.delete(name)),
        },
        style: {transform: ''},
        releasePointerCapture() {},
    };
    const context = {
        Math,
        Number,
        activeTruckDrag: {
            card,
            pointerId: 7,
            startX: 100,
            startY: 180,
            started: true,
            target,
            longPressOpened: false,
        },
        freeBucketController: {cancelAccepted() { calls.cancel += 1; }},
        removeTruckDragPreview() {},
        clearDropReady() {},
        canTruckLoad: () => true,
        selectTruck() {},
        selectDump() {},
        postTruckLoaded() { calls.load += 1; },
        window: {setTimeout(fn) { fn(); }},
    };
    vm.createContext(context);
    const helperStart = template.indexOf('    function isFreeBucketCancelSwipe(state, deltaX, deltaY)');
    const helperEnd = template.indexOf('    tabs.forEach(function (tab)', helperStart);
    vm.runInContext(template.slice(helperStart, helperEnd), context);
    const finishStart = template.indexOf('    function finishTruckDrag(event)');
    const finishEnd = template.indexOf('    function cancelTruckDrag(event)', finishStart);
    vm.runInContext(template.slice(finishStart, finishEnd), context);
    return {context, card, classes, calls};
}

test('predominantly upward swipe cancels exactly once and never creates a load', () => {
    const fixture = gestureFixture({target: {dataset: {eoDumpTarget: '9'}}});
    fixture.context.finishTruckDrag({pointerId: 7, clientX: 104, clientY: 110, preventDefault() {}});
    assert.deepEqual(fixture.calls, {cancel: 1, load: 0});
    assert.equal(fixture.context.activeTruckDrag, null);
});

test('short, sideways and ordinary truck gestures never cancel free bucket acceptance', () => {
    for (const coordinates of [
        {clientX: 102, clientY: 130},
        {clientX: 175, clientY: 115},
    ]) {
        const fixture = gestureFixture();
        fixture.context.finishTruckDrag({pointerId: 7, ...coordinates, preventDefault() {}});
        assert.equal(fixture.calls.cancel, 0);
    }
    const ordinary = gestureFixture({freeBucket: false});
    ordinary.context.finishTruckDrag({pointerId: 7, clientX: 100, clientY: 100, preventDefault() {}});
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
