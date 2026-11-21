// Real IndexedDB, isolated HTTPS origin. No command in this test can reach an actual workplace.
const assert=require("node:assert/strict");
const fs=require("node:fs"),path=require("node:path");
const {chromium}=require(process.env.COPPER_PLAYWRIGHT_MODULE || "playwright");
const origin="https://mining-master.example.test", root="/mining-master/tablet/";
const source=fs.readFileSync(path.resolve(__dirname,"../profiles/mining_master_tablet/assets/tablet/tablet-planning-draft.js"),"utf8");
const layout=rows=>({excavators:rows.map(([x,...ts])=>({excavator_id:x,truck_ids:ts}))});
const initial=layout([[1,11],[2]]);
const one=layout([[1],[2,11]]);
const both=layout([[1,12],[2,11]]);
const role=g=>({authenticated:true,role_active:true,role_app_code:"mining_master",active_role_code:"mining_master",session_role_code:"mining_master",session_revision:g});
function pageHtml(live,{stamp,open}) {
    const trucks=(n,assigned)=>'<article data-dispatcher-drag="truck" data-equipment-id="'+n+'" data-haul-assignment-state-id="'+(assigned?17:0)+'"></article>';
    const active=new Set(live.excavators.map(x=>x.excavator_id)), assigned=new Set(live.excavators.flatMap(x=>x.truck_ids));
    const zones=live.excavators.map(x=>'<article data-dispatcher-drop="complex" data-dispatcher-drag="complex" data-equipment-id="'+x.excavator_id+'">'+x.truck_ids.map(n=>trucks(n,true)).join("")+'</article>').join("");
    return '<!doctype html><div class="dispatcher-board" data-dispatcher-own-shift-open="'+open+'"><button data-confirm-variant="shift-end" data-confirm-date="02.11" data-confirm-time="08:00" data-confirm-person="'+stamp+'"></button><input name="csrfmiddlewaretoken" value="test">'+zones+[1,2,3].filter(n=>!active.has(n)).map(n=>'<article data-dispatcher-drag="excavator" data-equipment-id="'+n+'"></article>').join("")+[11,12,13].filter(n=>!assigned.has(n)).map(n=>trucks(n,false)).join("")+'</div><script src="/script.js"></script>';
}
(async()=>{
    const browser=await chromium.launch({headless:true,...(process.env.COPPER_CHROME_PATH?{executablePath:process.env.COPPER_CHROME_PATH}:{channel:"chrome"})});
    let successes=[];
    async function run(title,fn){
        const c=await browser.newContext(), p=await c.newPage();
        const mock={live:structuredClone(initial),stamp:"Первый",generation:"generation-a",open:true,connected:true,endpoint:false,posts:[],opaque:0,reply:"normal"};
        const json=(rt,status,data)=>rt.fulfill({status,contentType:"application/json",body:JSON.stringify(data)});
        await c.route("**/*",async rt=>{
            const req=rt.request(), u=new URL(req.url());
            assert.equal(u.origin,origin);
            if(u.pathname==="/script.js")return rt.fulfill({contentType:"text/javascript; charset=utf-8",body:source});
            if(req.method()==="GET" && u.pathname===root && !u.search) return rt.fulfill({contentType:"text/html; charset=utf-8",body:pageHtml(mock.live,mock)});
            if(!mock.connected)return rt.abort("internetdisconnected");
            if(u.pathname==="/realtime/state/")return json(rt,200,role(mock.generation));
            if(req.method()==="GET"&&u.pathname===root) return rt.fulfill({contentType:"text/html; charset=utf-8",body:pageHtml(mock.live,mock)});
            const sync=u.pathname===root+"field-sync/";
            if(sync&&req.method()==="GET") {
                if(!mock.endpoint)return json(rt,404,{});
                return json(rt,200,{protocol:"master-field/1",ticket:"signed-test",snapshot:{actor:{key:"phone:test",id:1,access_id:2,role:"mining_master",verified:true,generation:mock.generation},shift:{is_open:true,server_id:9,local_shift_id:"b970323b-365f-4c2b-9a05-873311993963"}}});
            }
            if(req.method()==="POST"){
                assert.equal(req.headers()["x-csrftoken"],"test");
                const body=req.postDataJSON(); mock.posts.push({path:u.pathname,body});
                if(mock.reply==="lost"){mock.reply="normal";return rt.abort("connectionreset");}
                if(mock.reply==="conflict")return json(rt,409,{error:"Изменено."});
                if(sync)return json(rt,200,{status:"applied",id:body.event.id,actor_key:body.event.actor_key});
                return json(rt,200,{ok:true,client_action_id:body.client_action_id});
            }
            throw Error(req.method()+" "+u.pathname);
        });
        async function start(){await p.goto(origin+root);return p.evaluate(live=>CopperTabletDraft.load(live,{document}),mock.live);}
        async function save(target){return p.evaluate(async target=>{const {revision}=await CopperTabletDraft.current(); return CopperTabletDraft.save(target,{document,revision});},target);}
        async function go(){return p.evaluate(async()=>CopperTabletDraft.activate({document,revision:(await CopperTabletDraft.current()).revision}));}
        await start();
        try{await fn({p,mock,start,save,go});successes.push(title);}
        finally{await c.close();}
    }
    try{
        await run("offline save survives reopen, no POST",async({p,mock,start,save})=>{
            mock.connected=false;const draft=await save(both);await start();
            assert.deepEqual((await p.evaluate(()=>CopperTabletDraft.current())).payload,both);
            assert.equal(mock.posts.length,0);
            await assert.rejects(p.evaluate(async ([v,r])=>CopperTabletDraft.save(v,{document,revision:r}),[initial,draft.revision-1]),/изменился/);
        });
        await run("one ordinary command guarded by actual state id",async({mock,save,go})=>{
            await save(one);assert.equal((await go()).status,"applied");
            assert.equal(mock.posts.length,1);assert.equal(mock.posts[0].path,root+"dispatcher/control/truck/assign/");
            assert.deepEqual({...mock.posts[0].body,client_action_id:1},{action:"assign",excavator_id:2,truck_id:11,expected_assignment_state_id:17,client_action_id:1});
        });
        await run("unknown result replays identical ID and payload",async({p,mock,save,go,start})=>{
            await save(one);mock.reply="lost";assert.equal((await go()).status,"queued");await start();
            assert.equal((await go()).status,"applied");
            assert.deepEqual(mock.posts[1],mock.posts[0]);
        });
        await run("unsupported batch never changes half the plan",async({mock,save,go})=>{
            await save(both);const res=await go();assert.equal(res.status,"queued");
            assert.match(res.message,/одно действие/);assert.equal(mock.posts.length,0);
        });
        await run("atomic exchange sends whole plan exactly once",async({mock,save,go})=>{
            mock.endpoint=true;await save(both); assert.equal((await go()).status,"applied");
            assert.equal(mock.posts.length,1);
            assert.equal(mock.posts[0].path,root+"field-sync/");const ev=mock.posts[0].body.event;
            assert.deepEqual(ev.payload,both);assert.deepEqual(ev.base_payload,initial);
            assert.equal(ev.server_shift_id,9);assert.equal(ev.actor_generation,mock.generation);
        });
        await run("conflicting live edit preserved, zero writes",async({mock,save,go})=>{
            await save(one);mock.live=layout([[1],[2],[3,11]]);assert.equal((await go()).status,"conflict");assert.equal(mock.posts.length,0);
        });
        await run("explicit rebase preserves unrelated new truck",async({p,mock,save,go})=>{
            const v=await save(one);mock.live=layout([[1],[2],[3,11,12]]);
            assert.equal((await go()).status,"conflict");
            const next=await p.evaluate(revision=>CopperTabletDraft.rebase({document,revision}),v.revision);
            assert.deepEqual(next.payload,layout([[1],[2,11],[3,12]]));assert.equal(next.activation,null);
            assert.equal(mock.posts.length,0);
        });
        await run("whole disband one guarded request",async({mock,save,go})=>{
            await save(layout([[2]]));assert.equal((await go()).status,"applied");
            assert.equal(mock.posts.length,1);assert.deepEqual(mock.posts[0].body.expected_assignment_states,{"11":17});
            assert.equal(mock.posts[0].body.zone,"inactive");assert.equal(mock.posts[0].body.expected_zone,"active");
        });
        await run("next draft uses confirmed topology and versions",async({mock,save,go,start})=>{
            await save(one);assert.equal((await go()).status,"applied");mock.live=one;await start();
            await save(initial);assert.equal((await go()).status,"applied");assert.equal(mock.posts.length,2);
            assert.equal(mock.posts[1].body.excavator_id,1);
        });
        await run("closed personal shift prevents edits",async({p,mock,start,save,go})=>{
            mock.open=false;await start();await assert.rejects(save(one),/своей открытой смене/);assert.equal((await go()).status,"auth");assert.equal(mock.posts.length,0);
        });
        await run("queued action never crosses an employee handover",async({p,mock,start,save,go})=>{
            await p.waitForFunction(async()=> (await CopperTabletDraft.current()).verified?.generation==="generation-a");
            await save(one); mock.connected=false;assert.equal((await go()).status,"queued");
            mock.connected=true;mock.generation="generation-b";mock.stamp="Другой";await start();
            const res=await p.evaluate(()=>CopperTabletDraft.resume({document}));assert.equal(res.status,"auth");assert.equal(mock.posts.length,0);
            assert.deepEqual((await p.evaluate(()=>CopperTabletDraft.current())).payload,one);
        });
        console.log(successes.length+" passed\n"+successes.join("\n"));
    }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
