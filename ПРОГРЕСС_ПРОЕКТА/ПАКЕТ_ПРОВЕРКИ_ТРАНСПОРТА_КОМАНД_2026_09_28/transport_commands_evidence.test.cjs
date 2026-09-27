'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const backend = process.env.PASSPORT_BACKEND;
if (!backend) throw new Error('PASSPORT_BACKEND is required');

const dispatcherSource = fs.readFileSync(
  path.join(backend, 'static/js/dispatcher-transport-v1.js'),
  'utf8',
);
const masterTemplate = fs.readFileSync(
  path.join(backend, 'templates/trips/dispatcher_control.html'),
  'utf8',
);

function evidence(name, value) {
  console.log(`EVIDENCE_TRANSPORT ${name} ${JSON.stringify(value)}`);
}

function createStorage(seed) {
  const values = seed || new Map();
  return {
    values,
    getItem(key) { return values.has(key) ? values.get(key) : null; },
    setItem(key, value) { values.set(key, String(value)); },
    removeItem(key) { values.delete(key); },
  };
}

function fakeDate(clock) {
  return class EvidenceDate extends Date {
    static now() { return clock.now; }
  };
}

function fakeMath() {
  const value = Object.create(Math);
  let counter = 0;
  value.random = () => (++counter) / 1000;
  return value;
}

function response(payload = {ok: true}) {
  return {ok: true, status: 200, json: () => Promise.resolve(payload)};
}

async function settle() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

function createDispatcherRuntime({storage, clock, fetch}) {
  const timers = [];
  const window = {
    localStorage: storage,
    isAppRoleReadonly: () => false,
    setTimeout(callback, delay) {
      timers.push({callback, delay});
      return timers.length;
    },
    clearTimeout() {},
  };
  const context = vm.createContext({
    window,
    Date: fakeDate(clock),
    Math: fakeMath(),
    Promise,
    Error,
    JSON,
    Object,
  });
  vm.runInContext(dispatcherSource, context, {filename: 'dispatcher-transport-v1.js'});
  const transport = window.createDispatcherTransport({fetch});
  return {transport, timers};
}

