import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const script = readFileSync(path.join(here, '../profiles/mining_master_tablet/assets/tablet/mining-master-tablet.js'), 'utf8');
const template = readFileSync(path.join(here, '../../../СИСТЕМА_MVP/backend/templates/trips/dispatcher_control.html'), 'utf8');
const dndSource = readFileSync(path.join(here, '../../../СИСТЕМА_MVP/backend/static/js/dispatcher-board-dnd-v1.js'), 'utf8');
const moduleContext = { module: { exports: {} } };
vm.runInNewContext(script, moduleContext);
const adapter = moduleContext.module.exports;

test('related page layout only accepts known HTML routes on the master tablet origin', () => {
  const routes = ['reports', 'shift-log', 'management', 'mining-volumes', 'transport', 'downtimes', 'shift-reports/trucks', 'shift-reports/excavation', 'shift-reports/hourly', 'shift-reports/hourly-grid'];
  for (const route of routes) {
    const url = new URL(`https://mining-master.driverform.ru/mining-master/tablet/dispatcher/${route}/?date=2026-10-30#details`);
    assert.equal(adapter.allowedRelatedLocation(url), true);
    assert.equal(adapter.allowedLocation(url), false, 'a report must not activate board operations');
  }
  for (const route of ['customer-daily', 'volume', 'templates', 'downtimes']) {
    const url = new URL(`https://mining-master.driverform.ru/mining-master/tablet/reports/${route}/?date=2026-10-30#details`);
    assert.equal(adapter.allowedRelatedLocation(url), true);
    assert.equal(adapter.allowedLocation(url), false, 'common reports must not activate board operations');
    assert.equal(adapter.allowedRelatedLocation(new URL(`https://mining-master.driverform.ru/mining-master/tablet/reports/${route}/export/`)), false);
  }
  for (const url of ['https://mining-master.driverform.ru/mining-master/tablet/', 'https://mining-master.driverform.ru/mining-master/tablet/dispatcher/reports/export/', 'https://mining-master.driverform.ru/mining-master/tablet/dispatcher/reports/unknown/', 'https://mining-master.driverform.ru/mining-master/tablet/dispatcher/reports', 'https://dispatcher.driverform.ru/mining-master/tablet/dispatcher/reports/', 'http://mining-master.driverform.ru/mining-master/tablet/dispatcher/reports/', 'https://mining-master.driverform.ru:8443/mining-master/tablet/dispatcher/reports/']) assert.equal(adapter.allowedRelatedLocation(new URL(url)), false);
});

