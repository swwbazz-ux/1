import fs from 'node:fs';
import http from 'node:http';
import https from 'node:https';

const args = new Map();
for (let index = 2; index < process.argv.length; index += 2) {
  args.set(process.argv[index], process.argv[index + 1]);
}

const listenPort = Number(args.get('--listen-port') || '18462');
const upstreamPort = Number(args.get('--upstream-port') || '18460');
const certPath = args.get('--cert');
const keyPath = args.get('--key');
const logPath = args.get('--log');

if (!certPath || !keyPath || !logPath) {
  throw new Error('Required: --cert, --key, --log');
}

function log(record) {
  fs.appendFileSync(
    logPath,
    `${JSON.stringify({ at: new Date().toISOString(), ...record })}\n`,
    'utf8',
  );
}

const server = https.createServer(
  {
    cert: fs.readFileSync(certPath),
    key: fs.readFileSync(keyPath),
  },
  (request, response) => {
    const upstream = http.request(
      {
        hostname: '127.0.0.1',
        port: upstreamPort,
        method: request.method,
        path: request.url,
        headers: {
          ...request.headers,
          'x-forwarded-proto': 'https',
          'x-forwarded-host': request.headers.host || '',
        },
      },
      (upstreamResponse) => {
        response.writeHead(upstreamResponse.statusCode || 502, upstreamResponse.headers);
        upstreamResponse.pipe(response);
        log({
          method: request.method,
          path: request.url,
          status: upstreamResponse.statusCode || 502,
        });
      },
    );

    upstream.on('error', (error) => {
      log({ method: request.method, path: request.url, error: error.message });
      if (!response.headersSent) {
        response.writeHead(502, { 'content-type': 'text/plain; charset=utf-8' });
      }
      response.end('QA1 upstream unavailable');
    });

    request.pipe(upstream);
  },
);

server.listen(listenPort, '127.0.0.1', () => {
  log({ event: 'listening', listenPort, upstreamPort });
});

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => {
    server.close(() => process.exit(0));
  });
}
