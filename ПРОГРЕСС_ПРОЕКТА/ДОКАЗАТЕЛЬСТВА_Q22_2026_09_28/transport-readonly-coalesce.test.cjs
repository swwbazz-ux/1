const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');

const sourcePath = process.env.Q22_TRANSPORT_SOURCE || path.resolve(
  __dirname, '../../СИСТЕМА_MVP/backend/static/js/dispatcher-transport-v1.js'
);
const source = fs.readFileSync(sourcePath);
const blob = crypto.createHash('sha1').update(`blob ${source.length}\0`).update(source).digest('hex');
assert.equal(blob, 'cb99c44ec9256d21181b5f54424438ff080de37e', 'exact REL transport required');

function harness() {
  const storage = new Map();
  let readonly = false, fetches = 0, timerId = 0;
  const window = {
    localStorage: {
      getItem: k => storage.has(k) ? storage.get(k) : null,
      setItem: (k, v) => storage.set(k, String(v)),
      removeItem: k => storage.delete(k),
    },
    isAppRoleReadonly: () => readonly,
    setTimeout: () => ++timerId,
    clearTimeout: () => {},
  };
  const context = vm.createContext({ window, Date, Math, Promise });
  vm.runInContext(source.toString('utf8'), context, { filename: 'dispatcher-transport-v1.js' });
  const transport = window.createDispatcherTransport({
    fetch: () => { fetches++; return Promise.reject(new Error('simulated network loss')); },
  });
  return { transport, storage, setReadonly: v => { readonly = v; }, fetches: () => fetches };
}

test('stored before readonly: retained, flush sends nothing', () => {
  const h = harness();
  assert.equal(h.transport.enqueue({ id: 'c1', kind: 'json', url: '/assign', data: { client_action_id: 'c1', target: 'EX-9' } }), true);
  const before = h.storage.get(h.transport.queueKey);
  h.setReadonly(true);
  h.transport.flush();
  assert.equal(h.fetches(), 0);
  assert.equal(h.storage.get(h.transport.queueKey), before);
  assert.equal(h.transport.readQueue()[0].id, 'c1');
});

test('new enqueue while readonly: rejected without changing an existing queue', () => {
  const h = harness();
  h.transport.enqueue({ id: 'c1', data: { target: 'EX-9' } });
  const before = h.storage.get(h.transport.queueKey);
  h.setReadonly(true);
  assert.equal(h.transport.enqueue({ id: 'c2', data: { target: 'EX-7' } }), false);
  assert.equal(h.storage.get(h.transport.queueKey), before);
});

test('explicit coalesceKey replaces payload and ID while retaining original createdAt', () => {
  const h = harness();
  h.transport.enqueue({ id: 'c1', createdAt: 100, coalesceKey: 'haul-truck-T1', data: { client_action_id: 'c1', target: 'EX-9' } });
  h.transport.enqueue({ id: 'c2', createdAt: 200, coalesceKey: 'haul-truck-T1', data: { client_action_id: 'c2', target: 'EX-7' } });
  const queue = h.transport.readQueue();
  assert.equal(queue.length, 1);
  assert.equal(queue[0].id, 'c2');
  assert.equal(queue[0].data.client_action_id, 'c2');
  assert.equal(queue[0].data.target, 'EX-7');
  assert.equal(queue[0].createdAt, 100);
});

test('desktop post network fallback creates two entries without coalesceKey', async () => {
  const h = harness();
  await h.transport.post('/assign', { client_action_id: 'c1', truck_id: 'T1', target: 'EX-9' });
  await h.transport.post('/assign', { client_action_id: 'c2', truck_id: 'T1', target: 'EX-7' });
  const queue = h.transport.readQueue();
  assert.equal(queue.length, 2);
  assert.equal(queue[0].data.client_action_id, 'c1');
  assert.equal(queue[1].data.client_action_id, 'c2');
  assert.equal(queue.every(x => !x.coalesceKey), true);
});
