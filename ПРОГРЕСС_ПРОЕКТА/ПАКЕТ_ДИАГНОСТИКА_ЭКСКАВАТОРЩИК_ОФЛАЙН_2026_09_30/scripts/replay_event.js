// Sends one fixed offline event to a stand: node replay_event.js <port> <sessionid> <eventFile>
const http = require('http');
const fs = require('fs');

const [port, sessionId, eventFile] = process.argv.slice(2);
const event = JSON.parse(fs.readFileSync(eventFile, 'utf8'));
const csrf = 'qareplaycsrftoken0123456789abcde';
const body = JSON.stringify({
  protocol_version: 1,
  actor_id: event.actor_id,
  access_id: event.access_id,
  role_code: event.role_code,
  device_id: event.device_id,
  events: [event],
});
const req = http.request({
  host: '127.0.0.1', port: Number(port), path: '/offline-events/sync/', method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Accept': 'application/json',
    'X-Requested-With': 'XMLHttpRequest',
    'X-CSRFToken': csrf,
    'Cookie': `sessionid=${sessionId}; csrftoken=${csrf}`,
    'Referer': `http://localhost:${port}/excavator/work/`,
    'Origin': `http://localhost:${port}`,
    'Host': `localhost:${port}`,
    'Content-Length': Buffer.byteLength(body),
  },
}, res => {
  let data = '';
  res.on('data', c => data += c);
  res.on('end', () => {
    try {
      const r = JSON.parse(data).results[0];
      console.log(res.statusCode, JSON.stringify({status: r.status, code: r.code, message: r.message, server_ids: r.server_ids}));
    } catch (e) {
      console.log(res.statusCode, data.slice(0, 300));
    }
  });
});
req.on('error', e => console.log('ERR ' + e.message));
req.end(body);
