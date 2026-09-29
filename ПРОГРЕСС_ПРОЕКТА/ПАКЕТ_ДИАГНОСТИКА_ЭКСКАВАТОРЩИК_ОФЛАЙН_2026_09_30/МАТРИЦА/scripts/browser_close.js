// node browser_close.js <cdpPort> — Browser.close over the browser websocket
const src = require('fs').readFileSync(__dirname + '/cdpx.js', 'utf8');
const http = require('http');
const connect = new Function('require', "const net=require('net'),crypto=require('crypto');" + src.slice(src.indexOf('function connect'), src.indexOf('const sleep')) + 'return connect;')(require);
http.get(`http://127.0.0.1:${process.argv[2]}/json/version`, r => {
  let d = ''; r.on('data', c => d += c).on('end', async () => {
    const c = await connect(JSON.parse(d).webSocketDebuggerUrl);
    c.send('Browser.close').catch(() => {});
    setTimeout(() => { console.log('closed'); process.exit(0); }, 1500);
  });
});
