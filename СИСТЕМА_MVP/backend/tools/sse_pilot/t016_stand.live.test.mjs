/**
 * T-016 WP-2 local stand probe.
 *
 * Local-only harness (not product code). Run with:
 *
 *   node --test tools/sse_pilot/t016_stand.live.test.mjs
 *
 * It assumes the three T-016 stand processes are already running:
 *   8001  uvicorn config.sse_asgi:application          (restricted ASGI)
 *   8005  manage.py runserver --insecure               (WSGI pages)
 *   8006  uvicorn tools.sse_pilot.static_asgi:application (T-origin, full ASGI)
 *
 * Credentials are never hard-coded; the authenticated checks read them from
 * SSE_STAND_PHONE / SSE_STAND_ACCESS_CODE and report SKIP when unset. The
 * browser step uses a real Edge/Chrome (CDP) because Node 24 has no global
 * WHATWG EventSource.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const SPLIT = 'http://127.0.0.1:8001';
const WSGI = 'http://127.0.0.1:8005';
const ORIGIN = 'http://127.0.0.1:8006';
const BROWSER_CANDIDATES = [
  process.env.SSE_STAND_BROWSER,
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
].filter(Boolean);

async function probe(url, options = {}) {
  try {
    const response = await fetch(url, { redirect: 'manual', ...options });
    return { response };
  } catch (error) {
    return { error };
  }
}

test('T-split: /realtime/stream/ on 8001 without cookie -> 401', async (t) => {
  const result = await probe(`${SPLIT}/realtime/stream/`);
  if (result.error) { t.skip(`8001 unreachable: ${result.error.message}`); return; }
  assert.equal(result.response.status, 401);
  assert.match(result.response.headers.get('content-type') || '', /application\/json/);
  const body = await result.response.json();
  assert.equal(body.authenticated === true, false);
  assert.equal(body.error, 'authentication_required');
  console.log('401 body:', JSON.stringify(body));
});

test('T-split: foreign path on 8001 -> 404 sse_endpoint_only', async (t) => {
  const result = await probe(`${SPLIT}/driver/`);
  if (result.error) { t.skip(`8001 unreachable: ${result.error.message}`); return; }
  assert.equal(result.response.status, 404);
  const body = await result.response.json();
  assert.equal(body.error, 'sse_endpoint_only');
  console.log('404 body:', JSON.stringify(body));
});

test('T-split: WSGI pages answer on 8005 and issue CSRF cookie', async (t) => {
  const result = await probe(`${WSGI}/`);
  if (result.error) { t.skip(`8005 unreachable: ${result.error.message}`); return; }
  assert.equal(result.response.status, 200);
  const html = await result.response.text();
  assert.match(html, /unified-login-form/);
  const cookies = result.response.headers.getSetCookie?.() || [];
  console.log('WSGI /login/ 200, cookies:', cookies.map((value) => value.split('=')[0]));
  const staticHref = html.match(/(?:href|src)="(?:\.\/)?(static\/[^"]+)"/);
  console.log('rendered static URL example:', staticHref ? staticHref[1] : 'none');
});

test('T-origin: same-origin static is served on 8006 only by the stand wrapper', async (t) => {
  const origin = await probe(`${ORIGIN}/static/js/realtime-client.js`);
  if (origin.error) { t.skip(`8006 unreachable: ${origin.error.message}`); return; }
  assert.equal(origin.response.status, 200);
  const body = await origin.response.text();
  assert.match(body, /EventSource/);
  const restricted = await probe(`${SPLIT}/static/js/realtime-client.js`);
  assert.equal(restricted.response.status, 404);
  console.log('static bytes:', body.length, '| 8001 static ->', restricted.response.status);
});

function parseSetCookies(headers) {
  const raw = headers.getSetCookie?.() || [];
  const jar = {};
  for (const line of raw) {
    const [pair] = line.split(';');
    const index = pair.indexOf('=');
    if (index > 0) jar[pair.slice(0, index).trim()] = pair.slice(index + 1).trim();
  }
  return jar;
}

function cookieHeader(jar) {
  return Object.entries(jar).map(([key, value]) => `${key}=${value}`).join('; ');
}

test('one session: WSGI login cookie is accepted by ASGI /realtime/stream/', async (t) => {
  const phone = process.env.SSE_STAND_PHONE;
  const code = process.env.SSE_STAND_ACCESS_CODE;
  const roleAppCode = process.env.SSE_STAND_ROLE_APP_CODE || 'driver';
  if (!phone || !code) {
    t.skip('SSE_STAND_PHONE / SSE_STAND_ACCESS_CODE not set (setup_sse_stand data absent)');
    return;
  }
  const loginPage = await probe(`${WSGI}/`);
  assert.ok(loginPage.response && loginPage.response.status === 200);
  const html = await loginPage.response.text();
  const jar = parseSetCookies(loginPage.response.headers);
  const tokenMatch = html.match(/name="csrfmiddlewaretoken" value="([^"]+)"/);
  assert.ok(tokenMatch, 'csrf token missing on login page');

  const form = new URLSearchParams({
    phone,
    access_code: code,
    action: 'login',
    csrfmiddlewaretoken: tokenMatch[1],
  });
  const login = await fetch(`${WSGI}/`, {
    method: 'POST',
    redirect: 'manual',
    headers: {
      'content-type': 'application/x-www-form-urlencoded',
      cookie: cookieHeader(jar),
      referer: `${WSGI}/`,
    },
    body: form.toString(),
  });
  const loginJar = { ...jar, ...parseSetCookies(login.headers) };
  console.log('WSGI login status:', login.status, 'cookies:', Object.keys(loginJar));
  assert.ok(loginJar.sessionid, 'sessionid was not issued by WSGI login');

  const stream = await fetch(`${SPLIT}/realtime/stream/?applied=0&after=0&role_app_code=${encodeURIComponent(roleAppCode)}`, {
    headers: { cookie: cookieHeader(loginJar), accept: 'text/event-stream' },
  });
  console.log('ASGI stream with WSGI cookie:', stream.status, stream.headers.get('content-type'), 'role:', roleAppCode);
  assert.equal(stream.status, 200);
  const reader = stream.body.getReader();
  const { value } = await reader.read();
  await reader.cancel();
  const firstFrame = new TextDecoder().decode(value || new Uint8Array());
  console.log('first frame:', firstFrame.split('\n').slice(0, 3).join(' | '));
  assert.match(firstFrame, /^event: (heartbeat|operational|reset)/m);
});

function findBrowser() {
  for (const candidate of BROWSER_CANDIDATES) {
    if (candidate && existsSync(candidate)) return candidate;
  }
  return null;
}

test('browser: real EventSource present and same-origin page loads', { timeout: 120_000 }, async (t) => {
  const browser = findBrowser();
  if (!browser) { t.skip('no Edge/Chrome binary found'); return; }
  const profileDir = mkdtempSync(join(tmpdir(), 't016-edge-'));
  const child = spawn(browser, [
    '--headless=new',
    '--disable-gpu',
    '--no-first-run',
    '--no-default-browser-check',
    '--remote-debugging-port=9223',
    `--user-data-dir=${profileDir}`,
    'about:blank',
  ], { stdio: 'ignore' });
  try {
    let target = null;
    for (let attempt = 0; attempt < 40 && !target; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 250));
      try {
        const list = await fetch('http://127.0.0.1:9223/json/list').then((r) => r.json());
        target = list.find((item) => item.type === 'page') || null;
      } catch { /* not ready yet */ }
    }
    assert.ok(target, 'CDP endpoint did not open');
    const socket = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      socket.addEventListener('open', resolve, { once: true });
      socket.addEventListener('error', reject, { once: true });
    });
    let seq = 0;
    const pending = new Map();
    socket.addEventListener('message', (event) => {
      const message = JSON.parse(event.data);
      if (message.id && pending.has(message.id)) {
        pending.get(message.id)(message);
        pending.delete(message.id);
      }
    });
    const send = (method, params = {}) => new Promise((resolve) => {
      const id = ++seq;
      pending.set(id, resolve);
      socket.send(JSON.stringify({ id, method, params }));
    });

    await send('Runtime.enable');
    await send('Page.enable');
    await send('Page.navigate', { url: `${ORIGIN}/` });
    await new Promise((resolve) => setTimeout(resolve, 2500));
    const evaluated = await send('Runtime.evaluate', {
      expression: `JSON.stringify({
        eventSource: typeof window.EventSource,
        readyState: document.readyState,
        forms: document.querySelectorAll('form').length,
        transport: document.body && document.body.dataset.realtimeTransport || '',
        styles: Array.from(document.styleSheets).map((sheet) => sheet.href || 'inline').slice(0, 3),
        scriptSrc: Array.from(document.scripts).map((node) => node.src).filter(Boolean).slice(0, 3)
      })`,
      returnByValue: true,
      awaitPromise: false,
    });
    const info = JSON.parse(evaluated.result.result.value);
    console.log('browser:', info);
    assert.equal(info.eventSource, 'function');
    assert.equal(info.readyState, 'complete');
    assert.ok(info.forms > 0, 'login form did not render');
    assert.ok(info.scriptSrc.length > 0, 'no external scripts resolved');
    for (const src of info.scriptSrc) {
      assert.doesNotMatch(src, /\/login\/static\//, `relative static URL broken: ${src}`);
    }
    assert.ok(
      info.styles.some((href) => href === 'inline' || /\/static\/css\//.test(href)),
      `stylesheets unresolved: ${JSON.stringify(info.styles)}`,
    );
    socket.close();
  } finally {
    child.kill();
    await new Promise((resolve) => setTimeout(resolve, 500));
    rmSync(profileDir, { recursive: true, force: true });
  }
});

