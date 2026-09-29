// CDP helper for the offline acceptance run: node cdpx.js <cdpPort> <urlPart|new> <cmd> ...args
const http = require('http');
const net = require('net');
const crypto = require('crypto');
const fs = require('fs');

function httpJson(port, path, method) {
  return new Promise((resolve, reject) => {
    const req = http.request({host: '127.0.0.1', port, path, method: method || 'GET'}, res => {
      let body = '';
      res.on('data', c => body += c);
      res.on('end', () => { try { resolve(JSON.parse(body)); } catch (e) { reject(new Error(body)); } });
    });
    req.on('error', reject);
    req.end();
  });
}

function connect(wsUrl) {
  const m = wsUrl.match(/^ws:\/\/([^:/]+):(\d+)(\/.*)$/);
  const [, host, port, path] = m;
  const key = crypto.randomBytes(16).toString('base64');
  const sock = net.connect(Number(port), host);
  return new Promise((resolve, reject) => {
    sock.on('error', reject);
    sock.on('connect', () => {
      sock.write(`GET ${path} HTTP/1.1\r\nHost: ${host}:${port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: ${key}\r\nSec-WebSocket-Version: 13\r\n\r\n`);
    });
    let buf = Buffer.alloc(0);
    let upgraded = false;
    const waiters = new Map();
    sock.on('data', chunk => {
      buf = Buffer.concat([buf, chunk]);
      if (!upgraded) {
        const idx = buf.indexOf('\r\n\r\n');
        if (idx < 0) return;
        buf = buf.slice(idx + 4);
        upgraded = true;
        resolve({send, close: () => sock.destroy()});
      }
      for (;;) {
        if (buf.length < 2) return;
        const len0 = buf[1] & 0x7f;
        let off = 2, len = len0;
        if (len0 === 126) { if (buf.length < 4) return; len = buf.readUInt16BE(2); off = 4; }
        else if (len0 === 127) { if (buf.length < 10) return; len = Number(buf.readBigUInt64BE(2)); off = 10; }
        if (buf.length < off + len) return;
        const payload = buf.slice(off, off + len).toString('utf8');
        buf = buf.slice(off + len);
        let msg; try { msg = JSON.parse(payload); } catch (e) { continue; }
        if (msg.id && waiters.has(msg.id)) { waiters.get(msg.id)(msg); waiters.delete(msg.id); }
      }
    });
    let nextId = 1;
    function send(method, params, timeoutMs) {
      const id = nextId++;
      const frame = Buffer.from(JSON.stringify({id, method, params: params || {}}), 'utf8');
      const mask = crypto.randomBytes(4);
      const masked = Buffer.from(frame);
      for (let i = 0; i < masked.length; i++) masked[i] ^= mask[i % 4];
      let header;
      if (frame.length < 126) header = Buffer.from([0x81, 0x80 | frame.length]);
      else if (frame.length < 65536) { header = Buffer.alloc(4); header[0] = 0x81; header[1] = 0x80 | 126; header.writeUInt16BE(frame.length, 2); }
      else { header = Buffer.alloc(10); header[0] = 0x81; header[1] = 0x80 | 127; header.writeBigUInt64BE(BigInt(frame.length), 2); }
      sock.write(Buffer.concat([header, mask, masked]));
      return new Promise((res, rej) => {
        waiters.set(id, res);
        setTimeout(() => { if (waiters.has(id)) { waiters.delete(id); rej(new Error('timeout: ' + method)); } }, timeoutMs || 15000);
      });
    }
  });
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function evaluate(client, expression) {
  const res = await client.send('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true}, 20000);
  if (res.result && res.result.exceptionDetails) {
    return 'ОШИБКА: ' + JSON.stringify(res.result.exceptionDetails.exception || res.result.exceptionDetails).slice(0, 600);
  }
  const value = res.result && res.result.result ? res.result.result.value : undefined;
  return typeof value === 'string' ? value : JSON.stringify(value);
}

(async () => {
  const [cdpPort, urlPart, cmd, ...args] = process.argv.slice(2);
  let page;
  if (urlPart === 'new') {
    page = await httpJson(cdpPort, '/json/new?' + encodeURI(args[0] || 'about:blank'), 'PUT');
    console.log('NEW ' + page.id);
    if (cmd === 'none') process.exit(0);
  } else {
    const pages = (await httpJson(cdpPort, '/json')).filter(p => p.type === 'page');
    page = pages.find(p => (p.url || '').includes(urlPart)) || (urlPart === 'any' ? pages[0] : null);
  }
  if (!page) { console.log('НЕТ СТРАНИЦЫ'); process.exit(1); }
  const client = await connect(page.webSocketDebuggerUrl);
  await client.send('Emulation.setTouchEmulationEnabled', {enabled: true, maxTouchPoints: 5});
  const touch = (type, x, y) => client.send('Input.dispatchTouchEvent', {
    type, touchPoints: type === 'touchEnd' ? [] : [{x, y, radiusX: 4, radiusY: 4, force: 1}],
  });
  if (cmd === 'eval') {
    console.log(await evaluate(client, args[0]));
  } else if (cmd === 'waitfor') {
    const deadline = Date.now() + Number(args[1] || 10000);
    let last = '';
    while (Date.now() < deadline) {
      last = await evaluate(client, args[0]);
      if (last && last !== 'false' && last !== 'null' && last !== 'undefined' && last !== '""' && !last.startsWith('ОШИБКА')) break;
      await sleep(250);
    }
    console.log(last);
  } else if (cmd === 'nav') {
    await client.send('Page.navigate', {url: args[0]});
    console.log('nav ok');
  } else if (cmd === 'cookie') {
    const r = await client.send('Network.setCookie', {name: 'sessionid', value: args[0], domain: 'localhost', path: '/', httpOnly: true});
    console.log(JSON.stringify(r.result));
  } else if (cmd === 'tap') {
    const x = Number(args[0]), y = Number(args[1]);
    await touch('touchStart', x, y);
    await sleep(Number(args[2] || 60));
    await touch('touchEnd', x, y);
    console.log('tap ok');
  } else if (cmd === 'hold') {
    const x = Number(args[0]), y = Number(args[1]);
    await touch('touchStart', x, y);
    const until = Date.now() + Number(args[2] || 1300);
    while (Date.now() < until) { await sleep(100); await touch('touchMove', x, y); }
    await touch('touchEnd', x, y);
    console.log('hold ok');
  } else if (cmd === 'drag') {
    const [x0, y0, x1, y1] = args.slice(0, 4).map(Number);
    const steps = Number(args[4] || 12);
    await touch('touchStart', x0, y0);
    for (let i = 1; i <= steps; i++) {
      await sleep(30);
      await touch('touchMove', x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps);
    }
    await sleep(40);
    await touch('touchEnd', x1, y1);
    console.log('drag ok');
  } else if (cmd === 'shot') {
    const r = await client.send('Page.captureScreenshot', {format: 'png'}, 20000);
    fs.writeFileSync(args[0], Buffer.from(r.result.data, 'base64'));
    console.log('shot ' + args[0]);
  } else if (cmd === 'cookies') {
    const r = await client.send('Network.getCookies', {urls: [args[0]]});
    console.log(JSON.stringify((r.result.cookies || []).map(c => [c.name, c.value, c.domain, c.expires])));
  } else if (cmd === 'clearstore') {
    const r = await client.send('Storage.clearDataForOrigin', {
      origin: args[0], storageTypes: args[1] || 'indexeddb,local_storage,session_storage',
    });
    console.log('clearstore ' + JSON.stringify(r.result || r.error));
  } else if (cmd === 'reload') {
    await client.send('Page.reload', {ignoreCache: false});
    console.log('reload ok');
  }
  client.close();
  process.exit(0);
})().catch(e => { console.log('СБОЙ: ' + e.message); process.exit(1); });
