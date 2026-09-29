// Answers every request with "200 OK" headers and then never sends the body: headers-without-body network.
const net = require('net');
const port = Number(process.argv[2]);
net.createServer(socket => {
  socket.on('error', () => {});
  socket.once('data', () => {
    socket.write('HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: 500000\r\nConnection: keep-alive\r\n\r\n<!doctype html><html><head>');
  });
}).listen(port, '127.0.0.1', () => console.log('halfhole on ' + port));
