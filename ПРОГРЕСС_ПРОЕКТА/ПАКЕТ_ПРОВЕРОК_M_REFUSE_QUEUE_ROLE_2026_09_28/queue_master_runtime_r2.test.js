'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const backend = process.env.PASSPORT_BACKEND;
if (!backend) throw new Error('PASSPORT_BACKEND is required');
const template = fs.readFileSync(path.join(backend, 'templates/trips/dispatcher_control.html'), 'utf8');

function extractFunction(name) {
  const start = template.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`Function not found: ${name}`);
  const brace = template.indexOf('{', start);
  let depth = 0;
  let quote = '';
  let escaped = false;
  let lineComment = false;
  let blockComment = false;
  for (let i = brace; i < template.length; i += 1) {
    const ch = template[i];
    const next = template[i + 1];
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
      if (depth === 0) return template.slice(start, i + 1);
    }
  }
  throw new Error(`Unclosed function: ${name}`);
}

function createStorage() {
  const values = new Map();
  return {
    getItem(key) { return values.has(key) ? values.get(key) : null; },
    setItem(key, value) { values.set(key, String(value)); },
    removeItem(key) { values.delete(key); },
  };
}

function createRuntime() {
  const timers = [];
  const effects = {notice: [], stale: 0, mobileRefresh: 0, desktopRefresh: 0, pending: []};
  const storage = createStorage();
  const context = {
    console, Date, Error, JSON, Math, Object, Promise,
    dispatcherSyncQueueKey: 'mining-master-mobile-sync-queue-v3',
    dispatcherMobileSyncFlushDelayMs: 300,
    dispatcherSyncQueueFlushing: false,
    dispatcherSyncFlushTimer: null,
    dispatcherRoleIsReadonly: () => false,
    dispatcherInactiveRoleError: () => Object.assign(new Error('readonly'), {isServerResponse: true}),
    updateDispatcherSyncIndicator: () => {},
    setDispatcherSyncPending: (value) => effects.pending.push(Boolean(value)),
    markMiningMasterBoardStale: () => { effects.stale += 1; },
    refreshMobileBoardFromServer: () => { effects.mobileRefresh += 1; return Promise.resolve(true); },
    refreshDispatcherDesktopBoardFromServer: () => { effects.desktopRefresh += 1; return Promise.resolve(true); },
    showDispatcherNotice: (...args) => { effects.notice.push(args); return true; },
    sendDispatcherSyncRequest: () => Promise.resolve({ok: true}),
    effects, storage, timers,
  };
  context.window = {
    localStorage: storage,
    isAppRoleReadonly: () => false,
    setTimeout(callback, delay) { timers.push({callback, delay}); return timers.length; },
    clearTimeout() {},
  };
  const source = [
    'var dispatcherSyncQueueKey = "mining-master-mobile-sync-queue-v3";',
    'var dispatcherMobileSyncFlushDelayMs = 300;',
    'var dispatcherSyncQueueFlushing = false;',
    'var dispatcherSyncFlushTimer = null;',
    extractFunction('readDispatcherSyncQueue'),
    extractFunction('writeDispatcherSyncQueue'),
    extractFunction('enqueueDispatcherSyncRequest'),
    extractFunction('scheduleDispatcherSyncFlush'),
    extractFunction('dispatcherPostQueued'),
    extractFunction('showDispatcherDnDError'),
    extractFunction('flushDispatcherSyncQueue'),
  ].join('\n');
  vm.createContext(context);
  vm.runInContext(source, context, {filename: 'dispatcher-master-inline-r2.js'});
  return context;
}

function serverError(status, conflict) {
  return Object.assign(new Error(`HTTP ${status}`), {
    isServerResponse: true,
    status,
    code: status === 409 ? 'state_conflict' : 'temporary_server_error',
    conflict: Boolean(conflict),
  });
}

async function settle() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

test('master queued-first coalesces repeated truck commands and keeps latest payload', async () => {
  const runtime = createRuntime();
  await runtime.dispatcherPostQueued('/assign/', {truck_id: 7, action: 'assign', excavator_id: 1}, 9999);
  await runtime.dispatcherPostQueued('/assign/', {truck_id: 7, action: 'assign', excavator_id: 2}, 9999);
  const queue = runtime.readDispatcherSyncQueue();
  assert.equal(queue.length, 1);
  assert.equal(queue[0].data.excavator_id, 2);
  assert.equal(queue[0].attempts, 0);
});

test('master queued 409 is removed, not retried, and invokes conflict refresh paths', async () => {
  const runtime = createRuntime();
  await runtime.dispatcherPostQueued('/assign/', {truck_id: 7, action: 'assign'}, 9999);
  runtime.sendDispatcherSyncRequest = () => Promise.reject(serverError(409, true));
  runtime.flushDispatcherSyncQueue();
  await settle();
  assert.equal(runtime.readDispatcherSyncQueue().length, 0);
  assert.equal(runtime.effects.stale, 1);
  assert.equal(runtime.effects.notice.length, 1);
  assert.equal(runtime.effects.mobileRefresh, 2);
  assert.equal(runtime.effects.desktopRefresh, 1);
  assert.equal(runtime.timers.filter((item) => item.delay === 1200).length, 0);
  console.log('EVIDENCE_R2 master_409', JSON.stringify({removed: true, retry: false, notice_call: 1, stale: 1, mobile_refresh: 2, desktop_refresh: 1, dom_rendering: 'not_tested'}));
});

test('master queued temporary 503 is terminal in current runtime', async () => {
  const runtime = createRuntime();
  await runtime.dispatcherPostQueued('/assign/', {truck_id: 8, action: 'release'}, 9999);
  runtime.sendDispatcherSyncRequest = () => Promise.reject(serverError(503, false));
  runtime.flushDispatcherSyncQueue();
  await settle();
  assert.equal(runtime.readDispatcherSyncQueue().length, 0);
  assert.equal(runtime.effects.stale, 1);
  assert.equal(runtime.effects.notice.length, 1);
  assert.equal(runtime.effects.mobileRefresh, 1);
  assert.equal(runtime.effects.desktopRefresh, 0);
  assert.equal(runtime.timers.filter((item) => item.delay === 1200).length, 0);
  console.log('EVIDENCE_R2 master_503', JSON.stringify({removed: true, retry: false, notice_call: 1, mobile_refresh: 1, desktop_refresh: 0, dom_rendering: 'not_tested'}));
});

test('master network failure keeps command and schedules retry', async () => {
  const runtime = createRuntime();
  await runtime.dispatcherPostQueued('/assign/', {truck_id: 9, action: 'assign'}, 9999);
  runtime.sendDispatcherSyncRequest = () => Promise.reject(new TypeError('network down'));
  runtime.flushDispatcherSyncQueue();
  await settle();
  const queue = runtime.readDispatcherSyncQueue();
  assert.equal(queue.length, 1);
  assert.equal(queue[0].attempts, 1);
  assert.equal(runtime.effects.stale, 0);
  assert.equal(runtime.effects.mobileRefresh, 0);
  assert.equal(runtime.effects.desktopRefresh, 0);
  assert.equal(runtime.timers.filter((item) => item.delay === 1200).length, 1);
  console.log('EVIDENCE_R2 master_network', JSON.stringify({kept: true, attempts: 1, retry_scheduled: true, refresh: 0}));
});
