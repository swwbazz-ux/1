// Check the native journal against the original pult DOM mutation library in real Chromium.
// The browser intercepts all traffic, including the real site's host, before it leaves the fixture.
const assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path");
const {chromium}=require(process.env.COPPER_PLAYWRIGHT_MODULE||"playwright");
const root=path.resolve(__dirname,"../../.."),assets=path.join(__dirname,"../profiles/mining_master_tablet/assets/tablet");
const home="https://mining-master.driverform.ru/mining-master/tablet/", reports=home+"reports/templates/";
const out=path.resolve(process.argv[2]||path.join(root,"qa_artifacts/field-live-actions"));fs.mkdirSync(out,{recursive:true});
const read=(base,name)=>fs.readFileSync(path.join(base,name),"utf8");
const actor={verified:true,key:"qa:local-board",generation:"qa-live",id:7,access_id:11,role:"mining_master",grant_until:new Date(Date.now()+3600000).toISOString()};
const inventory={excavators:[{id:1,number:"1"},{id:2,number:"2"}],trucks:[{id:10,number:"10"}]};
const live={excavators:[{excavator_id:1,truck_ids:[10]},{excavator_id:2,truck_ids:[]}]};
const snapshot={actor,inventory,live,server_version:1,shift:{is_open:true,server_id:3}};
const cardPath=home+"dispatcher/control/card/equipment/10/?state_version=1";
const tile='<article data-dispatcher-drag="truck" data-equipment-id="10" class="dispatcher-truck-tile complex-truck-tile status-gray"><strong>10</strong><span>На разгрузку</span></article>';
function card(id,trucks=""){
    return '<section class="dispatcher-complex-card" data-dispatcher-drag="complex" data-equipment-id="'+id+'" data-zone-id="z'+id+'" data-dispatcher-drop="complex">'+
        '<span class="complex-state-chip">Назначена</span><div class="dispatcher-plan-strip"><strong>0%</strong><span>0 / 7 000 м³</span></div>'+
        '<span class="complex-kpi" data-kpi="plan"><b>0%</b></span><span class="complex-kpi" data-kpi="volume"><b>0<small>м³</small></b></span>'+
        '<div class="complex-assigned-trucks">'+trucks+'</div></section>';
}
function plan(fact,percent,loop=percent,loops=0){
    return {plan_status:"assigned",plan_status_label:"План назначен",plan_group_name:"Экскаваторы",
        plan_calculation_mode:"volume",plan_value:"7 000",completed_value:fact,progress_percent:percent,
        progress_loop_percent:loop,progress_completed_loops:loops,progress_phase:loops?"amber":"green",
        unit:"м³",fact_plan_label:fact+" / 7 000 м³"};
}
function snapshotCards(fact,percent){
    return {
        "complex:1":{card:{id:"complex-equipment-1",status_key:"green",status_label:"В работе",plan:plan(fact,percent)}},
        "equipment:10":{card:{id:"10",status_key:"blue",status_label:"На погрузку",plan:plan("8 400",120,20,1),shift:{id:30}}}
    };
}
const board='<main class="dispatcher-shell"><div class="dispatcher-board" data-operational-state-version="1">'+
    card(1,tile)+card(2)+'<div class="dispatcher-trucks"></div></div></main><div id="dialog" hidden><div role="dialog" aria-modal="true">Подтвердите действие</div></div>';