async function withBrowser(port, run) {
  const browser = findBrowser();
  if (!browser) { throw Object.assign(new Error('no Edge/Chrome binary found'), { skip: true }); }
  const profileDir = mkdtempSync(join(tmpdir(), 't016-edge-'));
  const child = spawn(browser, [
    '--headless=new',
    '--disable-gpu',
    '--no-first-run',
    '--no-default-browser-check',
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profileDir}`,
    'about:blank',
  ], { stdio: 'ignore' });
  try {
    let target = null;
    for (let attempt = 0; attempt < 40 && !target; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 250));
      try {
        const list = await fetch(`http://127.0.0.1:${port}/json/list`).then((r) => r.json());
        target = list.find((item) => item.type === 'page') || null;
      } catch { /* not ready yet */ }
    }
    if (!target) throw new Error('CDP endpoint did not open');
    const socket = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      socket.addEventListener('open', resolve, { once: true });
      socket.addEventListener('error', reject, { once: true });
    });
    let seq = 0;
    const pending = new Map();
    socket.addEventListener('message', (event) => {
      const message = JSON.parse(event.data);
      if (message.id && pending.has(message.id)) {
        pending.get(message.id)(message);
        pending.delete(message.id);
      }
    });
    const send = (method, params = {}) => new Promise((resolve) => {
      const id = ++seq;
      pending.set(id, resolve);
      socket.send(JSON.stringify({ id, method, params }));
    });
    await send('Runtime.enable');
    await send('Page.enable');
    const evaluate = async (expression) => {
      const result = await send('Runtime.evaluate', {
        expression,
        returnByValue: true,
        awaitPromise: true,
      });
      if (result.result.exceptionDetails) {
        throw new Error(result.result.exceptionDetails.text || 'evaluate failed');
      }
      return result.result.result.value;
    };
    try {
      await run({ send, evaluate });
    } finally {
      socket.close();
    }
  } finally {
    child.kill();
    await new Promise((resolve) => setTimeout(resolve, 500));
    rmSync(profileDir, { recursive: true, force: true });
  }
}

