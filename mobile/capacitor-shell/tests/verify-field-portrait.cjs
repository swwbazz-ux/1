/* Portrait regressions in a real durable Chromium profile. The network is fully intercepted. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {chromium} = require(process.env.COPPER_PLAYWRIGHT_MODULE || 'playwright');

const origin = 'https://mining-master.driverform.ru';
const root = '/mining-master/tablet/';
const assets = path.join(__dirname, '../profiles/mining_master_tablet/assets/tablet');
const runtime = ['field-archive.js','field-reports.js','tablet-planning-cache.js','field-portrait.js']
    .map(name=>fs.readFileSync(path.join(assets,name),'utf8')).join('\n');
// Different single-pixel PNGs; both actors intentionally have the same name.
const png = [
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGMwKJgDAAIQAT0oaiiKAAAAAElFTkSuQmCC',
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNY46gBAAKyARZ79iRAAAAAAElFTkSuQmCC'
];
const data = id => 'data:image/png;base64,' + png[id - 1];
const rev = id => String(id).repeat(64);
function actor(id) {
    return {id,access_id:10+id,key:'qa:'+id,generation:'g'+id,role:'mining_master',verified:true,
        full_name:'Тестовый Мастер',grant_until:new Date(Date.now()+86400000).toISOString()};
}
function shift(id) { return {is_open:true,local_shift_id:'shift-'+id,server_id:100+id}; }
function html(source='') {
    return `<!doctype html><html data-cr-field-local="true"><head><meta charset="utf-8">
      <meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; connect-src 'self'"></head>
      <body data-app-role-code="mining_master"><main class="dispatcher-board" data-dispatcher-own-shift-open="true">
      <header class="dispatcher-topbar"><div class="dispatcher-active-badge"><span class="dispatcher-active-avatar">${source?'<img src="'+source+'">':'ТМ'}</span></div>
      <button name="action" value="end_shift" data-confirm-person="Тестовый Мастер">Завершить смену</button></header>
      <article id="keep-workplace">Сохранённый комплекс</article></main></body></html>`;
}
async function waitPhoto(page, expected) {
    await page.waitForFunction(src=>{
        const img=document.querySelector('.dispatcher-active-avatar img');
        return img?.getAttribute('src')===src&&!img.hidden&&img.complete&&img.naturalWidth===1;
    },expected,{timeout:6000});
}
async function noPhoto(page) {
    await page.waitForFunction(()=>!document.querySelector('.dispatcher-active-avatar img'),null,{timeout:6000});
}
async function seedLegacy(ctx) {
    const page=await ctx.newPage();
    await page.goto(origin+'/old-profile');
    const rows=[1,2].map(id=>({scope:JSON.stringify(['qa:'+id,id,10+id]),bytes:data(id),html:html(data(id))}));
    await page.evaluate(async({rows,root})=>{
        async function put(dbName,stores,write,version=1,keyPath=false) {
            const db=await new Promise((ok,no)=>{
                const op=indexedDB.open(dbName,version);op.onerror=()=>no(op.error);
                op.onupgradeneeded=()=>stores.forEach(k=>op.result.createObjectStore(k,keyPath?{keyPath:'key'}:{}));
                op.onsuccess=()=>ok(op.result);
            });
            const tx=db.transaction(stores,'readwrite');write(tx);
            await new Promise((ok,no)=>{tx.oncomplete=ok;tx.onabort=()=>no(tx.error);});db.close();
        }
        await put('cr-master-personal-portraits',['portraits'],tx=>{
            rows.forEach(row=>tx.objectStore('portraits').put({data:row.bytes,key:row.scope},row.scope));
        });
        await put('cr-original-master-pages',['pages','static'],tx=>{
            for(const row of rows)for(const route of [root,root+'dispatcher/shift-log/']){
                tx.objectStore('pages').put({scope:row.scope,route,html:row.html,at:1},JSON.stringify([row.scope,route]));
            }
            tx.objectStore('static').put('nonpersonal-icon','/static/equipment.png');
        });
        await put('cr-master-scoped-reports',['files','pages','requests','sizes'],tx=>{
            tx.objectStore('pages').put({key:'qa-report',html:rows[0].html,at:1});
            tx.objectStore('requests').put({key:'keep-request',path:root+'reports/volume/'});
        },2,true);
        await put('cr-tablet-board-cache',['board'],tx=>{
            tx.objectStore('board').put({at:1,markup:rows[1].html},'latest');
        });
    },{rows,root});
    await page.close();
}
async function verifyStorage(page) {
    return page.evaluate(async()=>{
        const dbs=await indexedDB.databases();
        const stored=dbs.some(x=>x.name==='cr-master-personal-portraits');
        const db=await new Promise((ok,no)=>{const op=indexedDB.open('cr-original-master-pages');op.onsuccess=()=>ok(op.result);op.onerror=()=>no(op.error);});
        const tx=db.transaction(['pages','static']);
        const read=r=>new Promise((ok,no)=>{r.onsuccess=()=>ok(r.result);r.onerror=()=>no(r.error);});
        const [pages,icons]=await Promise.all([read(tx.objectStore('pages').getAll()),read(tx.objectStore('static').getAll())]);db.close();
        async function get(dbName,shelf,key){
            const db=await new Promise((ok,no)=>{const req=indexedDB.open(dbName);req.onsuccess=()=>ok(req.result);req.onerror=()=>no(req.error);});
            const v=await read(db.transaction(shelf).objectStore(shelf).get(key));db.close();return v;
        }
        const report=await get('cr-master-scoped-reports','pages','qa-report');
        const queued=await get('cr-master-scoped-reports','requests','keep-request');
        const board=await get('cr-tablet-board-cache','board','latest');
        const markup=[...pages.map(p=>p.html),report?.html,board?.markup];
        return {stored,count:pages.length,containsPhoto:markup.some(p=>/data:image\/png;base64|\/media\/employee_photos\//i.test(p)),
            preserved:markup.every(p=>p?.includes('Сохранённый комплекс'))&&icons.includes('nonpersonal-icon')&&!!queued};
    });
}

(async()=>{
    const profile=fs.mkdtempSync(path.join(os.tmpdir(),'copper-portrait-'));
    let ctx,page,devtools;
    const errors=[],checks=[];
    async function context(offline) {
        ctx=await chromium.launchPersistentContext(profile,{headless:true,serviceWorkers:'block'});
        await ctx.route('**/*',async route=>{
            const req=route.request(),url=new URL(req.url());assert.equal(url.origin,origin);
            if(req.isNavigationRequest())return route.fulfill({contentType:'text/html; charset=utf-8',body:html()});
            if(offline)return route.abort('internetdisconnected');
            if(url.pathname===root)return route.fulfill({contentType:'text/html; charset=utf-8',body:html('/media/employee_photos/1.png')});
            if(url.pathname==='/media/employee_photos/1.png')return route.fulfill({contentType:'image/png',body:Buffer.from(png[0],'base64')});
            return route.fulfill({status:404,body:''});
        });
    }
    async function connection(isOffline) {
        // An explicit Android/Chromium link-state transition sets navigator.onLine as well as network IO.
        await devtools.send('Network.emulateNetworkConditions',{offline:isOffline,latency:0,downloadThroughput:-1,uploadThroughput:-1});
        await page.waitForFunction(expected=>navigator.onLine===expected,!isOffline,{timeout:4000});
    }
    async function boot(offline, id=1) {
        await context(offline);
        await ctx.addInitScript({content:`
            const makeActor=${actor.toString()},makeShift=${shift.toString()};
            const images=${JSON.stringify([data(1),data(2)])};
            window.fixture={actor:makeActor(${id}),shift:makeShift(${id}),locked:false};
            window.changeShift=(actorId,shiftId)=>{
                fixture.actor=makeActor(actorId);fixture.shift=makeShift(shiftId);
                document.documentElement.dataset.crFieldPortraitOwner=FieldArchive.scope(fixture.actor);
                dispatchEvent(new Event('fieldledgerchange'));
            };
            window.revision=String(${id}).repeat(64);
            window.deviceReads=0;window.onlineSignals=0;window.holdRead=false;
            window.mediaRequests=[];
            const fetchPage=window.fetch.bind(window);
            window.fetch=(url,init={})=>{
                if(new URL(url,location.href).pathname.startsWith('/media/employee_photos/'))
                    mediaRequests.push({policy:init.cache,header:new Headers(init.headers).get('X-Copper-Field-Portrait')});
                return fetchPage(url,init);
            };
            window.FieldLedger={read:async()=>structuredClone(fixture)};
            window.FieldTransport={
                identify:async()=>({actor:fixture.actor,snapshot:{shift:fixture.shift}}),
                readPortrait:async a=>{
                    deviceReads++;const response={employee_id:a.id,access_id:a.access_id,revision,data:images[a.id-1]};
                    if(holdRead)await new Promise(ok=>{window.finishRead=ok;});
                    return response;
                }
            };
            window.receiveSnapshot=()=>FieldPortrait.observe(fixture.actor,{actor:fixture.actor,shift:{...fixture.shift},
                portrait_revision:revision},'device_ticket');
            // Same handoff as the real transport: a connectivity signal wakes the field
            // exchange, and its authenticated snapshot is supplied to the portrait module.
            addEventListener('online',()=>{onlineSignals++;receiveSnapshot();});
            ${runtime}`});
        page=await ctx.newPage();devtools=await ctx.newCDPSession(page);await devtools.send('Network.enable');
        await connection(offline);page.on('pageerror',e=>errors.push(e.message));await page.goto(origin+root);
        await connection(offline);
        await page.evaluate(async()=>{await FieldArchive.has(fixture.actor);FieldPortrait.paint();});
    }
    try {
        await context(false);await seedLegacy(ctx);await ctx.close();
        await boot(true);
        assert.equal(await page.evaluate(()=>navigator.onLine),false);
        await noPhoto(page);await page.waitForFunction(()=>document.querySelector('.dispatcher-active-avatar').textContent.trim()==='ТМ');
        checks.push('cold process with no connection opens the active shift with initials');
        let old;
        for(let i=0;i<60;i++){
            old=await verifyStorage(page);if(!old.stored&&!old.containsPhoto)break;
            await new Promise(ok=>setTimeout(ok,100));
        }
        assert.deepEqual(old,{stored:false,count:4,containsPhoto:false,preserved:true});
        checks.push('0.1.44 photo records and images in all saved shifts removed, work pages and assets preserved');
        await connection(false);await waitPhoto(page,data(1));
        assert.equal(await page.evaluate(()=>onlineSignals),1);
        for(let i=0;i<3;i++)await page.evaluate(()=>receiveSnapshot());
        assert.equal(await page.evaluate(()=>deviceReads),1);
        checks.push('connectivity event loads current portrait; repeated snapshots do not download it twice');
        assert.deepEqual(await verifyStorage(page),old);
        await ctx.close();await boot(true);await noPhoto(page);await connection(false);await waitPhoto(page,data(1));
        checks.push('full offline restart has initials and automatically reloads the photo after reconnect');

        await page.evaluate(()=>{revision='3'.repeat(64);holdRead=true;window.pending=receiveSnapshot();});
        await page.waitForFunction(()=>typeof finishRead==='function');
        await page.evaluate(()=>{changeShift(2,2);revision='2'.repeat(64);holdRead=false;});
        await noPhoto(page);
        await page.evaluate(async()=>{finishRead();await pending;});await noPhoto(page);
        await page.evaluate(()=>receiveSnapshot());await waitPhoto(page,data(2));
        checks.push('late response for previous employee cannot populate new shift with identical name');
        await page.evaluate(()=>{revision='4'.repeat(64);holdRead=true;window.pending=receiveSnapshot();});
        await page.waitForFunction(()=>{return typeof finishRead==='function'&&deviceReads>=4;});
        await page.evaluate(()=>{fixture.shift.is_open=false;dispatchEvent(new Event('fieldledgerchange'));});
        await noPhoto(page);
        await page.evaluate(async()=>{changeShift(2,3);holdRead=false;finishRead();await pending;});
        await noPhoto(page);
        checks.push('closing a shift drops its photo and rejects a late response even for the same employee');
        await page.evaluate(()=>{revision='2'.repeat(64);return receiveSnapshot();});await waitPhoto(page,data(2));
        await page.evaluate(()=>{revision='';return receiveSnapshot();});await noPhoto(page);
        assert.deepEqual(await verifyStorage(page),old);
        checks.push('server removal clears the visible photo; there are no new durable portrait bytes');
        await ctx.close();await boot(false);
        await page.evaluate(()=>FieldPortrait.observe(fixture.actor,{actor:fixture.actor,shift:fixture.shift},'cookie'));
        await waitPhoto(page,data(1));assert.deepEqual(await page.evaluate(()=>mediaRequests),[{policy:'no-store',header:'1'}]);
        await ctx.close();await boot(true);await noPhoto(page);assert.deepEqual(await verifyStorage(page),old);
        checks.push('old server header loads authenticated photo uncached and cold offline start shows initials');
        assert.deepEqual(errors,[]);
        console.log(JSON.stringify({passed:checks.length,checks}));
    } finally {
        if(ctx)await ctx.close();
        if(path.dirname(profile)===os.tmpdir()&&path.basename(profile).startsWith('copper-portrait-'))
            fs.rmSync(profile,{recursive:true,force:true});
    }
})().catch(e=>{console.error(e);process.exitCode=1;});
