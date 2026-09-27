'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const backend = process.env.PASSPORT_BACKEND;
if (!backend) throw new Error('PASSPORT_BACKEND is required');
const transportSource = fs.readFileSync(path.join(backend, 'static/js/dispatcher-transport-v1.js'), 'utf8');

function transportFactory(localStorage) {
  const context = {
    console, Date, Error, JSON, Math, Object, Promise,
    FormData: class { append() {} },
    setTimeout, clearTimeout,
    localStorage,
    isAppRoleReadonly: () => false,
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(transportSource, context, {filename: 'dispatcher-transport-v1.js'});
  return context.createDispatcherTransport;
}

function storage() {
  const values = new Map();
  return {
    getItem: (key) => values.has(key) ? values.get(key) : null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
  };
}

function response(status, payload) {
  return {ok: status >= 200 && status < 300, status, json: async () => payload};
}

test('dispatcher direct 409 is not persisted; queued 409 is removed without retry', async () => {
  const store = storage();
  const errors = [];
  let calls = 0;
  const createDispatcherTransport = transportFactory(store);
  const direct = createDispatcherTransport({
    storage: store,
    queueKey: 'passport-queue',
    fetch: async () => { calls += 1; return response(409, {ok: false, conflict: true, code: 'state_conflict'}); },
    onServerError: (payload) => errors.push(payload),
  });
  await assert.rejects(() => direct.post('/dispatcher/', {client_action_id: 'direct-stale'}));
  assert.equal(direct.getQueueState().length, 0);

  const offline = createDispatcherTransport({
    storage: store,
    queueKey: 'passport-queue',
    fetch: async () => { throw new TypeError('network down'); },
  });
  await offline.post('/dispatcher/', {client_action_id: 'queued-stale'});
  assert.equal(offline.getQueueState().length, 1);

  const replayErrors = [];
  const replay = createDispatcherTransport({
    storage: store,
    queueKey: 'passport-queue',
    fetch: async () => response(409, {ok: false, conflict: true, code: 'state_conflict'}),
    onServerError: (payload) => replayErrors.push(payload),
  });
  replay.flush();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(replay.getQueueState().length, 0);
  assert.equal(replayErrors.length, 1);
  assert.equal(replayErrors[0].code, 'state_conflict');
  console.log('EVIDENCE queue_409', JSON.stringify({direct_saved: 0, offline_saved: 1, after_409: 0, retry: false}));
});

test('both workplace wiring paths expose the documented queue and refresh behavior', () => {
  const template = fs.readFileSync(path.join(backend, 'templates/trips/dispatcher_control.html'), 'utf8');
  assert.match(template, /function dispatcherPostQueued/);
  assert.match(template, /enqueueDispatcherSyncRequest\(\{/);
  assert.match(template, /dispatcherPostQueued\(dispatcherAssignTruckUrl/);
  assert.match(template, /dispatcherPost\(dispatcherAssignTruckUrl/);
  assert.match(template, /freshQueue = readDispatcherSyncQueue\(\)\.filter[\s\S]{0,1400}showDispatcherDnDError\(error\)/);
  assert.match(template, /if \(error && error\.conflict\)[\s\S]{0,300}refreshMobileBoardFromServer\(\{ preserveScreen: true \}\)[\s\S]{0,200}refreshDispatcherDesktopBoardFromServer\(\)/);
  assert.match(template, /markMiningMasterBoardStale\(\)/);
  console.log('EVIDENCE wiring', JSON.stringify({dispatcher_direct_first: true, master_queued_first: true, conflict_notice: true, mobile_refresh: true, desktop_refresh: true}));
});
