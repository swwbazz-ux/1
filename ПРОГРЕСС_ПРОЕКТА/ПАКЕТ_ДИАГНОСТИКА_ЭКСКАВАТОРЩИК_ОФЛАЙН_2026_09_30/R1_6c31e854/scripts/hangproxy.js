// Proxy <listen> -> <target>; requests whose path matches <regex> are accepted and never answered (one hung resource).
const http = require('http');
const [listen, target, pattern] = process.argv.slice(2);
const re = new RegExp(pattern);
http.createServer((req, res) => {
  if (re.test(req.url)) { console.log('HUNG ' + req.url); return; }
  const p = http.request({host: '127.0.0.1', port: Number(target), path: req.url, method: req.method, headers: req.headers}, r => {
    res.writeHead(r.statusCode, r.headers); r.pipe(res);
  });
  p.on('error', () => { res.writeHead(502); res.end(); });
  req.pipe(p);
}).listen(Number(listen), '127.0.0.1', () => console.log('hangproxy ' + listen + ' -> ' + target + ' hang ' + pattern));
