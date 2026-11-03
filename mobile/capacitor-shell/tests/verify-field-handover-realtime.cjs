// Pin the first role poll after an offline handover. Event ACK snapshots carry
// personal identity but no new grant, unlike successful cookie enrollment.
const assert=require("node:assert/strict"),path=require("node:path");
const {createHash}=require("node:crypto");
const {chromium}=require(process.env.COPPER_PLAYWRIGHT_MODULE||"playwright");
const assets=path.join(__dirname,"../profiles/mining_master_tablet/assets/tablet");
const home="https://mining-master.driverform.ru/mining-master/tablet/";
const deadline=new Date(Date.now()+86400000).toISOString();
const actor=id=>({id,access_id:id+10,role:"mining_master",verified:true,key:"qa-master-"+id,generation:"qa-generation-"+id});
const a=actor(1),b=actor(2),plan={excavators:[]},fleet={excavators:[],trucks:[]};
const snapshot=(actor,version,open,id,local=null)=>({actor,server_version:version,live:plan,inventory:fleet,
    shift:{is_open:open,server_id:id,local_shift_id:local}});
const closed=snapshot(b,1,false,null);

async function handover(browser,holdLock){
    const ctx=await browser.newContext({serviceWorkers:"block"}),page=await ctx.newPage();
    let server=closed;
    const reads=[],events=[],postAckGrants=[];
    try{
        await ctx.route("**/*",async route=>{
            const req=route.request(),url=new URL(req.url()),json=(status,data)=>route.fulfill({status,contentType:"application/json",body:JSON.stringify(data)});
            if(req.url()===home)return route.fulfill({contentType:"text/html",body:'<!doctype html><html data-cr-field-local="true"><body></body></html>'});
            if(url.pathname==="/realtime/state/")return json(401,{authenticated:false});
            if(!url.pathname.endsWith("/field-sync/"))return route.abort();
            if(req.method()==="GET"){
                if(!url.searchParams.has("challenge"))return json(403,{code:"authentication_required"});
                return json(200,{protocol:"master-field/2",challenge:"qa-challenge",csrf_token:"csrf",server_time:new Date().toISOString()});
            }
            const v=req.postDataJSON();
            if(v.op==="snapshot"){
                assert.equal(v.ticket,"ticket-"+b.key);
                if(events.length===2)postAckGrants.push(await page.evaluate(async()=>(await FieldLedger.read()).actor.grant_until));
                const reply=server;reads.push(reply.server_version);
                if(holdLock && reads.length===1)await page.evaluate(()=>window.releaseQueueLock());
                return json(200,{protocol:"master-field/2",snapshot:reply});
            }
            const e=v.event;
            assert.equal(v.ticket,"ticket-"+e.actor_key);
            assert.equal(JSON.parse(Buffer.from(v.signed_bytes,"base64url")).id,e.id);assert(v.signature);
            events.push([e.type,e.actor_id]);
            const isOpen=e.type==="shift.open",person=isOpen?b:a,serverId=isOpen?22:21;
            const ack=snapshot(person,isOpen?3:2,isOpen,serverId,e.local_shift_id);
            if(isOpen)server=ack;
            return json(200,{id:e.id,status:"applied",actor_key:person.key,actor_id:person.id,
                actor_access_id:person.access_id,server_shift_id:serverId,snapshot:ack});
        });
        await page.goto(home);
        await page.addScriptTag({path:path.join(assets,"field-ledger.js")});
        await page.evaluate(async({a,b,deadline,first,holdLock})=>{
            const personA={...a,grant_until:deadline},personB={...b,grant_until:deadline};
            await FieldLedger.selectActor(personA);await FieldLedger.ingest(first);await FieldLedger.putTicket(a.key,a.generation,"ticket-"+a.key);
            await FieldLedger.closeShift();await FieldLedger.lock();await FieldLedger.selectActor(personB);
            await FieldLedger.putTicket(b.key,b.generation,"ticket-"+b.key);await FieldLedger.openShift(personB);
            if(holdLock)await new Promise(ready=>navigator.locks.request("cr-master-signed-outbox",()=>new Promise(resolve=>{
                window.releaseQueueLock=resolve;ready();
            })));
        },{a,b,deadline,holdLock,first:snapshot({...a,grant_until:deadline},1,true,21)});
        await page.addScriptTag({path:path.join(assets,"field-transport.js")});
        const result=await page.evaluate(async()=>{
            const poll=()=>FieldTransport.personalRealtime("/realtime/state/?include_events=1&role_app_code=mining_master");
            const first=await poll(),state=await FieldLedger.read(),second=await poll();
            return {first:first.status,second:second.status,grant:state.actor.grant_until,shift:state.shift,queued:state.outbox.length,
                receiptCount:Object.keys(state.receipts).length};
        });
        assert.deepEqual(events,[["shift.close",a.id],["shift.open",b.id]]);
        assert.equal(result.queued,0);assert.equal(result.receiptCount,2);assert.equal(result.shift.server_id,22);
        assert.equal(result.shift.is_open,true);assert.equal(result.grant,deadline,"ACK discarded B's existing grant");
        assert.equal(result.first,200,"pre-flush snapshot used after B's opening was acknowledged");
        assert.equal(result.second,200,"second poll forced login for B");
        assert.deepEqual(postAckGrants,[deadline],"scope must survive the B.open ACK before a later read can repair it");
        assert.deepEqual(reads,holdLock?[1,3]:[3]);assert.equal(page.url(),home);
        if(!holdLock){
            const boundaries=await page.evaluate(async({b,deadline})=>{
                const snap=(actor,version)=>FieldLedger.ingest({actor,server_version:version});
                async function withoutGrant(actor,version){return (await snap(actor,version)).actor.grant_until;}
                const changed={...b,generation:b.generation+"-rotated"};
                const rotated=await withoutGrant(changed,4);
                await snap({...changed,grant_until:deadline},5);
                const explicit=(await snap({...changed,grant_until:null},6)).actor.grant_until;
                const commandDenied=await FieldLedger.closeShift().then(()=>false,e=>e.code==="grant_expired");
                const invalid=(await snap({...changed,grant_until:"invalid"},7)).actor.grant_until;
                const expired=new Date(Date.now()-10000).toISOString();
                await snap({...changed,grant_until:expired},8);
                const afterExpiry=await withoutGrant(changed,9);
                const expiredPoll=(await FieldTransport.personalRealtime("/realtime/state/")).status;
                await FieldLedger.revokeActor(changed);
                const revoked=await snap(changed,10),revokedPoll=(await FieldTransport.personalRealtime("/realtime/state/")).status;
                return {rotated,explicit,commandDenied,invalid,unchangedExpired:afterExpiry===expired,expiredPoll,revoked:revoked.locked&&!revoked.actor,revokedPoll};
            },{b,deadline});
            assert.deepEqual(boundaries,{rotated:null,explicit:null,commandDenied:true,invalid:"invalid",unchangedExpired:true,expiredPoll:401,revoked:true,revokedPoll:401});
        }
        console.log(JSON.stringify({holdLock,events,reads,first:result.first,second:result.second}));
    }finally{await ctx.close();}
}
async function upgrade(browser,mode){
    const ctx=await browser.newContext({serviceWorkers:"block"}),page=await ctx.newPage();
    const phone="9000000002",phoneHash=createHash("sha256").update("7"+phone).digest("hex");
    const owner={...b,key:"phone:"+phoneHash};
    const enrolled={...owner,grant_until:deadline};
    if(mode==="expired")enrolled.grant_until=new Date(Date.now()-10000).toISOString();
    if(mode==="wrong-generation")enrolled.generation="other";
    if(mode==="wrong-employee")enrolled.id++;
    if(mode==="wrong-access")enrolled.access_id++;
    if(mode==="wrong-key")enrolled.key="phone:"+"f".repeat(64);
    let reads=0;
    try{
        await ctx.route("**/*",async route=>{
            const req=route.request(),url=new URL(req.url()),json=(status,data)=>route.fulfill({status,contentType:"application/json",body:JSON.stringify(data)});
            if(req.url()===home)return route.fulfill({contentType:"text/html",body:'<html data-cr-field-local="true"><body></body></html>'});
            if(url.pathname==="/realtime/state/")return json(401,{authenticated:false});
            if(!url.pathname.endsWith("/field-sync/"))return route.abort();
            if(req.method()==="GET"){
                if(!url.searchParams.has("challenge"))return json(403,{code:"authentication_required"});
                return json(200,{protocol:"master-field/2",challenge:"qa",csrf_token:"csrf",server_time:new Date().toISOString()});
            }
            const v=req.postDataJSON();assert.equal(v.op,"snapshot");assert.equal(v.ticket,"existing-ticket");reads++;
            if(mode==="server-revoked")return json(403,{code:"access_revoked"});
            return json(200,{protocol:"master-field/2",snapshot:snapshot(mode==="server-cleared"?{...owner,grant_until:null}:owner,3,true,22)});
        });
        await page.goto(home);
        await page.addScriptTag({path:path.join(assets,"field-ledger.js")});
        await page.addScriptTag({path:path.join(assets,"field-identity.js")});
        await page.evaluate(async({owner,enrolled,phoneHash,mode,first})=>{
            // An omitted property is the .43 ledger's normalized null. Only a real
            // explicit removal on the new runtime carries the separate marker.
            await FieldLedger.selectActor(owner);await FieldLedger.ingest(first);
            if(mode==="explicit")await FieldLedger.selectActor({...owner,grant_until:null});
            await FieldLedger.putTicket(owner.key,owner.generation,"existing-ticket");
            await FieldIdentity.enrolledGrantUntil(owner);
            const database=await new Promise((resolve,reject)=>{
                const req=indexedDB.open("cr-master-personal-field-access-v2",1);req.onsuccess=()=>resolve(req.result);req.onerror=()=>reject(req.error);
            });
            const hash=Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",new TextEncoder().encode(owner.generation))),
                x=>x.toString(16).padStart(2,"0")).join("");
            const tx=database.transaction("grants","readwrite"),store=tx.objectStore("grants");
            if(mode==="scope")store.put(enrolled,"scope:"+hash);
            else store.put({actor:enrolled,until:Date.now()+3*86400000},"grant:"+phoneHash);
            await new Promise((resolve,reject)=>{tx.oncomplete=resolve;tx.onerror=()=>reject(tx.error);});database.close();
        },{owner,enrolled,phoneHash,mode,first:snapshot(owner,1,true,22)});
        await page.addScriptTag({path:path.join(assets,"field-transport.js")});
        const result=await page.evaluate(async()=>{
            const before=await FieldLedger.read();
            const res=await FieldTransport.personalRealtime("/realtime/state/?include_events=1&role_app_code=mining_master"),after=await FieldLedger.read();
            return {before:before.actor.grant_until,status:res.status,expiry:after.actor?.grant_until||null,locked:after.locked};
        });
        const valid=["grant","scope"].includes(mode);
        assert.equal(result.before,null);assert.equal(result.status,valid?200:401);
        assert.equal(result.expiry,valid?deadline:null);assert.equal(result.locked,mode==="server-revoked");
        assert.equal(reads,mode==="explicit"?0:1);
        if(mode==="explicit" || mode==="server-cleared"){
            await ctx.setOffline(true);
            const rejected=await page.evaluate(phone=>FieldIdentity.signIn(phone,"123456").then(()=>null,e=>e.message),phone);
            assert.match(rejected,/Система отозвала допуск/);
        }
        console.log(JSON.stringify({upgrade:mode,reads,status:result.status}));
    }finally{await ctx.close();}
}
(async()=>{
    const browser=await chromium.launch({headless:true});
    try{
        await handover(browser,false);await handover(browser,true);
        for(const mode of ["grant","scope","expired","wrong-generation","wrong-employee","wrong-access","wrong-key","explicit","server-cleared","server-revoked"])
            await upgrade(browser,mode);
    }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
