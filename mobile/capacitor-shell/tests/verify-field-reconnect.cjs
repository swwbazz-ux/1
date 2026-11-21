// Exercise the original cookie-only realtime runtime against the prepared master
// screen. All traffic is intercepted locally and every signed read is verified.
const assert=require("node:assert/strict"),crypto=require("node:crypto"),fs=require("node:fs"),path=require("node:path");
const {chromium}=require(process.env.COPPER_PLAYWRIGHT_MODULE||"playwright");
const base=path.resolve(__dirname,"../../.."),assets=path.join(__dirname,"../profiles/mining_master_tablet/assets/tablet");
const origin="https://mining-master.driverform.ru",home="/mining-master/tablet/",login=home+"login/";
const realtime=fs.readFileSync(path.join(base,"СИСТЕМА_MVP/backend/static/js/realtime-client.js"),"utf8");
const source=name=>fs.readFileSync(path.join(assets,name+".js"),"utf8");
const actor={id:7,access_id:9,key:"qa:field-reconnect",generation:"test-generation",verified:true,role:"mining_master",
    grant_until:new Date(Date.now()+86400000).toISOString()};
const snapshot={actor,server_version:11,live:{excavators:[]},inventory:{trucks:[],excavators:[]},shift:{is_open:true,server_id:27,local_shift_id:"local-existing"}};
async function scenario(browser,mode){
    const ctx=await browser.newContext({serviceWorkers:"block"}),page=await ctx.newPage();
    const pending=mode.startsWith("pending");
    const cached=mode.startsWith("cached");
    let publicKey,device,signed=0,realtimeHits=0,foreignHits=0,retried=0;
    let terminal=null;
    const nonces=new Set(),sourceUrl=origin+"/static/qa-realtime.js";
    const json=(route,status,data)=>route.fulfill({status,contentType:"application/json",body:JSON.stringify(data)});
    try{
        await ctx.route("**/*",async route=>{
            const req=route.request(),u=new URL(req.url());
            assert.equal(u.origin,origin);
            if(u.pathname==="/seed"||u.pathname===login)return route.fulfill({contentType:"text/html",body:"<html>entry</html>"});
            if(req.url()===sourceUrl)return route.fulfill({contentType:"text/javascript",body:realtime});
            if(u.pathname===home)return route.fulfill({contentType:"text/html",body:'<!doctype html><html '+(mode==="not-archive"?"":'data-cr-field-local="true"')+
                '><body data-app-role-code="mining_master"><div class="dispatcher-board" data-operational-state-version="11"></div>'+
                '<script>window.AppRealtimeConfig={stateUrl:"/realtime/state/",loginUrl:"'+login+'",initialVersion:11,screens:[{name:"tablet",role:"mining_master",mode:"custom",path:"^/mining-master/tablet/"}]};</script>'+
                '<script src="'+sourceUrl+'"></script></body></html>'});
            if(u.pathname==="/realtime/state/"){
                realtimeHits++;
                if(mode==="foreign"){foreignHits++;return json(route,200,{authenticated:true,role_active:true,active_role_code:"mining_master",
                    role_app_code:"mining_master",session_role_code:"mining_master",session_revision:"other",version:900,events:[{id:999}]});}
                return json(route,401,{authenticated:false});
            }
            if(u.pathname!==home+"field-sync/")return route.abort();
            if(req.method()==="GET"&&!u.searchParams.has("challenge"))return json(route,403,{code:"authentication_required"});
            if(u.searchParams.get("challenge")==="1"){
                const nonce=crypto.randomUUID();nonces.add(nonce);
                return json(route,200,{protocol:"master-field/2",challenge:nonce,server_time:new Date().toISOString(),csrf_token:"qa-csrf"});
            }
            const v=req.postDataJSON(),bytes=Buffer.from(v.signed_bytes,"base64url"),envelope=JSON.parse(bytes);
            if((pending && v.event?.type==="shift.open") || (mode==="cached-queue" && v.event?.type==="card.settings")){
                assert.equal(envelope.id,v.event.id);assert(envelope.local_shift_id);
                assert(crypto.verify("sha256",bytes,{key:crypto.createPublicKey({key:publicKey,format:"jwk"}),dsaEncoding:"ieee-p1363"},Buffer.from(v.signature,"base64url")));
                retried++;
                const status=mode==="pending-conflict"?"conflict":"retry";
                return json(route,status==="retry"?503:409,{...v.event,status,message:"Unresolved test opening"});
            }
            assert.equal(v.op,"snapshot");assert.equal(v.ticket,"test-grant");assert.equal(envelope.type,"snapshot.read");
            assert.equal(envelope.device_id,device);assert.equal(envelope.actor_key,actor.key);assert(nonces.delete(envelope.challenge));
            assert(crypto.verify("sha256",bytes,{key:crypto.createPublicKey({key:publicKey,format:"jwk"}),dsaEncoding:"ieee-p1363"},Buffer.from(v.signature,"base64url")));
            signed++;
            if(mode==="revoked"||terminal==="revoked")return json(route,403,{code:"access_revoked",status:"auth"});
            return json(route,200,{protocol:"master-field/2",snapshot:{...snapshot,
                blocking_shift:mode==="pending-blocked"?{server_id:88}:null,shift:{...snapshot.shift,is_open:mode!=="closed"&&terminal!=="closed"&&!pending}}});
        });
        await ctx.addInitScript({content:source("field-ledger")});
        await page.goto(origin+"/seed");
        const identity=await page.evaluate(async({actor,snapshot,pending})=>{
            const id=await FieldLedger.deviceIdentity();await FieldLedger.selectActor(actor);await FieldLedger.ingest(snapshot);
            if(pending)await FieldLedger.openShift(actor);
            await FieldLedger.putTicket(actor.key,actor.generation,"test-grant");return id;
        },{actor,pending,snapshot:pending?{...snapshot,shift:{is_open:false,server_id:null}}:snapshot});
        publicKey=identity.public_key;device=identity.device_id;
        assert.equal((await ctx.cookies()).length,0);
        await ctx.addInitScript({content:'window.offset=0;const now=performance.now.bind(performance);Object.defineProperty(performance,"now",{value:()=>now()+window.offset});\n'+source("field-transport")+"\n"+source("field-live-actions")});
        await page.goto(origin+home);
        if(mode==="active"||mode==="foreign"||cached){
            await page.waitForFunction(()=>window.AppRealtime?.getDebugState?.().lastSuccessAt>0 ||
                (window.FieldTransport?.status.last_snapshot_at&&window.AppRealtime?.getDebugState?.().connectionState==="ok"),null,{timeout:12000});
            const original=await page.evaluate(async()=>{
                const res=await fetch("/realtime/state/?include_events=1&role_app_code=mining_master");
                const rawCookie=await fetch("/realtime/state/?include_events=0&role_app_code=mining_master");
                return {p:await res.json(),cookieStatus:rawCookie.status,navigation:location.pathname,ended:AppRealtime.getDebugState().authEnded,
                    shift:(await FieldLedger.read()).shift};
            });
            assert.equal(original.navigation,home);assert.equal(original.ended,false);assert(original.shift.is_open);
            assert.equal(original.p.session_revision,actor.generation);assert.equal(original.p.version,11);
            assert.deepEqual(original.p.events,[]);assert.equal(original.cookieStatus,mode==="foreign"?200:401);
            assert(signed>0&&realtimeHits>0);if(mode==="foreign")assert(foreignHits>0);
            if(cached){
                const first=signed;
                const poll=()=>page.evaluate(async()=>{const res=await fetch("/realtime/state/?include_events=1&role_app_code=mining_master");
                    return [res.status,res.headers.get("X-Field-Source")];});
                for(let i=0;i<4;i++){
                    await page.evaluate(()=>{window.offset+=5200;});
                    assert.deepEqual(await poll(),[200,"verified-snapshot"]);
                }
                assert.equal(signed,first,"Repeated 5s polls downloaded a full snapshot");
                if(mode==="cached-expiry"){
                    terminal="closed";await page.evaluate(()=>{window.offset+=5500;AppRealtime.poll();});
                    await page.waitForURL("**"+login);assert(signed>first);
                    assert.equal((await page.evaluate(()=>FieldLedger.read())).shift.is_open,false);
                }else if(mode==="cached-reconnect"){
                    terminal="revoked";await page.evaluate(()=>{dispatchEvent(new Event("offline"));dispatchEvent(new Event("online"));});
                    await page.waitForURL("**"+login);assert(signed>first);
                    assert((await page.evaluate(()=>FieldLedger.read())).locked);
                }else{
                    await page.evaluate(()=>FieldLedger.command("card.settings",{key:"fixture"}));
                    await poll();assert(signed>first&&retried>0,"Queue should bypass a fresh cache");
                }
            }
        }else if(pending && mode!=="pending-blocked"){
            await page.waitForFunction(()=>AppRealtime?.getDebugState().pollInFlight===false || FieldTransport.status.last_snapshot_at,null,{timeout:12000});
            const s=await page.evaluate(async()=>{
                const r=await fetch("/realtime/state/?include_events=1&role_app_code=mining_master");
                const ledger=await FieldLedger.read(),event=ledger.outbox.find(e=>e.type==="shift.open");
                return {status:r.status,url:location.pathname,ended:AppRealtime.getDebugState().authEnded,receipt:ledger.receipts[event.id],
                    shift:ledger.shift,same:event.local_shift_id===ledger.shift.local_shift_id,entry:event.status};
            });
            assert(retried>0&&signed>0);assert.equal(s.url,home);assert.equal(s.status,409);assert.equal(s.ended,false);
            assert(s.same&&s.shift.is_open&&!s.shift.server_id&&!s.receipt);
            assert.equal(s.entry,mode==="pending-conflict"?"conflict":"retry");
        }else{
            await page.waitForURL("**"+login,{timeout:12000});
            const s=await page.evaluate(()=>FieldLedger.read());
            if(mode==="revoked"){assert(signed>0);assert(s.locked);assert.equal(s.actors[actor.key].verified,false);}
            if(mode==="closed"){assert(signed>0);assert.equal(s.shift.is_open,false);}
            if(mode==="pending-blocked"){assert(signed>0&&retried>0);assert.equal(s.outbox[0].type,"shift.open");}
            if(mode==="not-archive")assert.equal(signed,0);
        }
        return {mode,signed,realtimeHits,result:"pass"};
    }finally{await ctx.close();}
}
(async()=>{
    const browser=await chromium.launch({headless:true});
    try{for(const mode of ["active","foreign","closed","revoked","not-archive","pending-retry","pending-conflict","pending-blocked",
        "cached-expiry","cached-reconnect","cached-queue"])console.log(JSON.stringify(await scenario(browser,mode)));}
    finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
