/* Real Chromium/WebCrypto/IndexedDB; all traffic is intercepted inside a local test context. */
const fs=require("node:fs"),os=require("node:os"),path=require("node:path"),assert=require("node:assert/strict"),crypto=require("node:crypto");
const {chromium}=require(process.env.COPPER_PLAYWRIGHT_MODULE||"playwright");
const out=path.resolve(process.argv[2]||path.join(__dirname,"../../../qa_artifacts/field-ledger"));
fs.mkdirSync(out,{recursive:true});
const assets=path.join(__dirname,"../profiles/mining_master_tablet/assets/tablet");
const origin="https://tablet.example.test", route="/mining-master/tablet/field-sync/";
const actor={verified:true,key:"phone:aaa",id:1,access_id:11,full_name:"Мастер А",generation:"login-A",role:"mining_master"};
const B={...actor,key:"phone:bbb",id:2,access_id:22,full_name:"Мастер Б",generation:"login-B"};
const inventory={trucks:[{id:10,number:"11"},{id:11,number:"22"}],excavators:[{id:1,number:"1"},{id:2,number:"2"}]};
const p0={excavators:[{excavator_id:1,truck_ids:[10]}]},p1={excavators:[{excavator_id:2,truck_ids:[10]}]};
const snap=(a,v=1)=>({actor:a,shift:{is_open:false},server_version:v,inventory,live:p0,
    live_tokens:{trucks:{"10":{id:100+v,target:1}},zones:{"1":"active"}},
    cards:{"equipment:10":{card:{number:"11",note:"старое"},version:v}},refs:{worker_shifts:[]}});
