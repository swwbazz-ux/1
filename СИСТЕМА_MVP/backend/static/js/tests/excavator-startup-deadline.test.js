const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.resolve(__dirname,'../../../trips/views.py'),'utf8')
    .match(/EXCAVATOR_SERVICE_WORKER_JS = r"""([\s\S]*?)"""/)[1];
const origin='https://mine.test';
const shellPath='/excavator/work/';
const shellHtml='<main data-eo-shell data-eo-role-code="excavator_operator">Saved shift</main>';
function response(body=shellHtml, url=shellPath, status=200, type='text/html') {
    const r=new Response(body,{status,headers:{'Content-Type':type}});
    Object.defineProperty(r,'url',{value:origin+url});
    const clone=r.clone.bind(r);
    r.clone=()=>{const c=clone();Object.defineProperty(c,'url',{value:origin+url});return c;};
    return r;
}
function deferred(){let resolve;const promise=new Promise(r=>resolve=r);return{promise,resolve};}
async function flush(){for(let i=0;i<12;i++)await new Promise(resolve=>setImmediate(resolve));}
function harness(fetcher, options={}) {
    const timers=new Map(), entries=new Map(), handlers={}, writes=[];
    let next=0, requests=0, aborted=false;
    const key=r=>typeof r==='string'?new URL(r,origin).href:r.url;
    if(options.cached!==false)entries.set(origin+shellPath,response());
    const cache={
        match:r=>options.hangMatch?new Promise(()=>{}):Promise.resolve(entries.get(key(r))||null),
        put:async(r,value)=>{writes.push(key(r));entries.set(key(r),value);},
    };
    const context={
        Response,Request,URL,AbortController,console,
        setTimeout(fn,delay){const id=++next;timers.set(id,{fn,delay});return id;},
        clearTimeout(id){timers.delete(id);},
        self:{location:{origin},addEventListener(name,fn){handlers[name]=fn;}},
        caches:{open:()=>options.hangCache?new Promise(()=>{}):Promise.resolve(cache)},
        fetch:async(request,init)=>{requests++;if(init?.signal)init.signal.addEventListener('abort',()=>{aborted=true;});return fetcher(request,init);},
    };
    vm.createContext(context);vm.runInContext(source,context);
    return {context,entries,writes,timers,
        get requests(){return requests;},get aborted(){return aborted;},
        fire(delay){for(const [id,t]of [...timers])if(t.delay===delay){timers.delete(id);t.fn();}},
        start(url=shellPath,headers={}){
            const request=new Request(origin+url,{headers});
            let result;
            handlers.fetch({request,respondWith(value){result=value;}});
            return result;
        },
    };
}

test('prepared shell opens after hung headers, aborts network and releases the deadline',async()=>{
    const h=harness(()=>new Promise(()=>{}));const result=h.start();await flush();
    h.fire(8000);await flush();
    assert.match(await (await result).text(),/Saved shift/);
    assert.equal(h.aborted,true);assert.equal(h.timers.size,0);
});

test('headers without a complete body fall back; late completion cannot replace the cache',async()=>{
    const body=deferred();
    const hanging={ok:true,status:200,url:origin+shellPath,headers:new Headers({'Content-Type':'text/html'}),
        clone:()=>({arrayBuffer:()=>body.promise,text:async()=>shellHtml.replace('Saved','Late')})};
    const h=harness(()=>hanging);const result=h.start();await flush();
    h.fire(8000);await flush();
    assert.match(await (await result).text(),/Saved shift/);
    body.resolve(new ArrayBuffer(0));await flush();
    assert.deepEqual(h.writes,[]);
});

test('missing shell gives a finite 503 and a later healthy request succeeds',async()=>{
    let healthy=false;
    const h=harness(()=>healthy?response():new Promise(()=>{}),{cached:false});
    const first=h.start();await flush();h.fire(8000);await flush();
    assert.equal((await first).status,503);
    healthy=true;
    assert.equal((await h.start()).status,200);
    await flush();assert.ok(h.writes.length>0);
});

for(const failure of ['hangCache','hangMatch'])test('fallback '+failure+' is bounded after network failure',async()=>{
    const h=harness(()=>Promise.reject(Error('offline')),{[failure]:true});
    const result=h.start();await flush();h.fire(2500);await flush();
    assert.equal((await result).status,503);assert.equal(h.timers.size,0);
});

test('cached scripts load after hung network and redirected HTML is never cached as a script',async()=>{
    let bad=false;
    const h=harness(()=>bad?response('Login','/start/'):new Promise(()=>{}));
    h.entries.set(origin+'/static/app.js',response('saved-script','/static/app.js',200,'text/javascript'));
    const pending=h.start('/static/app.js');await flush();h.fire(8000);await flush();
    assert.equal(await (await pending).text(),'saved-script');
    bad=true;await h.start('/static/app.js');await flush();
    assert.deepEqual(h.writes,[]);
});

test('XHR never gets saved shell; a hung request yields a finite 503',async()=>{
    const h=harness(()=>new Promise(()=>{}));
    const result=h.start(shellPath,{'X-Requested-With':'XMLHttpRequest'});
    await flush();h.fire(8000);await flush();
    assert.equal((await result).status,503);assert.deepEqual(h.writes,[]);
});

test('server 503 uses saved shell but explicit authentication failure does not',async()=>{
    let status=503;
    const h=harness(()=>response('Access denied',shellPath,status));
    assert.match(await (await h.start()).text(),/Saved shift/);
    status=403;
    assert.equal((await h.start()).status,403);
});

test('invalid cached role is not served as an excavator shell',async()=>{
    const h=harness(()=>Promise.reject(Error('offline')));
    h.entries.set(origin+shellPath,response('<main data-eo-shell data-eo-role-code="driver">wrong role</main>'));
    assert.equal((await h.start()).status,503);
});

test('healthy navigation does not wait for a stalled cache',async()=>{
    const h=harness(()=>response(),{hangCache:true});
    const result=await h.start();
    assert.equal(result.status,200);assert.equal(h.timers.size,0);
});


test('deadline still returns the shell when AbortController is unavailable',async()=>{
    const h=harness(()=>new Promise(()=>{}));h.context.AbortController=undefined;
    const result=h.start();await flush();h.fire(8000);await flush();
    assert.match(await (await result).text(),/Saved shift/);
    assert.equal(h.timers.size,0);
});
