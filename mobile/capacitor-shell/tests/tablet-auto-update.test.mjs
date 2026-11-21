import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../profiles/mining_master_tablet/assets/tablet/field-identity.js', import.meta.url), 'utf8');
const login = '/mining-master/tablet/login/';
const token = '7cd577fa-f06c-47ac-a3f2-7a608c137034';

function load({ state = {}, read, path = login, inputs = [], focused = false } = {}) {
  const messages = [];
  const events = new Map();
  const document = {
    readyState: 'loading',
    activeElement: { matches: () => focused },
    addEventListener: (name, callback) => events.set(name, callback),
    querySelector: () => null,
    querySelectorAll: () => inputs,
  };
  const window = {
    addEventListener() {}, fetch() {},
    CopperFieldPage: { postMessage: (value) => messages.push(JSON.parse(value)) },
    FieldLedger: { read: read ?? (async () => ({
      storage_ok: true, snapshot_ready: true, locked: true, actor: null,
      pending_count: 0, shift: { is_open: false }, shifts: {}, ...state,
    })) },
  };
  const location = { pathname: path };
  vm.runInNewContext(source, { window, document, location, TextEncoder,
    HTMLFormElement: class {}, crypto: { subtle: { digest: async () => new Uint8Array(32).buffer } } });
  document.readyState = 'complete';
  return { identity: window.FieldIdentity, messages, submit: events.get('submit') };
}

test('does not reserve an update while any local shift is open', async () => {
  for (const state of [{ shift: { is_open: true } }, { shifts: { earlier: { is_open: true } } }]) {
    const { identity, messages } = load({ state });
    await identity.reserveUpdate(token);
    assert.equal(messages[0].state, 'shift');
  }
});

test('defers for pending actions, missing journal, active board or typed credentials', async () => {
  for (const config of [
    { state: { pending_count: 1 } }, { state: { storage_ok: false } },
    { state: { snapshot_ready: false } }, { state: { locked: false } },
    { read: async () => { throw new Error('journal unavailable'); } },
    { path: '/mining-master/tablet/' }, { focused: true },
    { inputs: [{ name: 'phone', value: '9' }] }, { inputs: [{ name: 'access_code', value: '1' }] },
  ]) {
    const { identity, messages } = load(config);
    await identity.reserveUpdate(token);
    assert.equal(messages[0].state, 'waiting');
  }
});

test('reserves only the empty handover screen and gates login until the reservation is released', async () => {
  const { identity, messages, submit } = load({ inputs: [{ name: 'phone', value: '+7' }] });
  await identity.reserveUpdate(token);
  assert.deepEqual(messages, [{ op: 'tablet_update_window', token, state: 'ready' }]);
  await assert.rejects(identity.signIn('9000000012', '123456'), /Устанавливается обновление/);
  let prevented = false;
  submit({ preventDefault: () => { prevented = true; }, stopImmediatePropagation() {} });
  assert.equal(prevented, true);
  identity.releaseUpdate(token);
  await identity.reserveUpdate(token);
  assert.equal(messages.length, 2);
});
