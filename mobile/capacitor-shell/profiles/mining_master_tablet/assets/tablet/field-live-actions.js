/* The native tablet commits its original pult commands before updating the operator's screen. */
(function(win,doc){
    "use strict";
    if(win.top!==win || win.FieldLiveActions || location.origin!=="https://mining-master.driverform.ru")return;
    const ROOT="/mining-master/tablet/", path=location.pathname;
    if(!path.startsWith(ROOT)||[ROOT+"login/",ROOT+"activate/",ROOT+"logout/"].includes(path))return;
    const nativeFetch=win.fetch.bind(win), L=()=>win.FieldLedger;
    const activeLocal=()=>doc.documentElement?.hasAttribute("data-cr-field-local");
    const clone=data=>data==null?data:JSON.parse(JSON.stringify(data));
    const fail=message=>{throw new Error(message);};
    const pos=value=>Number.isSafeInteger(Number(value)) && Number(value)>0 ? Number(value):0;
    const same=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
    const board=()=>doc.querySelector(".dispatcher-board");
    const cardKey=(kind,id)=>kind+":"+id;
    const rowKey=(kind,id)=>kind==="complex"?"complex-equipment-"+id:String(id);
    let last=null, writes=0, dragging=false, pointer=false, mutations=null, pageActor="";
    const routes=[
        [/^dispatcher\/control\/excavator\/move\/$/,"live.move"],
        [/^dispatcher\/control\/truck\/assign\/$/,"live.assign"],
        [/^dispatcher\/control\/card\/(equipment|complex)\/([1-9]\d*)\/$/,"card.settings","equipment_id"],
        [/^dispatcher\/control\/downtime\/([1-9]\d*)\/close\/$/,"downtime.close","downtime_id"],
        [/^dispatcher\/assignments\/([1-9]\d*)\/cancel\/$/,"assignment.cancel","assignment_id"],
        [/^dispatcher\/trips\/([1-9]\d*)\/cancel\/$/,"trip.cancel","trip_id"],
        [/^dispatcher\/trips\/([1-9]\d*)\/complete\/$/,"trip.complete","trip_id"],
        [/^dispatcher\/trucks\/([1-9]\d*)\/manual-trip\/$/,"trip.manual","truck_id"],
        [/^dispatcher\/shifts\/([1-9]\d*)\/service-close\/$/,"worker_shift.close","shift_id"],
        [/^dispatcher\/shift-reports\/(?:trucks|excavation|hourly|hourly-grid)\/$/,"report.trip_correct"],
        [/^reports\/templates\/$/,"report.template_save"]
    ];
    const fields={
        "live.move":["excavator_id","zone","expected_zone","expected_assignment_states"],
        "live.assign":["truck_id","excavator_id","action","expected_assignment_state_id","expected_assignment_states"],
        "card.settings":["loading_horizon","loading_block","rock_type_id","destinations","dump_point_ids"],
        "downtime.close":[],
        "assignment.cancel":["reason"],"trip.cancel":["reason"],"trip.complete":["reason"],
        "trip.manual":["excavator_id","rock_type_id","dump_point_id","trips_count","completed_at","reason"],
        "worker_shift.close":["close_kind","reason","end_fuel","end_engine_hours","end_mileage"],
        "report.trip_correct":["trip_id","date","shift_type","correction_reason","volume_m3","transport_distance_km",
            "loading_horizon","loading_block","downtime_text","note","rock_type_id","actual_dump_point_id"],
        "report.template_save":["template_id","name","columns","group_by","is_active","date_from","date_to",
            "loading_shift_type","unloading_shift_type","carryover","truck","excavator","rock_type","dump_point"]
    };
    const labels={"live.move":"Перемещение экскаватора","live.assign":"Назначение техники","card.settings":"Настройки комплекса",
        "downtime.close":"Завершение простоя","assignment.cancel":"Отмена назначения","trip.cancel":"Отмена рейса",
        "trip.complete":"Завершение рейса","trip.manual":"Ручной рейс","worker_shift.close":"Служебное завершение смены",
        "report.trip_correct":"Корректировка рейса","report.template_save":"Шаблон отчёта"};
    function unhandledWorkplaceWrite(url){
        return url.origin===location.origin && url.pathname.startsWith(ROOT) &&
            ![ROOT,ROOT+"field-sync/",ROOT+"login/",ROOT+"activate/",ROOT+"logout/"].includes(url.pathname);
    }
    function route(url){
        if(url.origin!==location.origin || !url.pathname.startsWith(ROOT))return null;
        const rest=url.pathname.slice(ROOT.length);
        for(const [regex,type,idKey] of routes){
            const m=rest.match(regex);if(!m)continue;
            const ids=idKey?{[idKey]:Number(m.at(-1))}:{};
            if(type==="card.settings")ids.category=m[1];
            return {type,ids};
        }
        return null;
    }
    function encoded(formData){
        const values={};
        for(const key of new Set(formData.keys())){
            const all=formData.getAll(key);if(all.some(v=>typeof v!=="string"))throw new Error("Файл нельзя записать как команду планшета.");
            values[key]=all.length===1&&key!=="columns"?all[0]:all;
        }
        return values;
    }
    function reportNumber(value){
        let text=String(value??"").trim().replace(/\s/g,"").replace(",",".");
        // Django's unlocalize filter renders a nullable report metric as "None".
        if(!text||text==="None")return "";
        if(!/^\d+(\.\d+)?$/.test(text)||!Number.isFinite(Number(text)))fail("Проверьте объём и плечо рейса.");
        return text;
    }
    function payload(command,raw){
        const allowed=fields[command.type];
        const p={};
        for(const key of Object.keys(raw||{})){
            if(allowed.includes(key)||(command.type==="report.template_save"&&/^column_label_[a-z_]+$/.test(key)))p[key]=clone(raw[key]);
        }
        Object.assign(p,command.ids);
        for(const key of ["excavator_id","truck_id","trip_id","rock_type_id","dump_point_id"]){
            if(p[key]!==undefined&&p[key]!==null&&p[key]!=="")p[key]=pos(p[key])||fail("Некорректный номер объекта.");
        }
        if(command.type==="card.settings"){
            if(!p.rock_type_id || !Array.isArray(p.destinations) || !p.destinations.length)fail("Укажите породу и хотя бы одну точку разгрузки.");
            const used=new Set();
            p.destinations=p.destinations.map(row=>{
                const id=pos(row?.dump_point_id), km=String(row?.transport_distance_km??"").trim().replace(",",".");
                if(!id||used.has(id)|| (km && (!/^\d+(\.\d+)?$/.test(km) || !Number.isFinite(Number(km)))))fail("Проверьте точки разгрузки и плечо.");
                used.add(id);return {dump_point_id:id,transport_distance_km:km};
            });
            p.dump_point_ids=p.destinations.map(r=>r.dump_point_id);
        }
        if(["assignment.cancel","trip.cancel","trip.complete","trip.manual","worker_shift.close"].includes(command.type) && !String(p.reason||"").trim()){
            // The original service-close handler supplies the default note only for neglected close.
            if(command.type!=="worker_shift.close" || p.close_kind!=="neglected")fail("Укажите причину действия.");
        }
        if(command.type==="trip.manual"){
            p.trips_count=Number(p.trips_count||1);
            if(!Number.isInteger(p.trips_count)||p.trips_count<1||p.trips_count>10||!p.excavator_id||!p.rock_type_id||!p.dump_point_id)fail("Проверьте параметры ручного рейса.");
        }
        if(command.type==="report.trip_correct" && (!p.trip_id||!String(p.correction_reason||"").trim()))fail("Укажите рейс и причину корректировки.");
        if(command.type==="report.trip_correct")for(const key of ["volume_m3","transport_distance_km"]){
            if(Object.hasOwn(p,key))p[key]=reportNumber(p[key]);
        }
        if(command.type==="report.template_save" && (!String(p.name||"").trim()||!Array.isArray(p.columns)||!p.columns.length))fail("Нужны название шаблона и столбцы отчёта.");
        return p;
    }
    function settingsBase(settings){
        if(!settings?.editable)return null;
        return {loading_horizon:settings.loading_horizon||"",loading_block:settings.loading_block||"",
            rock_type_id:settings.rock_type_id?Number(settings.rock_type_id):null,
            destinations:(settings.destinations||[]).map(d=>({dump_point_id:Number(d.dump_point_id),transport_distance_km:String(d.transport_distance_km??"")}))};
    }
    function detail(s,kind,id){
        const direct=s.details?.[cardKey(kind,id)]||s.details?.[rowKey(kind,id)];
        if(direct)return direct;
        // The same excavator may have moved between complexes and the garage while
        // offline. Its verified equipment facts and editable settings follow its ID.
        if(!s.inventory?.excavators?.some(row=>row.id===id))return null;
        const other=kind==="complex"?"equipment":"complex";
        return s.details?.[cardKey(other,id)]||s.details?.[rowKey(other,id)]||null;
    }
    function ownRefs(s,key){return Array.isArray(s.refs?.[key])?s.refs[key]:[];}
    function defaultFields(form,type){
        if(!form)return {};
        const data={};
        for(const input of form.elements){
            const k=input.name;
            if(!k||(!fields[type].includes(k)&&!(type==="report.template_save"&&/^column_label_[a-z_]+$/.test(k))))continue;
            if(type==="report.trip_correct"&&["correction_reason","date","shift_type","trip_id"].includes(k))continue;
            if(type==="report.template_save"&&k==="template_id")continue;
            if(input.tagName==="SELECT"){
                const checked=[...input.options].filter(o=>o.defaultSelected);
                data[k]=input.multiple?checked.map(o=>o.value):checked[0]?.value??input.options[0]?.value??"";
            }else if(k==="columns"){
                data[k]||=[];if(input.defaultChecked)data[k].push(input.value);
            }else if(input.type==="checkbox"){data[k]=input.defaultChecked?input.value:"";}
            else data[k]=input.defaultValue??"";
        }
        return data;
    }
    function baseFor(s,type,p,form){
        if(type.startsWith("live."))return clone(s.live);
        if(type==="card.settings"){
            const base=settingsBase(detail(s,p.category,p.equipment_id)?.settings);
            if(!base)fail("Нет сохранённых исходных настроек этой машины.");
            return base;
        }
        if(type==="worker_shift.close"){
            const sh=ownRefs(s,"worker_shifts").find(row=>Number(row.id)===p.shift_id);
            if(!sh || !Array.isArray(sh.open_trip_ids))fail("Для этой смены не сохранены исходные рейсы. Обновите данные при связи.");
            return {employee_id:sh.employee_id,equipment_id:sh.equipment_id,open_trip_ids:[...sh.open_trip_ids]};
        }
        if(type==="downtime.close"){
            const key=Object.keys(s.details||{}).find(k=>Number(s.details[k]?.downtime?.event_id??s.details[k]?.downtime?.id)===p.downtime_id);
            const d=key&&s.details[key].downtime;
            if(!d?.active)fail("Этот простой уже завершён или его исходные данные недоступны.");
            return {equipment_id:pos(key.split(":").at(-1))||pos(key),started_at:d.started_at};
        }
        if(type.startsWith("report.")){
            const base=defaultFields(form,type), ref=type==="report.trip_correct"?"trip_id":"template_id";
            if(type==="report.trip_correct")for(const key of ["volume_m3","transport_distance_km"]){
                if(Object.hasOwn(base,key))base[key]=reportNumber(base[key]);
            }
            // A second edit must validate against this tablet's already saved first edit,
            // even when the HTML archive still contains the last server values.
            const prev=[...s.outbox].reverse().find(e=>e.type===type && String(e.payload[ref]||"")===String(p[ref]||""));
            if(prev && p[ref])for(const key of Object.keys(base))
                if(Object.hasOwn(prev.payload,key))base[key]=clone(prev.payload[key]);
            return base;
        }
        const key=type==="assignment.cancel"?"assignments":"trips";
        const obj=ownRefs(s,key).find(row=>Number(row.id)===(p.assignment_id||p.trip_id));
        return obj?.status?{expected_status:obj.status}:{};
    }
    function move(s,type,p){
        const next=clone(s.live), rows=next.excavators;
        const valid=(kind,id)=>s.inventory[kind].some(e=>Number(e.id)===id);
        const row=rows.find(r=>r.excavator_id===p.excavator_id);
        if(type==="live.move"){
            if(!valid("excavators",p.excavator_id)||!["inactive","active"].includes(p.zone))fail("Экскаватор недоступен.");
            if(p.zone==="active"){if(row)fail("Экскаватор уже на рабочем поле.");rows.push({excavator_id:p.excavator_id,truck_ids:[]});}
            else{if(!row)fail("Комплекс уже расформирован.");next.excavators=rows.filter(r=>r!==row);}
        }else if(p.action==="release_complex"){
            if(!row)fail("Комплекс уже расформирован.");row.truck_ids=[];
        }else{
            if(!["assign","release"].includes(p.action)||!valid("trucks",p.truck_id))fail("Самосвал недоступен.");
            if(p.action==="assign"&&!row)fail("Выбранный экскаватор не на рабочем поле.");
            rows.forEach(r=>{r.truck_ids=r.truck_ids.filter(id=>id!==p.truck_id);});
            if(p.action==="assign")row.truck_ids.push(p.truck_id);
        }
        s.live=next;
    }
    function updateCardProjection(s,e){
        const {type,payload:p}=e;
        for(const [key,card] of Object.entries(s.details||{})){
            if(!card)continue;
            if(type==="card.settings" && card.settings && (key===cardKey(p.category,p.equipment_id)||key===cardKey("complex",p.equipment_id)||key===cardKey("equipment",p.equipment_id)||key===rowKey(p.category,p.equipment_id))){
                const known=card.settings, points=new Map((known.dump_points||[]).map(x=>[Number(x.id),x.name]));
                if(!known.rock_types?.some(x=>Number(x.id)===p.rock_type_id)||p.destinations.some(x=>!points.has(x.dump_point_id)))fail("Порода или точка разгрузки недоступны в справочнике.");
                Object.assign(known,{loading_horizon:p.loading_horizon||"",loading_block:p.loading_block||"",rock_type_id:p.rock_type_id,
                    destinations:p.destinations.map(d=>({...d,name:points.get(d.dump_point_id)}))});
            }
            if(type==="downtime.close" && Number(card.downtime?.event_id??card.downtime?.id)===p.downtime_id){
                card.downtime={...card.downtime,active:false,closed:true,ended_at:e.occurred_at};
                card.status_label="Простой завершён на планшете";
            }
            if(type==="worker_shift.close" && Number(card.shift?.id)===p.shift_id){
                card.shift=null;if(card.manual_trip)card.manual_trip={...card.manual_trip,blocked_reason:"Смена работника завершена."};
            }
        }
    }
    function json(data,status=200){
        return new Response(JSON.stringify(data),{status,headers:{"Content-Type":"application/json; charset=utf-8","Cache-Control":"no-store","X-Field-Source":"device"}});
    }
    function actorScope(s){return s.actor?.key||"";}
    function maySeedMissingCard(s){
        return s.outbox.every(e=>e.actor_key===actorScope(s) && ["shift.open","draft.save","draft.apply","live.move","live.assign"].includes(e.type));
    }
    async function apply(cmd,raw,form){
        if(!L())fail("Локальный журнал не готов. Действие не отправлено.");
        writes++;
        try{
            const s=await L().read(), key=actorScope(s), p=payload(cmd,raw);
            if(doc.body?.dataset.observerMode==="true" || (pageActor&&pageActor!==key))fail("Сотрудник сменился. Откройте пульт под своим именем.");
            if(!pageActor && s.actor?.verified)pageActor=key;
            const base=baseFor(s,cmd.type,p,form);
            const touched=cmd.type.startsWith("live.") ? new Set(p.truck_id?[p.truck_id]:
                (base.excavators.find(r=>r.excavator_id===p.excavator_id)?.truck_ids||[])):new Set();
            const predecessors=s.outbox.filter(ev=> ev.actor_key===key && ev.local_shift_id===s.shift?.local_shift_id &&
                ev.type.startsWith("live.") && (touched.has(ev.payload.truck_id)||ev.payload.excavator_id===p.excavator_id||
                ev.base_payload?.excavators?.some(r=>r.excavator_id===ev.payload.excavator_id&&r.truck_ids.some(id=>touched.has(id)))));
            const written=await L().command(cmd.type,p,{actor_key:key,local_shift_id:s.shift?.local_shift_id,
                depends_on:predecessors.map(ev=>ev.id),base_payload:base,reduce(state,e){
                if(actorScope(state)!==key||e.actor_generation!==s.actor?.generation||e.local_shift_id!==s.shift?.local_shift_id)fail("Смена изменилась. Откройте действие заново.");
                if(cmd.type.startsWith("live.")){
                    if(!same(state.live,base))fail("Состав поля изменился. Повторите действие на актуальной карточке.");
                    move(state,cmd.type,p);
                }else if(cmd.type==="card.settings" && !same(base,settingsBase(detail(state,p.category,p.equipment_id)?.settings)))fail("Настройки уже изменены. Откройте карточку снова.");
                if(cmd.type==="downtime.close" && !Object.values(state.details||{}).some(card=>card?.downtime?.active&&Number(card.downtime.event_id??card.downtime.id)===p.downtime_id))
                    fail("Простой уже завершён.");
                if(cmd.type==="report.template_save" && !p.template_id && state.outbox.some(ev=>
                    ev.actor_key===key && ev.type===cmd.type && !ev.payload.template_id && ev.payload.name.trim()===p.name.trim()))
                    fail("Шаблон с этим названием уже сохранён на планшете.");
                if(state.outbox.some(ev=>
                    (ev.type===cmd.type && ((p.shift_id && ev.payload.shift_id===p.shift_id) ||
                    (p.downtime_id && ev.payload.downtime_id===p.downtime_id) || (p.assignment_id && ev.payload.assignment_id===p.assignment_id))) ||
                    (p.trip_id && ["trip.cancel","trip.complete"].includes(cmd.type) &&
                    ["trip.cancel","trip.complete"].includes(ev.type) && ev.payload.trip_id===p.trip_id)))fail("Действие уже ожидает подтверждения.");
                updateCardProjection(state,e);
            }});
            last=written;
            const result={ok:true,queued:true,server_confirmed:false,local_event_id:written.last_event_id};
            if(cmd.type==="card.settings")result.settings=detail(written,p.category,p.equipment_id)?.settings;
            notice(labels[cmd.type]+": сохранено на планшете, ожидает подтверждения.");
            schedulePaint();return result;
        }finally{writes--;}
    }
    function notice(message){
        let node=doc.querySelector("[data-cr-field-notice]");
        if(!node && doc.body){node=doc.createElement("div");node.dataset.crFieldNotice="";node.setAttribute("role","status");
            Object.assign(node.style,{position:"fixed",zIndex:2147483000,bottom:"30px",left:"50%",transform:"translateX(-50%)",
                padding:"8px 14px",borderRadius:"5px",background:"#20292f",color:"#fff",maxWidth:"90%",pointerEvents:"none"});
            doc.body.appendChild(node);}
        if(node){node.hidden=false;node.textContent=message;clearTimeout(node.timer);node.timer=setTimeout(()=>node.hidden=true,4000);}
    }
    function localDetail(state,cmd,url){
        if(!state.actor?.verified||state.locked||(pageActor&&pageActor!==actorScope(state)))return json({error:"authentication_required"},403);
        const {category,equipment_id}=cmd.ids,card=clone(detail(state,category,equipment_id));
        if(!card)return json({error:"card_not_cached",message:"Нет подтверждённого снимка этой карточки."},503);
        card.category=category;
        const row=state.live.excavators.find(x=>x.excavator_id===equipment_id);
        const truck=state.inventory.trucks.find(x=>x.id===equipment_id);
        if(category==="complex" && row){
            const numbers=new Map(state.inventory.trucks.map(m=>[m.id,m.number||m.label||String(m.id)]));
            card.shift_report={...(card.shift_report||{}),current_trucks:row.truck_ids.map(id=>numbers.get(id)).filter(Boolean)};
        }else if(truck){
            const assignment=state.live.excavators.find(x=>x.truck_ids.includes(truck.id));
            const target=assignment && state.inventory.excavators.find(x=>x.id===assignment.excavator_id);
            card.zone=target?"К-"+target.number+" / в составе":"Гараж самосвалов";
        }
        // The legacy dialog compares the version it requested with the displayed board.
        const version=Number(url.searchParams.get("state_version")??board()?.dataset.operationalStateVersion??state.server_version);
        return json({ok:true,contract:"dispatcher-equipment-detail-v1",card_key:rowKey(category,equipment_id),
            operational_state_version:version,source_server_version:state.server_version,source:"device",card});
    }
    async function readCard(cmd,url,input,opts){
        const state=await L().read(), actor=actorScope(state);
        if(state.locked || !state.actor?.verified || (pageActor&&actor!==pageActor))return json({error:"authentication_required"},403);
        const hasCard=!!detail(state,cmd.ids.category,cmd.ids.equipment_id);
        if(navigator.onLine===false || !maySeedMissingCard(state) || ((activeLocal()||state.outbox.length)&&hasCard))return localDetail(state,cmd,url);
        try{
            if((activeLocal()||state.outbox.length) && !await win.FieldIdentity?.confirmScope?.(state.actor))return localDetail(state,cmd,url);
            const net=await nativeFetch(input,opts);
            if([401,403,404,409].includes(net.status))return net;
            if(!net.ok) return localDetail(state,cmd,url);
            const data=await net.clone().json();
            if(data.contract!=="dispatcher-equipment-detail-v1" || data.card_key!==rowKey(cmd.ids.category,cmd.ids.equipment_id) || !data.card)return net;
            if((activeLocal()||state.outbox.length) && !await win.FieldIdentity?.confirmScope?.(state.actor))return localDetail(await L().read(),cmd,url);
            await L().cacheDetails(cardKey(cmd.ids.category,cmd.ids.equipment_id),data.card,Number(data.operational_state_version),actor);
            const now=await L().read();
            if(actorScope(now)!==actor||now.locked)return json({error:"authentication_required"},403);
            return now.outbox.length?localDetail(now,cmd,url):net;
        }catch(err){if(opts?.signal?.aborted)throw err;return localDetail(await L().read(),cmd,url);}
    }
    async function parseBody(input,opts){
        const request=new Request(input,opts);
        if(request.headers.get("content-type")?.includes("json"))return request.json();
        return encoded(await request.formData());
    }
    // Capture both the original JSON transport and direct settings/downtime fetch calls.
    win.fetch=function(input,opts){
        const url=new URL(typeof input==="string" || input instanceof URL?input:input.url,location.href);
        const method=String(opts?.method||input?.method||"GET").toUpperCase(), cmd=route(url);
        if(activeLocal() && doc.body?.dataset.appRoleCode==="mining_master" && method==="GET" &&
            url.origin===location.origin && url.pathname==="/realtime/state/" && url.searchParams.get("include_events")==="1" &&
            url.searchParams.get("role_app_code")==="mining_master")return win.FieldTransport.personalRealtime(input,opts);
        if(method==="GET" && cmd?.type==="card.settings" && L())return readCard(cmd,url,input,opts);
        if((!cmd || method!=="POST") && !["GET","HEAD","OPTIONS"].includes(method) && unhandledWorkplaceWrite(url))
            return Promise.resolve(json({ok:false,error:"Это действие не поддерживается полевым журналом. Данные не отправлены."},405));
        if(method!=="POST" || !cmd) return nativeFetch(input,opts);
        return parseBody(input,opts).then(raw=>apply(cmd,raw)).then(body=>json(body,202)).catch(err=>json({ok:false,error:err.message,code:"local_command_rejected"},409));
    };
    function formInfo(form){
        if(form.method?.toUpperCase()!=="POST")return null;
        return route(new URL(form.getAttribute("action")||location.href,location.href));
    }
    function unknownForm(form){return form.method?.toUpperCase()==="POST" && unhandledWorkplaceWrite(new URL(form.getAttribute("action")||location.href,location.href));}
    const formBusy=new WeakSet();
    async function commitForm(form,cmd){
        if(formBusy.has(form))return;
        formBusy.add(form);
        try{
            const raw=encoded(new FormData(form));
            await apply(cmd,raw,form);
            form.closest("[data-gd-equipment-detail]")?.querySelector("button[data-gd-detail-close]")?.click();
            paintForms(last);
        }catch(err){notice(err.message);}finally{formBusy.delete(form);}
    }
    const nativeSubmit=win.HTMLFormElement.prototype.submit;
    win.HTMLFormElement.prototype.submit=function(){
        const cmd=formInfo(this);
        if(cmd){void commitForm(this,cmd);return;}
        if(unknownForm(this)){notice("Действие не сохранено: обновите полевой журнал.");return;}
        return nativeSubmit.call(this);
    };
    // At window bubble the original per-form/pult confirmation handler ran first.
    win.addEventListener("submit",e=>{
        if(e.defaultPrevented)return;
        const cmd=formInfo(e.target);
        if(cmd){e.preventDefault();void commitForm(e.target,cmd);}
        else if(unknownForm(e.target)){e.preventDefault();notice("Действие не сохранено: обновите полевой журнал.");}
    });
    function fieldsForForm(cmd,form){
        const values=encoded(new FormData(form));
        if(cmd.type==="report.trip_correct")return String(values.trip_id||"");
        if(cmd.type==="report.template_save")return String(values.template_id||"");
        return String(Object.values(cmd.ids)[0]||"");
    }
    function paintForms(state){
        if(!state?.actor || state.locked)return;
        const own=state.outbox.filter(e=>e.actor_key===actorScope(state));
        for(const form of doc.querySelectorAll("form[method=post]")){
            const cmd=formInfo(form);if(!cmd)continue;
            const key=fieldsForForm(cmd,form);
            const event=[...own].reverse().find(e=>e.type===cmd.type && (cmd.type==="report.trip_correct"?String(e.payload.trip_id)===key:
                cmd.type==="report.template_save"?String(e.payload.template_id||"")===key:
                Object.keys(cmd.ids).every(k=>e.payload[k]===cmd.ids[k])));
            let info=form.querySelector("[data-cr-command-status]");
            if(!event){info?.remove();continue;}
            if(!info){info=doc.createElement("small");info.dataset.crCommandStatus="";info.setAttribute("role","status");form.appendChild(info);}
            info.textContent=event.status==="conflict"?"Требуется сверка":labels[event.type]+": сохранено на планшете";
            if(!event.type.startsWith("report."))continue;
            if(event.type==="report.trip_correct"){
                const summary=form.closest(".dispatcher-shift-source-row")?.querySelector("summary");
                const values=summary?.querySelectorAll("span");
                if(values?.length>=4){
                    values[2].textContent=(event.payload.volume_m3||"нет объёма")+" м³";
                    values[3].textContent=(event.payload.transport_distance_km||"нет плеча")+" км";
                    const mark=summary.querySelector("b");if(mark)mark.textContent=event.status==="conflict"?"Нужна сверка":"На планшете";
                }
            }
            if(form.contains(doc.activeElement))continue;
            for(const input of form.elements){
                if(!Object.hasOwn(event.payload,input.name))continue;
                const v=event.payload[input.name];
                if(input.type==="checkbox"){input.checked=Array.isArray(v)?v.includes(input.value):v===input.value;}
                else if(typeof v!=="object"){input.value=String(v??"");}
            }
        }
        if(location.pathname!==ROOT+"reports/templates/")return;
        const tbody=doc.querySelector("tbody");
        if(!tbody)return;
        const templates=own.filter(event=>event.type==="report.template_save"&&!event.payload.template_id);
        for(const row of tbody.querySelectorAll("[data-field-template-event]")){
            if(!templates.some(e=>e.id===row.dataset.fieldTemplateEvent))row.remove();
        }
        for(const event of templates){
            if([...tbody.querySelectorAll("[data-field-template-event]")].some(row=>row.dataset.fieldTemplateEvent===event.id))continue;
            const row=doc.createElement("tr");row.dataset.fieldTemplateEvent=event.id;
            for(const value of [event.payload.name,event.payload.columns.length,event.status==="conflict"?"Нужна сверка":"На планшете","Ожидает подтверждения"]){
                const cell=doc.createElement("td");cell.textContent=String(value);row.appendChild(cell);
            }
            tbody.appendChild(row);
        }
    }
    function unsafe(){return pointer || dragging || [...doc.querySelectorAll('[aria-modal=true],[data-gd-equipment-detail]')]
        .some(node=>!node.closest("[hidden]") && node.getClientRects().length>0 && getComputedStyle(node).visibility!=="hidden") ||
        ["INPUT","TEXTAREA","SELECT"].includes(doc.activeElement?.tagName);}
    let verifiedRefresh=false;
    function ioBusy(){return unsafe() || writes>0 || !!last?.outbox?.length || (activeLocal()&&!verifiedRefresh);}
    function updateProjection(state){
        if(!board() || !mutations || !state?.snapshot_ready || !state.actor?.verified || state.locked || unsafe())return;
        const desired=new Map(state.live.excavators.map(row=>[row.excavator_id,row.truck_ids]));
        const areas=()=>[...board().querySelectorAll('.dispatcher-complex-card[data-dispatcher-drop="complex"]')];
        for(const card of areas()){
            const id=pos(card.dataset.equipmentId);
            if(id&&!desired.has(id))mutations.moveComplexToExcavatorGarage(card);
        }
        for(const id of desired.keys()){
            if(areas().some(card=>pos(card.dataset.equipmentId)===id))continue;
            const tile=[...board().querySelectorAll('[data-dispatcher-drag="excavator"][data-equipment-id]')].find(n=>pos(n.dataset.equipmentId)===id);
            const slot=areas().find(n=>!pos(n.dataset.equipmentId));
            if(tile&&slot)mutations.activateComplexFromExcavatorTile(tile,slot);
        }
        const targetByTruck=new Map();
        for(const [id,trucks] of desired)for(const truck of trucks)targetByTruck.set(truck,id);
        for(const tile of [...board().querySelectorAll('[data-dispatcher-drag="truck"][data-equipment-id]')]){
            if(!tile.isConnected || tile.classList.contains("is-placeholder"))continue;
            const truck=pos(tile.dataset.equipmentId), current=pos(tile.closest(".dispatcher-complex-card")?.dataset.equipmentId), target=targetByTruck.get(truck)||0;
            if(current===target)continue;
            const card=target && areas().find(n=>pos(n.dataset.equipmentId)===target);
            if(card)mutations.moveTruckToComplex(tile,card);else if(!target)mutations.moveTruckToGarage(tile);
        }
        // A cached original board may be older than the signed snapshot. Restore the
        // server's real assignment IDs for the next drag, without inventing IDs for
        // moves that have only been recorded on this tablet.
        if(!state.outbox.length && state.server_version>=Number(board().dataset.operationalStateVersion||0)){
            for(const tile of board().querySelectorAll('[data-dispatcher-drag="truck"][data-equipment-id]')){
                const token=state.live_tokens?.trucks?.[tile.dataset.equipmentId];
                const target=pos(tile.closest(".dispatcher-complex-card")?.dataset.equipmentId)||null;
                if(token && token.target===target && Number.isSafeInteger(token.id) && token.id>=0)
                    tile.dataset.haulAssignmentStateId=String(token.id);
            }
        }
        // Restore locally edited heading/context and remove the closed downtime timer only for
        // this actor's pending commands. The server will supply its own authoritative rows.
        for(const ev of state.outbox){
            if(ev.type==="card.settings"){
                const id=ev.payload.equipment_id, card=areas().find(n=>pos(n.dataset.equipmentId)===id);
                const cfg=detail(state,"complex",id)?.settings||detail(state,"equipment",id)?.settings;
                if(card&&cfg){
                    const h=card.querySelector(".chip-horizon"),b=card.querySelector(".chip-block"),r=card.querySelector(".chip-rock");
                    if(h)h.textContent=cfg.loading_horizon||"Гор. -";if(b)b.textContent=cfg.loading_block||"Блок -";
                    const rock=cfg.rock_types?.find(x=>Number(x.id)===Number(cfg.rock_type_id));if(r&&rock)r.textContent=rock.name;
                }
            }
            if(ev.type==="downtime.close"&&ev.base_payload?.equipment_id){
                const tile=areas().find(n=>pos(n.dataset.equipmentId)===Number(ev.base_payload.equipment_id));
                tile?.querySelector(".complex-downtime-status")?.remove();
            }
        }
    }
    const detailAttempts=new Map();
    let hydrating=false;
    async function hydrateCards(){
        if(hydrating || !L() || navigator.onLine===false || doc.hidden || !board() || unsafe())return;
        hydrating=true;
        try{
            const state=await L().read();
            if(!state.snapshot_ready||state.locked||!state.actor?.verified||!maySeedMissingCard(state))return;
            const actor=actorScope(state), version=Number(board().dataset.operationalStateVersion)||0;
            const table=[...board().querySelectorAll("[data-dispatcher-drag][data-equipment-id]")];
            let checkedScope=false;
            for(const tile of table){
                if(doc.hidden||unsafe()||navigator.onLine===false || (last && !maySeedMissingCard(last)))break;
                const id=pos(tile.dataset.equipmentId), kind=tile.dataset.dispatcherDrag==="complex"?"complex":"equipment";
                const key=cardKey(kind,id),attempt=actor+":"+state.actor.generation+":"+key;
                if(!id||state.details[key]||state.details[rowKey(kind,id)]||Date.now()-(detailAttempts.get(attempt)||0)<60000)continue;
                detailAttempts.set(attempt,Date.now());
                if((activeLocal()||state.outbox.length)&&!checkedScope){
                    if(!await win.FieldIdentity?.confirmScope?.(state.actor))break;
                    checkedScope=true;
                }
                const url=ROOT+"dispatcher/control/card/"+kind+"/"+id+"/?state_version="+version;
                const res=await nativeFetch(url,{credentials:"same-origin",cache:"no-store",headers:{Accept:"application/json"},signal:AbortSignal.timeout(5500)});
                if(!res.ok)break;
                const dto=await res.json();
                if(dto.contract!=="dispatcher-equipment-detail-v1"||dto.card_key!==rowKey(kind,id)||Number(dto.operational_state_version)!==version||!dto.card)break;
                if((activeLocal()||state.outbox.length)&&!await win.FieldIdentity?.confirmScope?.(state.actor))break;
                await L().cacheDetails(key,dto.card,version,actor);
            }
        }catch(_){/* A weak connection must not block the original pult or any saved card. */}
        finally{hydrating=false;}
    }
    let paintTimer,readSequence=0,hadPending=false,fragmentRequest=null;
    async function canonicalFragment(state){
        const version=state.server_version,element=board();
        if(!element || !win.FieldIdentity || !win.applyOperationalStateRefresh || !win.FieldTransport?.status.connected ||
           win.FieldTransport.status.protocol!==win.FieldTransport.protocol || version<=Number(element.dataset.operationalStateVersion||0))return;
        const key=actorScope(state)+":"+version;
        if(fragmentRequest?.key===key && Date.now()-fragmentRequest.at<30000)return;
        fragmentRequest={key,at:Date.now()};
        try{
            // The device ticket can read the right snapshot even after the WebView
            // cookie changes. A legacy HTML fragment still uses the browser cookie.
            if(!await win.FieldIdentity.confirmScope(state.actor))return;
            const current=await L().read();
            if(actorScope(current)!==actorScope(state)||current.locked||current.outbox.length||unsafe())return;
            verifiedRefresh=true;
            await win.applyOperationalStateRefresh({version,events:[],eventsTruncated:true});
        }catch(_){/* The signed snapshot already supplied the board topology. */}
        finally{verifiedRefresh=false;}
    }
    function schedulePaint(){
        clearTimeout(paintTimer);paintTimer=setTimeout(async()=>{
            if(!L())return;
            const seq=++readSequence;
            try{
                const state=await L().read();if(seq!==readSequence)return;
                last=state;
                if(!pageActor && state.actor?.verified && !state.locked)pageActor=actorScope(state);
                if(!state.actor?.verified || state.locked || (pageActor && actorScope(state)!==pageActor))return;
                if(state.outbox.length)hadPending=true;
                if(unsafe())return;
                const boardVersion=Number(board()?.dataset.operationalStateVersion||0);
                const canonical=!state.outbox.length && !state.sync.needs_reconcile && state.server_version>=boardVersion;
                if(state.outbox.length || activeLocal() || (canonical && (hadPending || state.server_version>boardVersion)))updateProjection(state);
                if(canonical){
                    hadPending=false;
                    if(state.server_version>boardVersion)void canonicalFragment(state);
                }
                paintForms(state);
                if(maySeedMissingCard(state))void hydrateCards();
            }catch(err){notice(err.message);}
        },30);
    }
    function wrap(name,factory){
        const original=win[name];if(typeof original!=="function"||original.fieldWrapped)return;
        const adapter=function(...args){return factory(original.apply(this,args),args[0]||{});};
        adapter.fieldWrapped=true;win[name]=adapter;
    }
    function installFactories(){
        wrap("createDispatcherBoardMutations",api=>{mutations=api;return api;});
        wrap("createDispatcherTransport",api=>{
            const stock=api.getQueueState;
            const legacy=api.readQueue();
            if(legacy.length)notice("Есть неподтверждённые действия прежней версии. Сверьте их с рабочим пультом.");
            // A pre-upgrade request has no trustworthy actor, time or signature. Never
            // relabel it as something the currently logged-in employee just performed.
            // Keep that original queue in storage for review; normal new actions use IDB.
            api.scheduleFlush=()=>{};api.flush=()=>Promise.resolve();
            if(legacy.length){
                const update=api.updateRealtimeConnection;
                api.updateRealtimeConnection=(...args)=>{if(!api.readQueue().length)return update(...args);};
            }
            api.post=(target,data)=>{
                const cmd=route(new URL(target,location.href));
                if(!cmd)return Promise.reject(new Error("Действие отсутствует в полевом журнале."));
                return apply(cmd,data);
            };
            api.getQueueState=()=>{const q=stock();return {...q,length:0,pendingCount:(last?.outbox.length||0)+writes+(unsafe()||(activeLocal()&&!verifiedRefresh)?1:0)};};
            return api;
        });
        wrap("createDispatcherRealtime",api=>{
            const apply=api.applyOperationalStateRefresh, refresh=api.refreshBoardFromServer, blocked=api.isOperationalRefreshUnsafe;
            api.isOperationalRefreshUnsafe=(...args)=> ioBusy()||blocked(...args);
            api.applyOperationalStateRefresh=(...args)=> ioBusy()?Promise.resolve({deferred:true,reason:"field_pending"}):apply(...args);
            api.refreshBoardFromServer=(...args)=>ioBusy()?Promise.resolve(false):refresh(...args);
            return api;
        });
    }
    function install(){
        win.FieldTransport?.setBusy(unsafe);
        installFactories();schedulePaint();
        const shell=doc.querySelector(".dispatcher-shell");
        if(shell)new MutationObserver(changes=>{
            if(changes.some(c=>[...c.addedNodes].some(n=>n.nodeType===1&&n.matches(".dispatcher-board"))))schedulePaint();
        }).observe(shell,{childList:true,subtree:true});
        if(doc.body)new MutationObserver(records=>{
            if(records.some(rec=>rec.target.hasAttribute("hidden") && rec.target.querySelector('[aria-modal=true]'))){
                schedulePaint();if(!unsafe())win.FieldTransport?.idle()?.catch(()=>{});
            }
        }).observe(doc.body,{subtree:true,attributes:true,attributeFilter:["hidden"]});
    }
    win.addEventListener("pointerdown",()=>{pointer=true;},true);
    win.addEventListener("dragstart",()=>{dragging=true;},true);
    for(const event of ["pointerup","pointercancel","dragend","drop","focusout"]){
        win.addEventListener(event,()=>{pointer=false;dragging=false;schedulePaint();if(!unsafe())win.FieldTransport?.idle()?.catch(()=>{});},true);
    }
    win.addEventListener("fieldledgerchange",schedulePaint);
    win.addEventListener("fieldtransportchange",schedulePaint);
    win.addEventListener("pageshow",schedulePaint);
    win.FieldLiveActions={route,unsafe,refresh:schedulePaint,apply,installFactories};
    if(doc.readyState==="loading")win.addEventListener("DOMContentLoaded",install,{capture:true,once:true});
    else install();
})(window,document);
