/* Personal avatar regression in a real Chromium profile. Every network request is intercepted. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {chromium} = require(process.env.COPPER_PLAYWRIGHT_MODULE || 'playwright');

const origin = 'https://mining-master.driverform.ru';
const root = '/mining-master/tablet/';
const assets = path.join(__dirname, '../profiles/mining_master_tablet/assets/tablet');
const runtime = ['field-archive.js', 'field-portrait.js'].map(name => fs.readFileSync(path.join(assets, name), 'utf8')).join('\n');
// Self-contained 1x1 true-colour PNGs with different pixels.
const png = [
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGMwKJgDAAIQAT0oaiiKAAAAAElFTkSuQmCC',
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNY46gBAAKyARZ79iRAAAAAAElFTkSuQmCC'
];
const data = id => 'data:image/png;base64,' + png[id - 1];
function actor(id) {
    return {id, access_id:10+id, key:'qa:'+id, generation:'g'+id, role:'mining_master', verified:true,
        full_name:'Тестовый Мастер', grant_until:new Date(Date.now()+86400000).toISOString()};
}
function html(source, own=true, person='Тестовый Мастер') {
    return `<!doctype html><html data-cr-field-local="true"><head><meta charset="utf-8">
        <meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; connect-src 'self'"></head>
        <body data-app-role-code="mining_master"><main class="dispatcher-board" data-dispatcher-own-shift-open="${own}">
        <header class="dispatcher-topbar"><div class="dispatcher-active-badge"><span class="dispatcher-active-avatar">${source ? '<img src="'+source+'">' : 'ТМ'}</span></div>
        <button name="action" value="end_shift" data-confirm-person="${person}">Завершить смену</button></header></main></body></html>`;
}
async function waitPhoto(page, expected) {
    await page.waitForFunction(src => {
        const img=document.querySelector('.dispatcher-active-avatar img');
        return img?.getAttribute('src')===src && !img.hidden && img.complete && img.naturalWidth===1;
    }, expected, {timeout:6000});
}
const noPhoto = page => page.waitForFunction(() => !document.querySelector('.dispatcher-active-avatar img'), null, {timeout:6000});
function observe(page, revision, via) {
    return page.evaluate(([rev,src]) => {
        const snapshot={actor:fixture.actor,shift:{is_open:true}};
        if(rev!==null)snapshot.portrait_revision=rev;
        return FieldPortrait.observe(fixture.actor,snapshot,src);
    }, [revision, via]);
}

(async () => {
    const profile=fs.mkdtempSync(path.join(os.tmpdir(),'copper-portrait-'));
    let ctx, page, employee=1, offline=false;
    const requests=[], errors=[], checks=[];
    async function start() {
        ctx=await chromium.launchPersistentContext(profile,{headless:true,serviceWorkers:'block'});
        await ctx.route('**/*',async route => {
            const req=route.request(),url=new URL(req.url());
            assert.equal(url.origin,origin);
            const workplace=(body)=>route.fulfill({contentType:'text/html; charset=utf-8',body});
            // Navigation models the already prepared local document. The original header
            // is initially missing an image because the login preceded opening the shift.
            if(req.isNavigationRequest() && url.pathname===root)return workplace(html(''));
            if(offline)return route.abort('internetdisconnected');
            requests.push(url.pathname);
            if(url.pathname===root)return workplace(html('/media/employee_photos/'+employee+'.png'));
            if(url.pathname==='/media/employee_photos/'+employee+'.png')
                return route.fulfill({contentType:'image/png',body:Buffer.from(png[employee-1],'base64')});
            return route.fulfill({status:404,body:''});
        });
        await ctx.addInitScript({content:`
            window.fixtureActor=${actor.toString()};
            window.fixture={actor:fixtureActor(${employee}),shift:{is_open:true},locked:false};
            window.counts={identity:0,report:0,portrait:0};
            window.FieldLedger={read:async()=>structuredClone(fixture)};
            window.FieldTransport={
                identify:async()=>{counts.identity++;return {actor:fixture.actor,snapshot:{shift:{is_open:true}}};},
                readReport:async()=>{counts.report++;throw Error('No personal HTTP cache on this test profile');},
                readPortrait:async a=>{counts.portrait++;return {employee_id:a.id,access_id:a.access_id,revision:fixture.rev,data:fixture.image};}
            };
            ${runtime}`});
        page=await ctx.newPage();
        page.on('pageerror',e=>errors.push(e.message));
        await page.goto(origin+root);
    }
    async function restart() { await ctx.close();await start(); }
    try {
        await start();
        await observe(page,null,'cookie');
        await waitPhoto(page,data(1));
        assert.ok(requests.includes('/media/employee_photos/1.png'));
        assert.equal(await page.evaluate(()=>counts.identity),2);
        checks.push('active authenticated header repairs the pre-open shell');
        const keys=await page.evaluate(async()=>{
            const op=indexedDB.open('cr-original-master-pages',1);await new Promise(r=>op.onsuccess=r);
            const request=op.result.transaction('static').objectStore('static').getAllKeys();
            return new Promise(r=>request.onsuccess=()=>r(request.result));
        });
        assert.ok(!keys.some(k=>String(k).includes('employee_photos')));
        checks.push('personal image is never in the shared static cache');

        offline=true;await restart();
        await waitPhoto(page,data(1));
        await observe(page,null,'device_ticket');
        assert.deepEqual(await page.evaluate(()=>counts),{identity:0,report:0,portrait:0});
        checks.push('same employee portrait survives a full offline process restart');
        await page.evaluate(()=>{
            document.documentElement.dataset.crFieldPortraitOwner=FieldArchive.scope(fixture.actor);
            fixture.actor=fixtureActor(2);dispatchEvent(new Event('fieldledgerchange'));
        });
        await noPhoto(page);
        employee=2;await restart();
        await page.evaluate(()=>FieldPortrait.paint());
        await noPhoto(page);
        checks.push('second employee cannot see the first employee photo');

        const rev='a'.repeat(64);
        await page.evaluate(([r,b])=>{fixture.rev=r;fixture.image=b;},[rev,data(2)]);
        await observe(page,rev,'device_ticket');await waitPhoto(page,data(2));
        checks.push('personal device-ticket response shows only employee B');
        await observe(page,'','device_ticket');await noPhoto(page);
        await restart();await page.evaluate(()=>FieldPortrait.paint());await noPhoto(page);
        checks.push('explicit server removal survives cold start');
        for(let n=0;n<3;n++){
            await page.evaluate(()=>dispatchEvent(new Event('online')));
            await observe(page,null,'device_ticket');
        }
        assert.deepEqual(await page.evaluate(()=>counts),{identity:0,report:1,portrait:0});
        checks.push('anonymous cache recovery is bounded and does not fetch media over the network');

        const migrated=await page.evaluate(async({a,b,markup})=>{
            const key=FieldArchive.scope(a),root=FieldArchive.root;
            const open=indexedDB.open('cr-original-master-pages',1);await new Promise(r=>open.onsuccess=r);
            const tx=open.result.transaction('pages','readwrite');
            tx.objectStore('pages').put({html:markup,scope:key,route:root},JSON.stringify([key,root]));
            await new Promise((r,j)=>{tx.oncomplete=r;tx.onabort=()=>j(tx.error);});
            const pick=(d,who)=>FieldPortrait.ownHeader(d,who)?.querySelector('img')?.getAttribute('src');
            return Promise.all([FieldArchive.findPortrait(a,pick),FieldArchive.findPortrait(b,pick)]);
        },{a:actor(1),b:actor(2),markup:html(data(1))});
        assert.deepEqual(migrated,[data(1),null]);
        checks.push('0.1.43 archived media remains bound to employee and access despite identical names');
        assert.deepEqual(errors,[]);
        console.log(JSON.stringify({passed:checks.length,checks}));
    } finally {
        if(ctx)await ctx.close();
        if(path.dirname(profile)===os.tmpdir() && path.basename(profile).startsWith('copper-portrait-'))
            fs.rmSync(profile,{recursive:true,force:true});
    }
})().catch(e=>{console.error(e);process.exitCode=1;});