const template='<form method="post"><input name="name"><input type="checkbox" name="columns" value="truck" checked><button>Сохранить</button></form><table><tbody></tbody></table>';
const checks={},ok=(k,value)=>{assert(value,k);checks[k]=true;};
(async()=>{
    const browser=await chromium.launch({headless:true,channel:process.env.COPPER_CHROMIUM_CHANNEL}),ctx=await browser.newContext({
        viewport:{width:1280,height:768}
    }),p=await ctx.newPage(),direct=[];
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
        const one=p.locator('.dispatcher-complex-card[data-equipment-id="1"]');
        const fact=one.locator('.dispatcher-plan-strip span');
        const oldTruck=await truckAt(1).elementHandle();
        // The signed snapshot works with the cached board and no browser session or event stream.
        await p.evaluate(c=>FieldLedger.ingest(c),{...snapshot,cards:snapshotCards("5 900",84)});
        await p.waitForFunction(()=>document.querySelector(".dispatcher-plan-strip span")?.textContent==="5 900 / 7 000 м³");
        ok("snapshot_updates_complex_plan_at_same_version",await one.locator('[data-kpi="volume"] b').textContent()==="5 900м³"&&
            await one.getAttribute("data-plan-loop-percent")==="84"&&await one.evaluate(el=>el.style.getPropertyValue("--complex-progress"))==="84%");
        ok("snapshot_updates_truck_without_replacing_draggable_node",await oldTruck.evaluate(el=>el.isConnected&&
            el.classList.contains("status-blue")&&!el.classList.contains("status-gray")&&el.querySelector("span").textContent==="На погрузку"&&
            el.querySelector(".dispatcher-plan-loop-badge")?.textContent==="×1"&&el.style.getPropertyValue("--tile-progress")==="20%"));
        await p.locator("#dialog").evaluate(el=>el.hidden=false);
        await p.evaluate(c=>FieldLedger.ingest(c),{...snapshot,cards:snapshotCards("6 300",90)});
        ok("snapshot_keeps_open_dialog_and_card_values",await fact.textContent()==="5 900 / 7 000 м³");
        await p.locator("#dialog").evaluate(el=>el.hidden=true);
        await p.waitForFunction(()=>document.querySelector(".dispatcher-plan-strip span")?.textContent==="6 300 / 7 000 м³");
        ok("snapshot_refreshes_complex_after_dialog_closes",await one.locator('[data-kpi="plan"] b').textContent()==="90%");

        await p.evaluate(async c=>{
            document.documentElement.removeAttribute("data-cr-field-local");
            FieldTransport.status.snapshot_source="device_ticket";
            await FieldLedger.ingest(c);
        },{...snapshot,cards:snapshotCards("6 580",94)});
        await p.waitForFunction(()=>document.querySelector(".dispatcher-plan-strip span")?.textContent==="6 580 / 7 000 м³");
        ok("signed_refresh_updates_existing_online_page_after_cookie_loss",true);
        await p.evaluate(async c=>{
            FieldTransport.status.snapshot_source="cookie";
            window.applyOperationalStateRefresh=()=>Promise.reject(new Error("fragment unavailable"));
            await FieldLedger.ingest(c);
        },{...snapshot,cards:snapshotCards("1 083",15)});
        await p.waitForFunction(()=>document.querySelector(".dispatcher-plan-strip span")?.textContent==="1 083 / 7 000 м³",null,{timeout:2500});
        ok("authenticated_cookie_snapshot_paints_without_fragment_or_reload",true);
        await p.evaluate(()=>document.documentElement.setAttribute("data-cr-field-local",""));

        await p.evaluate(s=>FieldLedger.ingest({...s,cards:{}}),snapshot);
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
        await p.evaluate(async ({snap,cards,prior})=>{
            const current=await FieldLedger.read();
            await FieldLedger.ingest({...snap,server_version:3,shift:{is_open:false},cards:prior});
            await FieldLedger.openShift(current.actor);
            await FieldLedger.flush(async e=>({id:e.id,actor_id:e.actor_id,actor_access_id:e.actor_access_id,
                actor_key:e.actor_key,status:"conflict",code:"shift_occupied",
                snapshot:{...snap,server_version:4,shift:{is_open:false},cards}}));
        },{snap:snapshot,cards:snapshotCards("1 197",17),prior:snapshotCards("969",14)});
        await p.waitForFunction(()=>document.querySelector(".dispatcher-plan-strip span")?.textContent==="1 197 / 7 000 м³");
        const complexWhileBlocked=await p.evaluate(async path=>await (await fetch(path)).json(),
            home+"dispatcher/control/card/complex/1/?state_version=3");
        ok("shift_conflict_detail_uses_current_plan",complexWhileBlocked.card.plan.completed_value==="1 197");
        const conflict=await p.evaluate(()=>FieldLedger.read());
        ok("shift_conflict_keeps_journal_and_updates_plan_without_reload",
            conflict.shift.is_open&&conflict.outbox.length===1&&conflict.outbox[0].status==="conflict"&&conflict.server_version===3);
        await p.evaluate(async snap=>{
            const head=(await FieldLedger.read()).outbox[0];
            await FieldLedger.resolveConflict(head.id);
            await FieldLedger.flush(async e=>({id:e.id,actor_id:e.actor_id,actor_access_id:e.actor_access_id,
                actor_key:e.actor_key,status:"applied",resolved_event_id:head.id,snapshot:{...snap,server_version:5}}));
        },snapshot);
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