class FakeNode {
  constructor(dataset = {}) {
    this.dataset = dataset;
    this.isConnected = true;
    this.classes = new Set();
    this.classList = { contains: value => this.classes.has(value), add: value => this.classes.add(value), remove: value => this.classes.delete(value) };
    this.handlers = new Map();
    this.events = [];
    this.complex = null;
  }
  addEventListener(type, callback) { this.handlers.set(type, [...(this.handlers.get(type) || []), callback]); }
  dispatchEvent(event) { this.events.push(event); for (const callback of this.handlers.get(event.type) || []) callback(event); return !event.defaultPrevented; }
  closest() { return this.complex; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
}
class Transfer {
  constructor() { this.data = {}; }
  setData(type, value) { this.data[type] = value; }
  setDragImage() {}
}
class Drag {
  constructor(type, options) { this.type = type; Object.assign(this, options); this.defaultPrevented = false; }
  preventDefault() { this.defaultPrevented = true; }
  stopPropagation() {}
}
function serverFunction(name) {
  const start = template.indexOf(`    function ${name}(`);
  assert.notEqual(start, -1, `existing role handler ${name} must exist`);
  let end = template.indexOf('\n    function ', start + 1);
  if (end < 0) end = template.length;
  return template.slice(start, end).trim();
}
function fixture(kind = 'truck', destination = 'complex') {
  const source = new FakeNode({ dispatcherDrag: kind, equipmentId: '17', equipmentName: '17', haulAssignmentStateId: '51', placementZone: kind === 'excavator' ? 'inactive' : 'active', complexTruck: kind === 'truck' ? 'true' : '' });
  const target = new FakeNode({ dispatcherDrop: destination, equipmentId: destination === 'complex' ? '29' : '' });
  const board = new FakeNode();
  board.contains = node => node === source || node === target;
  const posts = [];
  const confirmations = [];
  const context = {
    draggedTile: null, dragGhost: null, board, dispatcherShiftOpen: true,
    dispatcherMoveExcavatorUrl: '/mining-master/tablet/dispatcher/control/excavator/move/',
    dispatcherAssignTruckUrl: '/mining-master/tablet/dispatcher/control/truck/assign/',
    clearDragGhost() {}, document: { querySelectorAll: () => [], body: { appendChild() {} } },
    dispatcherPost(url, payload) { posts.push({ url, payload }); return Promise.resolve({ ok: true }); },
    haulAssignmentStateId: node => Number(node.dataset.haulAssignmentStateId || 0),
    applyDesktopTruckAction: () => {}, showDispatcherDnDError: error => { throw error; },
    refreshDesktopBoardAfterStructuralAction: () => {}, handleDesktopOptimisticBoardError: error => { throw error; },
    activateDesktopComplexFromExcavatorTile: () => {}, moveDesktopComplexToExcavatorGarage: () => {},
    collectComplexAssignmentStates: () => ({ 17: 51 }),
    window: { openAppConfirmDialog: (message, callback, delay, label, options) => confirmations.push({ message, callback, delay, label, options }) },
  };
  vm.createContext(context);
  for (const name of ['requestDispatcherDesktopDangerConfirmation']) {
    vm.runInContext(serverFunction(name), context);
  }
  const rootDocument = { body: { appendChild() {} }, querySelectorAll(selector) { if (selector === '[data-dispatcher-drag]') return [source]; if (selector === `[data-dispatcher-drop='${destination}']`) return [target]; return []; } };
  const dispatcherContext = { window: {}, document: rootDocument };
  vm.runInNewContext(dndSource, dispatcherContext);
  const dnd = dispatcherContext.window.createDispatcherBoardDnD({ document: rootDocument, post: context.dispatcherPost, moveExcavatorUrl: context.dispatcherMoveExcavatorUrl, assignTruckUrl: context.dispatcherAssignTruckUrl, getShiftOpen: () => context.dispatcherShiftOpen, getBoard: () => board, getAssignmentStateId: context.haulAssignmentStateId, collectComplexAssignmentStates: context.collectComplexAssignmentStates, applyTruckAction: context.applyDesktopTruckAction, confirmDanger: context.requestDispatcherDesktopDangerConfirmation });
  dnd.bind();
  return { source, target, board, posts, confirmations, server: context, context: { compatible: true, shiftOpen: true, ownShiftOpen: true, observer: false, modalOpen: false, board }, constructors: { DataTransfer: Transfer, DragEvent: Drag } };
}
function run(f, selection = adapter.snapshot(f.source, f.board)) { return adapter.dispatchExistingDrop(selection, f.target, f.context, f.constructors); }

test('adapter only accepts the exact master tablet origin and route', () => {
  const location = new URL('https://mining-master.driverform.ru/mining-master/tablet/');
  assert.equal(adapter.allowedLocation(location), true);
  for (const url of ['http://mining-master.driverform.ru/mining-master/tablet/', 'https://driverform.ru/mining-master/tablet/', 'https://mining-master.driverform.ru.evil.test/mining-master/tablet/', 'https://mining-master.driverform.ru:8443/mining-master/tablet/', 'https://mining-master.driverform.ru/mining-master/assignments/']) assert.equal(adapter.allowedLocation(new URL(url)), false);
});
test('a tablet assignment invokes the real role handler once with the current assignment token', async () => {
  const f = fixture();
  const selection = adapter.snapshot(f.source, f.board);
  assert.equal(run(f, selection), true);
  assert.equal(f.posts.length, 1);
  assert.equal(f.posts[0].url, '/mining-master/tablet/dispatcher/control/truck/assign/');
  assert.deepEqual(JSON.parse(JSON.stringify(f.posts[0].payload)), { action: 'assign', truck_id: '17', excavator_id: '29', expected_assignment_state_id: 51 });
  assert.deepEqual(f.source.events.map(event => event.type), ['dragstart', 'dragend']);
  assert.deepEqual(f.target.events.map(event => event.type), ['dragover', 'drop']);
  assert.equal(f.source.events[0].dataTransfer, f.target.events[1].dataTransfer);
  assert.equal(f.source.classList.contains('dispatcher-dragging'), false);
  assert.equal(run(f, selection), false);
  assert.equal(f.posts.length, 1);
  await Promise.resolve();
});
test('readonly, observer, modal and unrecognised DOM never dispatch role operations', () => {
  for (const mutation of [{ shiftOpen: false }, { observer: true }, { modalOpen: true }, { compatible: false }]) {
    const f = fixture(); Object.assign(f.context, mutation);
    assert.equal(run(f), false); assert.equal(f.posts.length, 0); assert.equal(f.source.events.length, 0);
  }
});
test('another employee shift cannot authorize tablet operations', () => {
  const f = fixture(); f.context.ownShiftOpen = false;
  assert.equal(run(f), false); assert.equal(f.posts.length, 0);
});
test('replaced fragments, disconnected nodes and changed assignment state invalidate the selection', () => {
  for (const change of [f => { f.source.isConnected = false; }, f => { f.target.isConnected = false; }, f => { f.context.board = new FakeNode(); }, f => { f.source.dataset.haulAssignmentStateId = '52'; }, f => { f.source.dataset.assignedZone = 'new-zone'; }]) {
    const f = fixture(); const selection = adapter.snapshot(f.source, f.board); change(f);
    assert.equal(run(f, selection), false); assert.equal(f.posts.length, 0);
  }
});
test('cancelled selection, same complex and unbound controls cannot submit', () => {
  const f = fixture(); assert.equal(run(f, null), false);
  f.source.complex = f.target; assert.equal(run(f), false);
  f.source.complex = null; f.target.dataset.dispatcherDropBound = 'false'; assert.equal(run(f), false);
  assert.equal(f.posts.length, 0);
});
test('returning a complex to reserve retains the existing confirmation before any POST', async () => {
  const f = fixture('complex', 'excavator-garage');
  assert.equal(run(f), true);
  assert.equal(f.posts.length, 0);
  assert.equal(f.confirmations.length, 1);
  assert.equal(f.confirmations[0].options.confirmTitle, 'Расформировать комплекс?');
  f.confirmations[0].callback();
  assert.equal(f.posts.length, 1);
  assert.equal(f.posts[0].url, '/mining-master/tablet/dispatcher/control/excavator/move/');
  assert.equal(f.posts[0].payload.zone, 'inactive');
  assert.equal(f.posts[0].payload.expected_zone, 'active');
  await Promise.resolve();
});
test('the server dragstart veto is respected and drag state is cleaned', () => {
  const f = fixture(); f.server.dispatcherShiftOpen = false;
  assert.equal(run(f), false); assert.equal(f.posts.length, 0); assert.equal(f.source.classList.contains('dispatcher-dragging'), false);
});
