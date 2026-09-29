const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, 'source/СИСТЕМА_MVP/backend/trips/views.py'), 'utf8');
const marker = 'EXCAVATOR_SERVICE_WORKER_JS = r"""';
const start = source.indexOf(marker) + marker.length;
assert.ok(start >= marker.length);
const worker = source.slice(start, source.indexOf('"""', start));
const url = 'https://excavator.test/excavator/work/';
const tick = () => new Promise(resolve => setTimeout(resolve, 10));
function setup(headersFirst) {
  const timers = new Map(); let seq = 0, matches = 0, settled = false, body;
  const cached = new Response('<main data-eo-shell data-eo-role-code="excavator_operator">cached</main>', {headers:{'content-type':'text/html'}});
  const context = vm.createContext({URL, Request, Response, AbortController,
    self:{location:{origin:'https://excavator.test'},addEventListener:()=>{}},
    setTimeout:fn=>{timers.set(++seq,fn);return seq},clearTimeout:id=>timers.delete(id),
    caches:{open:async()=>({match:async()=>{matches++;return cached},put:async()=>{}})},
    fetch:async(request,options)=>{
      if (!headersFirst) return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new Error('aborted'))));
      const stream = new ReadableStream({start(c){body=c; c.enqueue(new TextEncoder().encode('<main data-eo-shell '));}});
      options.signal.addEventListener('abort',()=>body.error(new Error('aborted')));
      const response = new Response(stream,{headers:{'content-type':'text/html'}});
      Object.defineProperty(response,'url',{value:url});
      return response;
    }});
  vm.runInContext(worker,context);
  const pending = vm.runInContext(`networkFirst(new Request('${url}'), '/excavator/work/', isExcavatorShellResponse)`, context);
  pending.then(()=>{settled=true});
  return {pending,cached,timers,get matches(){return matches},get settled(){return settled},finish:()=>body && body.error(new Error('probe cleanup'))};
}
test('control: no response headers -> deadline returns prepared cache',async()=>{
  const h=setup(false); await tick(); assert.equal(h.timers.size,1);
  for(const fn of h.timers.values()) fn();
  assert.equal(await h.pending,h.cached); assert.equal(h.matches,1);
});
test('SW1: headers arrived but body stalled -> same deadline must return prepared cache',async()=>{
  const h=setup(true); await tick();
  for(const fn of h.timers.values()) fn();
  await tick();
  const observed={active_deadlines:h.timers.size,settled:h.settled,cache_reads:h.matches};
  console.log('SW1',JSON.stringify(observed));
  h.finish(); await h.pending;
  assert.equal(observed.settled,true,'navigation remains pending after deadline; timer ended at headers');
});