function extractFunction(source, name) {
  const start = source.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`Function not found: ${name}`);
  const brace = source.indexOf('{', start);
  let depth = 0;
  let quote = '';
  let escaped = false;
  let lineComment = false;
  let blockComment = false;
  for (let i = brace; i < source.length; i += 1) {
    const ch = source[i];
    const next = source[i + 1];
    if (lineComment) {
      if (ch === '\n') lineComment = false;
      continue;
    }
    if (blockComment) {
      if (ch === '*' && next === '/') { blockComment = false; i += 1; }
      continue;
    }
    if (quote) {
      if (escaped) escaped = false;
      else if (ch === '\\') escaped = true;
      else if (ch === quote) quote = '';
      continue;
    }
    if (ch === '/' && next === '/') { lineComment = true; i += 1; continue; }
    if (ch === '/' && next === '*') { blockComment = true; i += 1; continue; }
    if (ch === '"' || ch === "'" || ch === '`') { quote = ch; continue; }
    if (ch === '{') depth += 1;
    if (ch === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error(`Unclosed function: ${name}`);
}

function createMasterRuntime({storage, clock, fetch}) {
  const timers = [];
  const effects = {mobileRefresh: 0, pending: []};
  const context = {
    console,
    Date: fakeDate(clock),
    Math: fakeMath(),
    Error,
    JSON,
    Object,
    Promise,
    fetch,
    dispatcherSyncQueueKey: 'mining-master-mobile-sync-queue-v3',
    dispatcherSyncRequestTimeoutMs: 12000,
    dispatcherMobileSyncFlushDelayMs: 300,
    dispatcherSyncQueueFlushing: false,
    dispatcherSyncFlushTimer: null,
    dispatcherRoleIsReadonly: () => false,
    dispatcherInactiveRoleError: () => Object.assign(new Error('readonly'), {isServerResponse: true}),
    updateDispatcherSyncIndicator: () => {},
    setDispatcherSyncPending: (value) => effects.pending.push(Boolean(value)),
    markMiningMasterBoardStale: () => {},
    refreshMobileBoardFromServer: () => {
      effects.mobileRefresh += 1;
      return Promise.resolve(true);
    },
    refreshDispatcherDesktopBoardFromServer: () => Promise.resolve(true),
    showDispatcherNotice: () => true,
    getCsrfToken: () => 'test-csrf',
    effects,
    timers,
  };
  context.window = {
    localStorage: storage,
    isAppRoleReadonly: () => false,
    setTimeout(callback, delay) {
      timers.push({callback, delay});
      return timers.length;
    },
    clearTimeout() {},
  };
  const source = [
    'var dispatcherSyncQueueKey = "mining-master-mobile-sync-queue-v3";',
    'var dispatcherSyncRequestTimeoutMs = 12000;',
    'var dispatcherMobileSyncFlushDelayMs = 300;',
    'var dispatcherSyncQueueFlushing = false;',
    'var dispatcherSyncFlushTimer = null;',
    extractFunction(masterTemplate, 'readDispatcherSyncQueue'),
    extractFunction(masterTemplate, 'writeDispatcherSyncQueue'),
    extractFunction(masterTemplate, 'enqueueDispatcherSyncRequest'),
    extractFunction(masterTemplate, 'sendDispatcherSyncRequest'),
    extractFunction(masterTemplate, 'flushDispatcherSyncQueue'),
    extractFunction(masterTemplate, 'scheduleDispatcherSyncFlush'),
    extractFunction(masterTemplate, 'dispatcherPostQueued'),
  ].join('\n');
  vm.createContext(context);
  vm.runInContext(source, context, {filename: 'dispatcher-control-master-inline.js'});
  return context;
}

function postedData(call) {
  return JSON.parse(call.options.body);
}

test('dispatcher fresh online command is direct-first and is not locally durable', async () => {
  const storage = createStorage();
  const clock = {now: 1_000_000};
  const calls = [];
  const runtime = createDispatcherRuntime({
    storage,
    clock,
    fetch: (url, options) => { calls.push({url, options}); return Promise.resolve(response({ok: true})); },
  });
  const result = await runtime.transport.post('/assign/', {action: 'assign', truck_id: 7, excavator_id: 3});
  assert.equal(result.ok, true);
  assert.equal(calls.length, 1);
  assert.match(postedData(calls[0]).client_action_id, /^mm-1000000-/);
  assert.deepEqual(runtime.transport.readQueue(), []);
  evidence('dispatcher_fresh_online', {
    first_path: 'direct', client_action_id: postedData(calls[0]).client_action_id,
    locally_saved_before_send: false, queue_after_response: 0, server_response: 'MOCK',
  });
});

test('dispatcher lost response queues the same payload and retries the same action id', async () => {
  const storage = createStorage();
  const clock = {now: 2_000_000};
  const calls = [];
  const accepted = new Set();
  let attempt = 0;
  const runtime = createDispatcherRuntime({
    storage,
    clock,
    fetch: (url, options) => {
      calls.push({url, options});
      const data = postedData(calls.at(-1));
      attempt += 1;
      if (attempt === 1) {
        accepted.add(data.client_action_id); // MOCK server acceptance before response loss.
        return Promise.reject(new TypeError('MOCK response lost'));
      }
      return Promise.resolve(response({ok: true, deduplicated: accepted.has(data.client_action_id)}));
    },
  });
  const firstResult = await runtime.transport.post('/assign/', {action: 'assign', truck_id: 8, excavator_id: 4});
  assert.equal(firstResult.queued, true);
  const saved = runtime.transport.readQueue()[0];
  assert.equal(saved.createdAt, 2_000_000);
  assert.equal(saved.attempts, 0);
  runtime.transport.flush();
  await settle();
  assert.equal(runtime.transport.readQueue().length, 0);
  assert.equal(calls.length, 2);
  assert.deepEqual(postedData(calls[1]), postedData(calls[0]));
  evidence('dispatcher_lost_response_retry_MOCK', {
    mock_acceptance: true, same_client_action_id: true, same_payload: true,
    queue_record_id: saved.id, createdAt: saved.createdAt, attempts_before_retry: saved.attempts,
    queue_after_retry: 0,
  });
});

test('dispatcher permits two direct first sends to complete in reverse order', async () => {
  const storage = createStorage();
  const clock = {now: 3_000_000};
  const calls = [];
  const resolvers = [];
  const completed = [];
  const runtime = createDispatcherRuntime({
    storage,
    clock,
    fetch: (url, options) => {
      const index = calls.length;
      calls.push({url, options});
      return new Promise((resolve) => resolvers.push(() => {
        completed.push(postedData(calls[index]).truck_id);
        resolve(response({ok: true}));
      }));
    },
  });
  const first = runtime.transport.post('/assign/', {action: 'assign', truck_id: 'A', excavator_id: 1});
  clock.now += 1;
  const second = runtime.transport.post('/assign/', {action: 'assign', truck_id: 'B', excavator_id: 2});
  assert.equal(calls.length, 2);
  resolvers[1]();
  await second;
  resolvers[0]();
  await first;
  assert.deepEqual(completed, ['B', 'A']);
  assert.notEqual(postedData(calls[0]).client_action_id, postedData(calls[1]).client_action_id);
  evidence('dispatcher_reverse_direct_MOCK', {
    call_order: ['A', 'B'], mock_completion_order: completed,
    locally_serialized: false, ui_double_action_reachability: 'NOT_RUN',
  });
});

test('dispatcher queued fallback survives runtime restart', async () => {
  const storage = createStorage();
  const clock = {now: 4_000_000};
  const failed = createDispatcherRuntime({storage, clock, fetch: () => Promise.reject(new TypeError('offline'))});
  await failed.transport.post('/assign/', {action: 'release', truck_id: 9});
  const before = failed.transport.readQueue()[0];
  const calls = [];
  clock.now += 50_000;
  const restarted = createDispatcherRuntime({
    storage,
    clock,
    fetch: (url, options) => { calls.push({url, options}); return Promise.resolve(response()); },
  });
  assert.equal(restarted.transport.readQueue()[0].id, before.id);
  restarted.transport.flush();
  await settle();
  assert.equal(restarted.transport.readQueue().length, 0);
  assert.equal(postedData(calls[0]).client_action_id, before.data.client_action_id);
  evidence('dispatcher_restart', {
    queue_record_id: before.id, same_local_record_id: true, same_client_action_id: true,
    createdAt_preserved: before.createdAt, automatic_OS_restart_delivery: 'NOT_RUN',
  });
});

test('master fresh online command is queue-first, then actual flush sends it', async () => {
  const storage = createStorage();
  const clock = {now: 5_000_000};
  const calls = [];
  const runtime = createMasterRuntime({
    storage,
    clock,
    fetch: (url, options) => { calls.push({url, options}); return Promise.resolve(response()); },
  });
  const queued = await runtime.dispatcherPostQueued('/assign/', {action: 'assign', truck_id: 10, excavator_id: 5}, 99_999);
  assert.equal(queued.queued, true);
  const before = runtime.readDispatcherSyncQueue()[0];
  assert.equal(calls.length, 0);
  runtime.flushDispatcherSyncQueue();
  await settle();
  assert.equal(calls.length, 1);
  assert.equal(runtime.readDispatcherSyncQueue().length, 0);
  evidence('master_fresh_online', {
    first_path: 'queue', locally_saved_before_send: true,
    queue_record_id: before.id, client_action_id: before.data.client_action_id,
    createdAt: before.createdAt, queue_after_response: 0, server_response: 'MOCK',
  });
});

test('master lost response keeps and retries the same queued command', async () => {
  const storage = createStorage();
  const clock = {now: 6_000_000};
  const calls = [];
  const accepted = new Set();
  let attempt = 0;
  const runtime = createMasterRuntime({
    storage,
    clock,
    fetch: (url, options) => {
      calls.push({url, options});
      const data = postedData(calls.at(-1));
      attempt += 1;
      if (attempt === 1) {
        accepted.add(data.client_action_id); // MOCK server acceptance before response loss.
        return Promise.reject(new TypeError('MOCK response lost'));
      }
      return Promise.resolve(response({ok: true, deduplicated: accepted.has(data.client_action_id)}));
    },
  });
  await runtime.dispatcherPostQueued('/assign/', {action: 'release', truck_id: 11}, 99_999);
  const original = runtime.readDispatcherSyncQueue()[0];
  runtime.flushDispatcherSyncQueue();
  await settle();
  const retained = runtime.readDispatcherSyncQueue()[0];
  assert.equal(retained.id, original.id);
  assert.equal(retained.attempts, 1);
  runtime.flushDispatcherSyncQueue();
  await settle();
  assert.equal(runtime.readDispatcherSyncQueue().length, 0);
  assert.deepEqual(postedData(calls[1]), postedData(calls[0]));
  evidence('master_lost_response_retry_MOCK', {
    mock_acceptance: true, same_queue_record_id: true,
    same_client_action_id: true, same_payload: true, attempts_after_loss: retained.attempts,
    queue_after_retry: 0,
  });
});

test('master queued command survives runtime restart', async () => {
  const storage = createStorage();
  const clock = {now: 7_000_000};
  const first = createMasterRuntime({storage, clock, fetch: () => Promise.reject(new TypeError('unused'))});
  await first.dispatcherPostQueued('/assign/', {action: 'assign', truck_id: 12, excavator_id: 6}, 99_999);
  const before = first.readDispatcherSyncQueue()[0];
  const calls = [];
  clock.now += 40_000;
  const restarted = createMasterRuntime({
    storage,
    clock,
    fetch: (url, options) => { calls.push({url, options}); return Promise.resolve(response()); },
  });
  assert.equal(restarted.readDispatcherSyncQueue()[0].id, before.id);
  restarted.flushDispatcherSyncQueue();
  await settle();
  assert.equal(restarted.readDispatcherSyncQueue().length, 0);
  assert.equal(postedData(calls[0]).client_action_id, before.data.client_action_id);
  evidence('master_restart', {
    queue_record_id: before.id, same_local_record_id: true, same_client_action_id: true,
    createdAt_preserved: before.createdAt, startup_schedule_present_in_template: true,
    installed_app_restart: 'NOT_RUN',
  });
});

test('master coalescing replaces identity and payload but keeps the earlier createdAt', async () => {
  const storage = createStorage();
  const clock = {now: 8_000_000};
  const runtime = createMasterRuntime({storage, clock, fetch: () => Promise.resolve(response())});
  await runtime.dispatcherPostQueued('/assign/', {action: 'assign', truck_id: 13, excavator_id: 7}, 99_999);
  const first = runtime.readDispatcherSyncQueue()[0];
  clock.now += 10_000;
  await runtime.dispatcherPostQueued('/assign/', {action: 'assign', truck_id: 13, excavator_id: 8}, 99_999);
  const merged = runtime.readDispatcherSyncQueue()[0];
  assert.equal(runtime.readDispatcherSyncQueue().length, 1);
  assert.notEqual(merged.id, first.id);
  assert.notEqual(merged.data.client_action_id, first.data.client_action_id);
  assert.equal(merged.createdAt, first.createdAt);
  assert.equal(merged.data.excavator_id, 8);
  evidence('master_coalesce', {
    old_queue_record_id: first.id, new_queue_record_id: merged.id,
    client_action_id_replaced: true, payload_replaced: true,
    old_createdAt_preserved_for_new_command: true,
  });
});

test('local temporal metadata is not transmitted in either role payload', async () => {
  const storageA = createStorage();
  const storageB = createStorage();
  const clock = {now: 9_000_000};
  const dispatcherCalls = [];
  const masterCalls = [];
  const dispatcher = createDispatcherRuntime({
    storage: storageA,
    clock,
    fetch: (url, options) => { dispatcherCalls.push({url, options}); return Promise.resolve(response()); },
  });
  await dispatcher.transport.post('/assign/', {action: 'assign', truck_id: 14, excavator_id: 9});
  const master = createMasterRuntime({
    storage: storageB,
    clock,
    fetch: (url, options) => { masterCalls.push({url, options}); return Promise.resolve(response()); },
  });
  await master.dispatcherPostQueued('/assign/', {action: 'assign', truck_id: 15, excavator_id: 9}, 99_999);
  const localMaster = master.readDispatcherSyncQueue()[0];
  master.flushDispatcherSyncQueue();
  await settle();
  for (const data of [postedData(dispatcherCalls[0]), postedData(masterCalls[0])]) {
    assert.equal(Object.hasOwn(data, 'createdAt'), false);
    assert.equal(Object.hasOwn(data, 'attempts'), false);
    assert.equal(Object.hasOwn(data, 'occurred_at'), false);
    assert.equal(Object.hasOwn(data, 'format_version'), false);
  }
  evidence('temporal_fields', {
    local_master_createdAt: localMaster.createdAt,
    transmitted: ['client_action_id', 'action', 'truck_id', 'excavator_id'],
    not_transmitted: ['queue_record_id', 'createdAt', 'attempts', 'occurred_at', 'format_version'],
    clock_source: 'Date.now wall clock',
  });
});
