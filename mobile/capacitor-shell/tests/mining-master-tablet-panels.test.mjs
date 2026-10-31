import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { createPanelState } = require('../profiles/mining_master_tablet/assets/tablet/mining-master-tablet.js');

function fixture() {
  let now = 0, serial = 0, blocked = false;
  const timers = new Map(), frames = [];
  const controller = createPanelState({
    setTimer(callback, delay) { const id = ++serial; timers.set(id, { at: now + delay, callback }); return id; },
    clearTimer(id) { timers.delete(id); },
    blocked: () => blocked,
    render(state) { frames.push({ at: now, ...state }); },
  });
  function advance(ms) {
    const until = now + ms;
    while (true) {
      const due = [...timers].filter(([, timer]) => timer.at <= until).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      now = due[1].at; timers.delete(due[0]); due[1].callback();
    }
    now = until;
  }
  return { controller, frames, timers, advance, block(value) { blocked = value; } };
}

test('idle closes after ten seconds and emits no countdown DOM publications', () => {
  const f = fixture(); f.controller.request('top');
  f.advance(9999); assert.deepEqual(f.frames, [{ at: 0, active: 'top', phase: 'open' }]);
  f.advance(1); assert.equal(f.controller.state().phase, 'closing');
  f.advance(200); assert.equal(f.controller.state().active, null);
  assert.equal(f.frames.length, 3);
});

test('interaction starts a fresh ten-second idle interval', () => {
  const f = fixture(); f.controller.request('left'); f.advance(9000); f.controller.activity();
  f.advance(9999); assert.equal(f.controller.state().phase, 'open');
  f.advance(1); assert.equal(f.controller.state().phase, 'closing');
});

test('switching panels closes the old panel fully before opening the next', () => {
  const f = fixture(); f.controller.request('left'); f.controller.request('right');
  f.advance(199); assert.equal(f.controller.state().active, 'left'); assert.equal(f.controller.state().phase, 'closing');
  assert.equal(f.frames.some(frame => frame.active === 'right'), false);
  f.advance(1);
  assert.deepEqual(f.frames.slice(-2), [{ at: 200, active: null, phase: 'closed' }, { at: 200, active: 'right', phase: 'open' }]);
});

test('rapid alternating edges keep only the last requested panel', () => {
  const f = fixture(); f.controller.request('top'); f.controller.request('left'); f.advance(50);
  f.controller.request('bottom'); f.controller.request('right'); f.advance(150);
  assert.equal(f.controller.state().active, 'right');
  assert.equal(f.frames.some(frame => frame.active === 'left' || frame.active === 'bottom'), false);
});

test('pause for a gesture, input, keyboard or dialog resumes with a full interval', () => {
  const f = fixture(); f.controller.request('bottom'); f.advance(9900); f.controller.pause(true);
  f.advance(60000); assert.equal(f.controller.state().phase, 'open');
  f.controller.pause(false); f.advance(9999); assert.equal(f.controller.state().phase, 'open');
  f.advance(1); assert.equal(f.controller.state().phase, 'closing');
});

test('a newly opened dialog is rechecked at timeout even before its observer runs', () => {
  const f = fixture(); f.controller.request('top'); f.block(true); f.advance(10000);
  assert.equal(f.controller.state().phase, 'open'); assert.equal(f.controller.state().paused, true);
  f.block(false); f.controller.pause(false); f.advance(9999); assert.equal(f.controller.state().phase, 'open');
});

test('closing during a pending switch cancels the new panel', () => {
  const f = fixture(); f.controller.request('left'); f.controller.request('right'); f.controller.request(null); f.advance(300);
  assert.equal(f.controller.state().active, null); assert.equal(f.frames.some(frame => frame.active === 'right'), false);
});

test('fragment teardown cancels idle and transition work without late writes', () => {
  for (const closing of [false, true]) {
    const f = fixture(); f.controller.request('top'); if (closing) f.controller.request('right');
    const before = f.frames.length; f.controller.destroy(); f.advance(60000);
    f.controller.request('left'); f.controller.activity(); f.controller.pause(false);
    assert.equal(f.frames.length, before); assert.equal(f.timers.size, 0);
  }
});