const directory=fs.mkdtempSync(path.join(os.tmpdir(),"master-ledger-"));
let ctx,page,mode="offline",owner=actor,pinned=null,receipt=new Map(),attempts=[],lostAck=true,version=1;
const checks={};
function ok(name,condition){assert(condition,name);checks[name]=true;}
const wire=(e,status="applied")=>({id:e.id,actor_id:e.actor_id,actor_key:e.actor_key,actor_access_id:e.actor_access_id,status});
async function intercept(r){
    const req=r.request(),url=new URL(req.url());
    if(url.origin!==origin)return r.abort("blockedbyclient");
    if(url.pathname!==route)return r.fulfill({status:200,contentType:"text/html",body:'<html><body><input name="csrfmiddlewaretoken" value="test"></body></html>'});
    if(mode==="offline")return r.abort("internetdisconnected");
    const json=(code,obj)=>r.fulfill({status:code,contentType:"application/json",body:JSON.stringify(obj)});
    if(mode==="legacy")return json(404,{});
    const cap=()=>({protocol:"master-field/2",public_key:pinned,ticket:"ticket:"+owner.key+":"+owner.generation,snapshot:snap(owner,version)});
    if(req.method()==="GET")return pinned?json(200,cap()):json(409,{code:"enrollment_required"});
    const body=req.postDataJSON();assert.equal(req.headers()["x-csrftoken"],"test");
    if(body.op==="enroll"){if(pinned)assert.deepEqual(body.public_key,pinned);pinned=body.public_key;return json(200,cap());}
    const bytes=Buffer.from(body.signed_bytes,"base64url"),sig=Buffer.from(body.signature,"base64url"),e=body.event;
    assert.deepEqual(JSON.parse(bytes.toString()),e);
    assert(crypto.verify("sha256",bytes,{key:crypto.createPublicKey({key:pinned,format:"jwk"}),dsaEncoding:"ieee-p1363"},sig));
    assert.equal(body.ticket,"ticket:"+e.actor_key+":"+e.actor_generation);
    attempts.push({id:e.id,seq:e.sequence,bytes:body.signed_bytes,sig:body.signature});
    if(receipt.has(e.id))return json(200,{...receipt.get(e.id),deduplicated:true});
    if(e.depends_on.some(id=>!receipt.has(id)))return json(409,{...wire(e,"retry"),message:"dependency"});
    let a=wire(e);
    if(e.type==="card.settings"&&e.payload.forceConflict)a={...wire(e,"conflict"),code:"state_changed",snapshot:snap(owner,++version)};
    if(e.type==="conflict.resolve")a.resolved_event_id=e.payload.event_id;
    receipt.set(e.id,a);version++;
    if(lostAck){lostAck=false;return r.abort("connectionreset");}return json(200,a);
}
async function launch(transport=false){
    ctx=await chromium.launchPersistentContext(directory,{headless:true,args:["--disable-background-networking"]});
    await ctx.route("**/*",intercept);
    page=ctx.pages()[0]||await ctx.newPage();
    await page.goto(origin+"/work/");
    await page.addScriptTag({path:path.join(assets,"field-ledger.js")});
    if(transport)await page.addScriptTag({path:path.join(assets,"field-transport.js")});
}
const read=()=>page.evaluate(()=>FieldLedger.read());
const run=fn=>page.evaluate(fn);
(async()=>{
    try{
        await launch();
        await page.evaluate(s=>FieldLedger.ingest(s),snap(actor));
        let s=await read();
        ok("current authorized cards, refs and assignment compare-and-swap tokens",s.snapshot_ready&&s.details["equipment:10"].number==="11"&&!!s.refs.worker_shifts&&s.live_tokens.trucks[10].id===101);
        await run(()=>FieldLedger.openShift());
        await page.evaluate(a=>FieldLedger.cacheDetails("equipment:11",{number:"22"},1,a.key),actor);
        await page.evaluate(a=>FieldLedger.cacheDetails("equipment:11",{number:"wrong update"},1,a.key),actor);
        await page.evaluate(a=>FieldLedger.cacheDetails("complex:5",{number:"foreign"},1,a.key),B);
        s=await read();
        ok("first detail hydration during local opening cannot replace cached cards or cross actors",s.details["equipment:11"].number==="22"&&!s.details["complex:5"]);
        const first=(await read()).outbox[0],firstDate=first.occurred_at,local=first.local_shift_id;
        const original=await run(()=>FieldLedger.deviceIdentity());
        ok("stable signed origin time and sequence",first.sequence===1&&first.signature.length===86&&first.depends_on.length===0&&Date.parse(firstDate)>0);
        const signed=Buffer.from(first.signed_bytes,"base64url");
        ok("actual ECDSA signature verifies on original bytes",crypto.verify("sha256",signed,{key:crypto.createPublicKey({key:original.public_key,format:"jwk"}),dsaEncoding:"ieee-p1363"},Buffer.from(first.signature,"base64url")));
        ok("cannot alter signed time",!crypto.verify("sha256",Buffer.from(signed.toString().replace(firstDate,"2000-01-01T00:00:00Z")),{key:crypto.createPublicKey({key:original.public_key,format:"jwk"}),dsaEncoding:"ieee-p1363"},Buffer.from(first.signature,"base64url")));
        const rawkey=await run(async()=>{const db=await new Promise(r=>{const op=indexedDB.open("copper-master-tablet-ledger-v2");op.onsuccess=()=>r(op.result);});
            const pair=await new Promise(r=>{const op=db.transaction("secrets").objectStore("secrets").get("p256");op.onsuccess=()=>r(op.result);});
            try{await crypto.subtle.exportKey("jwk",pair.privateKey);return true;}catch{return false;}});
        ok("private device key is not exportable",!rawkey);
        const rev=s.draft.revision;
        await page.evaluate(async({p,rev})=>FieldLedger.saveDraft(p,rev),{p:p1,rev});
        assert.equal((await read()).live.excavators[0].excavator_id,1);
        await run(()=>FieldLedger.applyDraft());
        await run(()=>Promise.all([1,2,3].map(id=>FieldLedger.command("card.settings",{id},{reduce(st){st.details.count=(st.details.count||0)+1;}}))));
        await page.evaluate(a=>FieldLedger.cacheDetails("complex:5",{number:"stale"},23,a.key),actor);
        ok("pending detail edits block remote card hydration",!(await read()).details["complex:5"]);
        s=await read();
        ok("concurrent input commits without lost updates",s.details.count===3&&s.outbox.length===6&&s.outbox.every((e,i)=>e.sequence===i+1&&(i===0||e.depends_on[0]===s.outbox[i-1].id)));
        const fail=await run(async()=>{try{await FieldLedger.command("card.settings",{id:99},{reduce(){throw Error("conflict");}});return false;}catch{return true;}});
        ok("abort projection and journal append together",fail&&(await read()).sequence===6);
        const ids=s.outbox.map(e=>e.id);
        await ctx.close();await launch();
        s=await read();ok("cold start keeps key, actor, exact local shift, time and events",s.shift.is_open&&s.shift.local_shift_id===local&&s.outbox[0].occurred_at===firstDate&&JSON.stringify(ids)===JSON.stringify(s.outbox.map(e=>e.id))&&JSON.stringify((await run(()=>FieldLedger.deviceIdentity())).public_key)===JSON.stringify(original.public_key));
        await page.evaluate(s=>FieldLedger.ingest(s),snap(actor,23));
        s=await read();ok("external state cannot erase offline projection",s.live.excavators[0].excavator_id===2&&s.sync.observed_version===23&&s.sync.applied_version===1);
        await run(()=>FieldLedger.closeShift());
        ok("closed shift requires fresh personal selection",await run(async()=>{try{await FieldLedger.openShift();return false;}catch(e){return e.code==="actor_not_verified";}}));
        await run(()=>FieldLedger.lock());await page.evaluate(a=>FieldLedger.selectActor(a),B);await run(()=>FieldLedger.openShift());
        s=await read();ok("next master inherits draft, prior actor events untouched",s.actor.key===B.key&&s.draft.payload.excavators[0].excavator_id===2&&s.outbox[0].actor_key===actor.key&&s.outbox.at(-1).actor_key===B.key);
        await page.addScriptTag({path:path.join(assets,"field-transport.js")});
        mode="legacy";const legacy=await run(()=>FieldTransport.identify());ok("legacy 404 never ACKs work",legacy.legacy&&(await read()).outbox.length===8);
        mode="server";owner=actor;await run(()=>FieldTransport.identify());owner=B;await run(()=>FieldTransport.refresh());
        await run(()=>FieldLedger.retry());
        s=await read();
        ok("network replay uses original actor despite new employee cookie",receipt.size===8&&s.outbox.length===0);
        ok("lost ACK sends exact bytes and UUID again",attempts[0].id===attempts[1].id&&attempts[0].bytes===attempts[1].bytes&&attempts[0].sig===attempts[1].sig);
        ok("audit remains after positive ACK",!!(await page.evaluate(id=>FieldLedger.journal(id),first.id))&&s.receipts[first.id].sequence===1);
        mode="offline";
        await page.evaluate(a=>FieldLedger.selectActor(a),B);if(!(await read()).shift.is_open)await run(()=>FieldLedger.openShift());
        await run(()=>FieldLedger.command("card.settings",{forceConflict:true},{reduce(s){s.details.note="mine";}}));
        const cc=(await read()).outbox.at(-1).id;
        await run(()=>FieldLedger.command("card.settings",{tail:true},{reduce(s){s.details.note="two";}}));
        mode="server";await run(()=>FieldTransport.refresh());await run(()=>FieldLedger.retry());
        ok("conflict blocks dependent event", (await read()).outbox[0].id===cc&&(await read()).outbox.length===2&&!receipt.has((await read()).outbox[1].id));
        await page.evaluate(id=>FieldLedger.resolveConflict(id),cc);await run(()=>FieldLedger.retry());
        s=await read();ok("explicit resolution retains original rejected receipt and replays remaining tail",s.outbox.length===0&&!!s.receipts[cc].resolved_by);
        await run(()=>FieldTransport.setBusy(()=>true));await page.evaluate(s=>FieldLedger.ingest(s),{...snap(B,100),live:p0});
        s=await read();ok("incoming state deferred while editing",s.sync.observed_version===100&&s.sync.applied_version<100);
        await run(()=>{FieldTransport.setBusy(()=>false);return FieldTransport.idle();});
        ok("deferred authoritative state applied at safe boundary",(await read()).sync.applied_version===100);
        mode="offline";
        const originalOld={revision:7,payload:p1,base:p0,dirty:true,activation:{id:crypto.randomUUID(),stamp:"old",owner:{generation:"earlier"},
            status:"queued",command:{path:"/legacy",data:{client_action_id:"existing-request"}}}};
        await page.evaluate(async old=>{
            const db=await new Promise(resolve=>{const op=indexedDB.open("copper-master-planning-copy-v1",1);
                op.onupgradeneeded=()=>op.result.createObjectStore("drafts");op.onsuccess=()=>resolve(op.result);});
            await new Promise(resolve=>{const t=db.transaction("drafts","readwrite");t.objectStore("drafts").put(old,"tablet-draft");t.oncomplete=()=>{db.close();resolve();};});
        },originalOld);
        await page.addScriptTag({path:path.join(assets,"tablet-planning-draft.js")});
        const count=(await read()).sequence;
        const imported=await run(()=>CopperTabletDraft.load(null,{document}));
        ok("0.1.41 shared draft kept; old pending command retains identity; import sends no event",
            JSON.stringify(imported.payload)===JSON.stringify(p1)&&imported.activation.id===originalOld.activation.id&&imported.activation.command.data.client_action_id==="existing-request"&&(await read()).sequence===count);
        await page.evaluate(s=>FieldLedger.ingest(s),{...snap(B,101),draft:{revision:15,payload:p0}});
        ok("server refresh cannot erase never-synced migrated draft",JSON.stringify((await read()).draft.payload)===JSON.stringify(p1));
        if(!(await read()).shift.is_open)await run(()=>FieldLedger.openShift());
        const stale=await run(async()=>{try{await FieldLedger.command("card.settings",{id:11},{actor_key:"previous-employee"});return false;}catch(e){return e.code==="shift_changed";}});
        ok("stale UI cannot write using a replacement employee",stale);
        const failed=await run(async()=>{
            await FieldLedger.flush(async e=>({id:e.id,actor_key:"wrong-person",actor_id:e.actor_id,actor_access_id:e.actor_access_id,status:"applied"}));
            return FieldLedger.read();
        });
        ok("wrong actor ACK does not delete queue",failed.outbox.length>0);
        // Outbox proofs and the confirmed/rejected originals remain queryable after handover.
        ok("durable applied confirmation and conflict resolution record",
            (await page.evaluate(id=>FieldLedger.confirmation(id),first.id))?.status==="applied" &&
            (await page.evaluate(id=>FieldLedger.confirmation(id),cc))?.resolved_by!=null);
        const negative=await ctx.newPage();await negative.goto(origin+"/work/");
        await negative.addScriptTag({path:path.join(assets,"field-ledger.js")});
        const duplicateProofs=await Promise.all([page,negative].map(tab=>tab.evaluate(()=>FieldLedger.deviceIdentity())));
        ok("both tabs operate with the same registered device key",JSON.stringify(duplicateProofs[0])===JSON.stringify(duplicateProofs[1]));
        await negative.close();
        // Retained grant does not permit a freshly signed new shift once expired.
        await run(async()=>{if((await FieldLedger.read()).shift.is_open)await FieldLedger.closeShift();await FieldLedger.lock();});
        await page.evaluate(b=>FieldLedger.selectActor({...b,grant_until:"2001-01-01T00:00:00Z"}),B);
        ok("expired personal grant refuses a new shift",await run(async()=>{try{await FieldLedger.openShift();return false;}catch(e){return e.code==="grant_expired";}}));
        await page.reload();await page.addScriptTag({path:path.join(assets,"field-ledger.js")});
        await run(()=>FieldLedger.retry());
        await run(()=>{void FieldLedger.flush((e,proof)=>{window.waitingSend={id:e.id,...proof};return new Promise(()=>{});});});
        await page.waitForFunction(()=>!!window.waitingSend);
        const started=await run(()=>window.waitingSend);const before=Date.now();
        await page.reload();await page.addScriptTag({path:path.join(assets,"field-ledger.js")});
        const recovered=await run(async()=>{
            let attempt;
            await FieldLedger.flush(async(e,proof)=>{
                if(!attempt)attempt={id:e.id,...proof};
                return {id:e.id,actor_key:e.actor_key,actor_id:e.actor_id,actor_access_id:e.actor_access_id,status:"applied"};
            });return attempt;
        });
        ok("new page immediately reclaims abandoned send with identical UUID and signed bytes",Date.now()-before<5000&&JSON.stringify(started)===JSON.stringify(recovered));
        fs.writeFileSync(path.join(out,"results.json"),JSON.stringify({checks,server_receipts:receipt.size},null,2));
        console.log(Object.keys(checks).length+" real browser checks passed");
    }finally{await ctx?.close();/* temp profile retained on failure for local diagnosis */}
})().catch(e=>{fs.writeFileSync(path.join(out,"results.json"),JSON.stringify({checks,error:e.message},null,2));console.error(e);process.exitCode=1;});