test('browser: real EventSource streams to an authenticated driver session and gets one live event', { timeout: 180_000 }, async (t) => {
  const phone = process.env.SSE_STAND_PHONE;
  const code = process.env.SSE_STAND_ACCESS_CODE;
  if (!phone || !code) {
    t.skip('SSE_STAND_PHONE / SSE_STAND_ACCESS_CODE not set');
    return;
  }
  if ((process.env.SSE_STAND_ROLE_APP_CODE || 'driver') !== 'driver') {
    t.skip('browser live-event step is driver-scoped (downtime action)');
    return;
  }
  const downtimeReasonId = process.env.SSE_STAND_DOWNTIME_REASON_ID || '8';
  try {
    await withBrowser(9224, async ({ evaluate }) => {
      await evaluate(`window.location.href = ${JSON.stringify(`${ORIGIN}/`)}; true`);
      await new Promise((resolve) => setTimeout(resolve, 2500));

      const login = await evaluate(`(async () => {
        const form = document.querySelector('[data-validated-login]');
        if (!form) return JSON.stringify({error: 'login form missing'});
        const data = new FormData(form);
        data.set('phone', ${JSON.stringify(phone)});
        data.set('access_code', ${JSON.stringify(code)});
        data.set('action', 'login');
        const response = await fetch('/', {
          method: 'POST', body: data, credentials: 'same-origin', redirect: 'manual',
        });
        return JSON.stringify({status: response.status, type: response.type});
      })()`);
      console.log('browser login:', login);
      assert.doesNotMatch(login, /login form missing/);

      const started = await evaluate(`(async () => {
        const state = await fetch('/realtime/state/?include_events=0&role_app_code=driver', {
          credentials: 'same-origin',
        }).then((response) => response.json());
        window.__t016 = {events: [], readyState: -1, errors: 0, serverVersion: state.version};
        const source = new EventSource(
          '/realtime/stream/?applied=0&after=0&role_app_code=driver',
          {withCredentials: true},
        );
        window.__t016.source = source;
        source.onopen = () => { window.__t016.readyState = source.readyState; };
        source.onerror = () => { window.__t016.errors += 1; };
        source.addEventListener('operational', (event) => {
          window.__t016.events.push({name: 'operational', id: event.lastEventId, size: event.data.length, at: Date.now(), data: JSON.parse(event.data)});
        });
        source.addEventListener('heartbeat', (event) => {
          window.__t016.events.push({name: 'heartbeat', id: event.lastEventId, size: event.data.length, at: Date.now(), data: JSON.parse(event.data)});
        });
        source.addEventListener('reset', (event) => {
          window.__t016.events.push({name: 'reset', id: event.lastEventId, size: event.data.length, at: Date.now(), data: JSON.parse(event.data)});
        });
        return JSON.stringify({serverVersion: state.version, roleActive: state.role_active});
      })()`);
      console.log('event source started:', started);
      assert.match(started, /"serverVersion"/);

      const snapshot = await evaluate(`new Promise((resolve) => {
        const deadline = Date.now() + 8000;
        const poll = () => {
          const frame = window.__t016.events.find((item) => item.name === 'operational'
            && Array.isArray(item.data.events) && item.data.events.length > 0);
          if (frame) {
            resolve(JSON.stringify({id: frame.id, version: frame.data.version, count: frame.data.events.length, size: frame.size}));
            return;
          }
          if (Date.now() > deadline) { resolve(JSON.stringify({timeout: true, names: window.__t016.events.map((item) => item.name)})); return; }
          setTimeout(poll, 100);
        };
        poll();
      })`);
      console.log('snapshot frame:', snapshot);
      assert.doesNotMatch(snapshot, /"timeout":true/, `no snapshot page: ${snapshot}`);

      const action = await evaluate(`(async () => {
        const csrf = (document.cookie.split('; ').find((row) => row.startsWith('csrftoken=')) || '').split('=')[1] || '';
        window.__t016.t0 = Date.now();
        const response = await fetch('/driver/downtime/', {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'content-type': 'application/json',
            'accept': 'application/json',
            'x-requested-with': 'XMLHttpRequest',
            'x-csrftoken': decodeURIComponent(csrf),
          },
          body: JSON.stringify({action: 'start', reason_id: ${Number(downtimeReasonId)}}),
        });
        return JSON.stringify({status: response.status, t0: window.__t016.t0, body: await response.text()});
      })()`);
      console.log('business action:', action.slice(0, 300));

      const delivered = await evaluate(`new Promise((resolve) => {
        const deadline = Date.now() + 12000;
        const poll = () => {
          const frame = window.__t016.events.find((item) => item.name === 'operational'
            && Array.isArray(item.data.events)
            && item.data.events.some((event) => event.type === 'downtime_changed'
              && Number(event.version) > window.__t016.serverVersion));
          if (frame) { resolve(JSON.stringify(frame)); return; }
          if (Date.now() > deadline) {
            resolve(JSON.stringify({timeout: true, seen: window.__t016.events.map((item) => item.name)}));
            return;
          }
          setTimeout(poll, 200);
        };
        poll();
      })`);
      console.log('delivered frame:', delivered.slice(0, 400));

      await evaluate(`(async () => {
        try { window.__t016.source.close(); } catch (error) { /* ignore */ }
        const csrf = (document.cookie.split('; ').find((row) => row.startsWith('csrftoken=')) || '').split('=')[1] || '';
        await fetch('/driver/downtime/', {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'content-type': 'application/json',
            'accept': 'application/json',
            'x-requested-with': 'XMLHttpRequest',
            'x-csrftoken': decodeURIComponent(csrf),
          },
          body: JSON.stringify({action: 'close'}),
        });
        return 'closed';
      })()`);

      const frame = JSON.parse(delivered);
      assert.equal(frame.timeout, undefined, `no live event delivered: ${delivered}`);
      assert.equal(frame.name, 'operational');
      const downtime = frame.data.events.find((event) => event.type === 'downtime_changed'
        && Number(event.version) > JSON.parse(started).serverVersion);
      assert.ok(downtime, 'live downtime_changed event missing from the frame');
      assert.equal(downtime.payload.equipment_id, 1);
      console.log('live event version:', downtime.version, '| frame id:', frame.id, '| commit->receive ms:', frame.at - JSON.parse(action).t0);

      const traffic = await evaluate(`JSON.stringify({
        names: window.__t016.events.map((item) => item.name),
        heartbeats: window.__t016.events.filter((item) => item.name === 'heartbeat').map((item) => ({id: item.id, size: item.size})),
        errors: window.__t016.errors
      })`);
      console.log('event source traffic:', traffic);
      const summary = JSON.parse(traffic);
      assert.ok(summary.heartbeats.length >= 1, `no heartbeat frame: ${traffic}`);
      assert.equal(summary.errors, 0, `EventSource errors: ${traffic}`);
    });
  } catch (error) {
    if (error.skip) { t.skip(error.message); return; }
    throw error;
  }
});
