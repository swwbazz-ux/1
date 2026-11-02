// Check the native journal against the original pult DOM mutation library in real Chromium.
// The browser intercepts all traffic, including the real site's host, before it leaves the fixture.
const assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path");
const {chromium}=require(process.env.COPPER_PLAYWRIGHT_MODULE||"playwright");
const root=path.resolve(__dirname,"../../.."),assets=path.join(__dirname,"../profiles/mining_master_tablet/assets/tablet");
const home="https://mining-master.driverform.ru/mining-master/tablet/", reports=home+"reports/templates/";
const out=path.resolve(process.argv[2]||path.join(root,"qa_artifacts/field-live-actions"));fs.mkdirSync(out,{recursive:true});
const read=(base,name)=>fs.readFileSync(path.join(base,name),"utf8");
const actor={verified:true,key:"qa:local-board",generation:"qa-live",id:7,access_id:11,role:"mining_master"};
const inventory={excavators:[{id:1,number:"1"},{id:2,number:"2"}],trucks:[{id:10,number:"10"}]};
const live={excavators:[{excavator_id:1,truck_ids:[10]},{excavator_id:2,truck_ids:[]}]};
const snapshot={actor,inventory,live,server_version:1,shift:{is_open:true,server_id:3}};
const cardPath=home+"dispatcher/control/card/equipment/10/?state_version=1";
const tile='<button data-dispatcher-drag="truck" data-equipment-id="10" class="complex-truck-tile">10</button>';
function card(id,trucks=""){
    return '<section class="dispatcher-complex-card" data-equipment-id="'+id+'" data-zone-id="z'+id+'" data-dispatcher-drop="complex"><div class="complex-assigned-trucks">'+trucks+'</div></section>';
}
const board='<main class="dispatcher-shell"><div class="dispatcher-board" data-operational-state-version="1">'+
    card(1,tile)+card(2)+'<div class="dispatcher-trucks"></div></div></main><div id="dialog" hidden><div role="dialog" aria-modal="true">Подтвердите действие</div></div>';
const template='<form method="post"><input name="name"><input type="checkbox" name="columns" value="truck" checked><button>Сохранить</button></form><table><tbody></tbody></table>';
const checks={},ok=(k,value)=>{assert(value,k);checks[k]=true;};
(async()=>{
    const browser=await chromium.launch({headless:true}),ctx=await browser.newContext(),p=await ctx.newPage(),direct=[];
    let cardOnline=false,cardReads=0;
    const err=[];p.on("pageerror",e=>err.push(e.message));
    await ctx.route("**/*",route=>{
        const req=route.request();
        if(req.method()!=="GET"){direct.push(req.url());return route.abort("blockedbyclient");}
        if(req.url()===cardPath && cardOnline){cardReads++;return route.fulfill({contentType:"application/json",body:JSON.stringify({
            contract:"dispatcher-equipment-detail-v1",card_key:"10",operational_state_version:1,card:{name:"Проверенная машина №10"}})});}
        if(![home,reports].includes(req.url()))return route.abort("internetdisconnected");
        return route.fulfill({contentType:"text/html",body:'<!doctype html><html data-cr-field-local><body>'+(req.url()===reports?template:board)+'</body></html>'});
    });
    const tag=read(path.join(root,"СИСТЕМА_MVP/backend/static/js"),"dispatcher-board-mutations-v1.js");
    await ctx.addInitScript({content:[tag,read(assets,"field-ledger.js"),
        'window.FieldTransport={setBusy:fn=>FieldLedger.setBusy(fn),idle:()=>FieldLedger.reconcile(),status:{connected:false}};',
        read(assets,"field-live-actions.js"),
        'window.addEventListener("DOMContentLoaded",()=>createDispatcherBoardMutations());'].join("\n")});
    const truckAt=id=>p.locator('.dispatcher-complex-card[data-equipment-id="'+id+'"] [data-dispatcher-drag="truck"]');
    try{
        await p.goto(home);await p.evaluate(s=>FieldLedger.ingest(s),snapshot);
        const res=await p.evaluate(async()=>{
            const result=await fetch("/mining-master/tablet/dispatcher/control/truck/assign/",{method:"POST",
                headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"assign",truck_id:10,excavator_id:2,expected_assignment_state_id:0})});
            return {code:result.status,data:await result.json(),state:await FieldLedger.read()};
        });
        ok("local_receipt_after_signed_commit",res.code===202&&res.data.queued&&!res.data.server_confirmed&&
            res.state.outbox[0].signature&&res.state.outbox[0].id===res.data.local_event_id);
        await truckAt(2).waitFor();ok("original_pult_projects_durable_move",true);
        await p.reload();await truckAt(2).waitFor();ok("pending_survives_original_screen_reload",true);
        cardOnline=true;
        await p.evaluate(()=>window.FieldIdentity={confirmScope:async a=>a.key==="qa:local-board"});
        const seeded=await p.evaluate(url=>fetch(url).then(r=>r.json()),cardPath);
        cardOnline=false;
        const cached=await p.evaluate(url=>fetch(url).then(r=>r.json()),cardPath);
        ok("first_legacy_detail_with_pending_topology_is_durable",cardReads>0&&seeded.card.name===cached.card.name&&cached.source==="device"&&cached.card.zone.includes("2"));
        await p.locator("#dialog").evaluate(el=>el.hidden=false);
        const whileOpen=await p.evaluate(async snap=>{
            await FieldLedger.flush(e=>Promise.resolve({id:e.id,actor_id:e.actor_id,actor_access_id:e.actor_access_id,actor_key:e.actor_key,status:"applied",
                snapshot:{...snap,server_version:3,live_tokens:{trucks:{"10":{id:88,target:1}},zones:{"1":"active","2":"active"}}}}));
            return await FieldLedger.read();
        },snapshot);
        ok("receipt_holds_newer_snapshot_while_modal_open",!whileOpen.outbox.length&&whileOpen.sync.needs_reconcile&&whileOpen.live.excavators[1].truck_ids[0]===10);
        ok("open_dialog_and_selection_preserved",await truckAt(2).count()===1&&await p.locator("#dialog").isVisible());
        await p.locator("#dialog").evaluate(el=>el.hidden=true);await truckAt(1).waitFor();
        ok("last_ack_applies_authoritative_topology_on_idle",(await p.evaluate(()=>FieldLedger.read())).server_version===3);
        ok("next_drag_sees_server_cas_token",await truckAt(1).getAttribute("data-haul-assignment-state-id")==="88");
        const unknown=await p.evaluate(()=>fetch("/mining-master/tablet/not-a-command/",{method:"POST"}).then(x=>x.status));
        ok("unknown_workspace_mutations_fail_closed",unknown===405);
        await p.goto(reports);await p.locator('[name="name"]').fill("Полевой шаблон");await p.locator("button").click();
        await p.locator("tbody [data-field-template-event]").waitFor();await p.reload();
        ok("normal_report_form_reload",await p.locator("tbody [data-field-template-event]").innerText().then(s=>s.includes("Полевой шаблон")));
        await p.locator("button").click();await p.getByText("Шаблон с этим названием уже сохранён на планшете.").waitFor();
        const state=await p.evaluate(()=>FieldLedger.read());
        ok("duplicate_create_is_not_signed_twice",state.outbox.length===1&&state.outbox[0].actor_key===actor.key);
        ok("no_unjournaled_posts_or_browser_errors",!direct.length&&!err.length);
        fs.writeFileSync(path.join(out,"canonical-actions.json"),JSON.stringify(checks,null,2));console.log(checks);
    }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
