const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const template = fs.readFileSync(path.join(__dirname, '../../../templates/trips/excavator_work.html'), 'utf8');

function fixture(manual) {
    const handlers = {}, classes = new Set(), timers = new Map();
    let timerId = 0, vibrations = 0, details = 0, sends = 0;
    const card = {
        disabled: false, style: {},
        dataset: {eoManualAvailable: manual ? '1' : '0', eoTruckInactive: manual ? '1' : '', eoCanLoad: manual ? '0' : '1'},
        classList: {add: (...names) => names.forEach(n => classes.add(n)), remove: (...names) => names.forEach(n => classes.delete(n)), contains: n => classes.has(n)},
        addEventListener: (name, fn) => { handlers[name] = fn; },
        setPointerCapture() {}, releasePointerCapture() {},
    };
    const context = {
        activeTruckDrag: null, Math, card,
        shell: {querySelectorAll: () => [card]},
        window: {setTimeout: fn => {timers.set(++timerId, fn); return timerId;}, clearTimeout: id => timers.delete(id)},
        navigator: {vibrate: () => {vibrations++;}},
        isInactiveTruck: c => c.dataset.eoTruckInactive === '1',
        isTruckLoadBlocked: c => c.dataset.eoCanLoad === '0',
        selectTruck() {}, selectDump() {},
        openTruckDetailCard: () => {details++; return true;},
        removeTruckDragPreview() {}, clearDropReady() {},
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
    return {card, classes, context, fire, hold, counts: () => ({vibrations, details, sends})};
}

test('passive pickup needs hold, glows and vibrates without opening a modal', () => {
    const f = fixture(true);
    f.fire('pointerdown');
    assert.equal(f.context.canTruckLoad(f.card), false);
    f.hold();
    assert.equal(f.context.canTruckLoad(f.card), true);
    assert.equal(f.classes.has('is-picked-up'), true);
    assert.deepEqual(f.counts(), {vibrations: 1, details: 0, sends: 0});
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
    }
});

test('active icons respond immediately with no hold requirement', () => {
    const f = fixture(false);
    f.fire('pointerdown');
    assert.equal(f.classes.has('is-picked-up'), true);
    assert.equal(f.context.canTruckLoad(f.card), true);
    f.fire('pointerup');
    assert.equal(f.classes.has('is-picked-up'), false);
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
