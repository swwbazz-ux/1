// Accepts TCP connections on the given ports and never answers: simulates a weak signal.
const net = require('net');
for (const port of process.argv.slice(2).map(Number)) {
  net.createServer(socket => { socket.on('error', () => {}); }).listen(port, '127.0.0.1', () => console.log('blackhole on ' + port));
}
