const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const template = fs.readFileSync(path.join(__dirname, '../../../templates/trips/excavator_work.html'), 'utf8');

function fixture(manual) {
    const handlers = {}, classes = new Set(), timers = new Map();
    let timerId = 0, vibrations = 0, details = 0, sends = 0;
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
    return {card, classes, context, fire, hold, delays, haptics, counts: () => ({vibrations, details, sends})};
}

test('passive pickup needs hold, glows and vibrates without opening a modal', () => {
    const f = fixture(true);
    f.fire('pointerdown');
    assert.equal(f.delays[0], 290);
    assert.equal(f.context.canTruckLoad(f.card), false);
    f.hold();
    assert.equal(f.context.canTruckLoad(f.card), true);
    assert.equal(f.classes.has('is-picked-up'), true);
    assert.deepEqual(f.counts(), {vibrations: 1, details: 0, sends: 0});
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

function effectsFixture(reducedMotion = false) {
    const frames = new Map(), nodes = [];
    let nextFrame = 0;
    const makeNode = () => ({
        style: {}, children: [], setAttribute() {},
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
        document: {createElement: makeNode, body: {appendChild: node => nodes.push(node)}},
    };
    vm.createContext(context);
    const start = template.indexOf('    function moveTruckDragPreview(state, dx, dy)');
    const end = template.indexOf('    function findDumpTargetIntersectingPreview', start);
    vm.runInContext(template.slice(start, end), context);
    const preview = makeNode(); nodes.push(preview);
    const state = {preview, originRect: {left: 10, top: 20, width: 100, height: 100}};
    function tick(now) {
        const pending = [...frames.values()]; frames.clear(); pending.forEach(fn => fn(now));
    }
    return {context, state, frames, nodes, tick};
}

test('comet uses a bounded pool, fades while stationary and releases its RAF and DOM', () => {
    const f = effectsFixture();
    f.context.createTruckComet(f.state);
    const comet = f.state.comet;
    for (let n = 1; n <= 100; n++) {
        f.context.moveTruckDragPreview(f.state, n * 8, n * 4);
        f.tick(n * 16);
    }
    assert.equal(comet.layer.children.length, 12);
    assert.equal(f.nodes.length, 2);
    assert.ok(comet.particles.some(p => Number(p.node.style.opacity) > 0));
    f.tick(2000);
    assert.ok(comet.particles.every(p => Number(p.node.style.opacity || 0) === 0));
    f.context.removeTruckDragPreview(f.state);
    assert.equal(f.frames.size, 0);
    assert.equal(f.nodes.length, 0);
    assert.equal(f.state.comet, null);
});

test('larger visual preview preserves the nominal drop hitbox', () => {
    const f = effectsFixture();
    f.context.moveTruckDragPreview(f.state, 30, 40);
    assert.match(f.state.preview.style.transform, /scale\(1\.18\)/);
    assert.deepEqual({...f.state.previewHitRect}, {left: 45, right: 135, top: 65, bottom: 155});
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
