/* Shared tablet draft. Preparing a layout never calls a production mutation. */
(function (global) {
    "use strict";

    const ROOT = "/mining-master/tablet/";
    const EXCHANGE = ROOT + "field-sync/";
    const TRUCK = ROOT + "dispatcher/control/truck/assign/";
    const MOVE = ROOT + "dispatcher/control/excavator/move/";
    const ROLE = "/realtime/state/?include_events=0&role_app_code=mining_master";
    const KEY = "tablet-draft";
    const unavailable = "Нет связи. Запрос активации сохранён на планшете.";
    let database, flushing, context, lastIdentity = "";
    const copy = value => value == null ? value : JSON.parse(JSON.stringify(value));
    const uid = () => crypto.randomUUID();
    const failure = (status,message) => Object.assign(new Error(message), {status});
    const own = doc => doc?.querySelector(".dispatcher-board")?.dataset.dispatcherOwnShiftOpen === "true" && doc.body.dataset.observerMode !== "true";
    const ownerStamp = doc => {
        if (!own(doc)) return "";
        const end = doc.querySelector('[data-confirm-variant="shift-end"]');
        if (!end?.dataset.confirmDate || !end.dataset.confirmTime || !end.dataset.confirmPerson) return "";
        return JSON.stringify([end.dataset.confirmDate, end.dataset.confirmTime, end.dataset.confirmPerson]);
    };
    function openDatabase() {
        if (!database) database = new Promise((resolve,reject) => {
            const req = indexedDB.open("copper-master-planning-copy-v1", 1);
            req.onupgradeneeded = () => req.result.createObjectStore("drafts");
            req.onerror = () => reject(req.error);
            req.onblocked = () => reject(new Error("Предыдущее окно удерживает черновик."));
            req.onsuccess = () => {
                req.result.onversionchange = () => { req.result.close(); database = null; };
                resolve(req.result);
            };
        }).catch(err => { database = null; throw err; });
        return database;
    }
    async function change(operation) {
        const db = await openDatabase();
        return new Promise((resolve,reject) => {
            const tx = db.transaction("drafts", operation ? "readwrite" : "readonly");
            const shelf = tx.objectStore("drafts"), req = shelf.get(KEY);
            let result, error;
            req.onsuccess = () => {
                try {
                    result = operation ? operation(req.result || null) : req.result || null;
                    if (operation && result) shelf.put(result,KEY);
                } catch (e) { error=e; tx.abort(); }
            };
            tx.oncomplete = () => resolve(copy(result));
            tx.onabort = tx.onerror = () => reject(error || new Error("Не удалось сохранить черновик на планшете."));
        });
    }
    function plan(input) {
        if (!Array.isArray(input?.excavators)) throw new Error("Нет снимка рабочей расстановки.");
        const xs = new Set(), ts = new Set();
        const rows = input.excavators.map(row => {
            const id = row.excavator_id;
            if (!Number.isSafeInteger(id) || id < 1 || xs.has(id) || !Array.isArray(row.truck_ids)) throw new Error("Некорректный комплекс.");
            xs.add(id);
            const trucks = row.truck_ids.map(truck => {
                if (!Number.isSafeInteger(truck) || truck < 1 || ts.has(truck)) throw new Error("Самосвал не может занимать два места.");
                ts.add(truck); return truck;
            }).sort((a,b) => a-b);
            return {excavator_id:id,truck_ids:trucks};
        });
        return {excavators:rows.sort((a,b)=>a.excavator_id-b.excavator_id)};
    }
    const equal = (a,b) => JSON.stringify(a) === JSON.stringify(b);
    function readBoard(doc) {
        const board = doc.querySelector(".dispatcher-board");
        if (!board) throw failure("auth","Нет личного рабочего пульта горного мастера.");
        const states = {}, trucks = new Set(), excavators = new Set(), rows=[];
        for (const tile of board.querySelectorAll('[data-equipment-id][data-dispatcher-drag]')) {
            const id = Number(tile.dataset.equipmentId);
            if (!Number.isSafeInteger(id) || id<=0) continue;
            if (tile.dataset.dispatcherDrag === "truck") {
                trucks.add(id);
                const raw = tile.dataset.haulAssignmentStateId;
                if (/^\d+$/.test(raw || "")) states[id] = Number(raw);
            } else if (["excavator","complex"].includes(tile.dataset.dispatcherDrag)) excavators.add(id);
        }
        for (const tile of board.querySelectorAll('[data-dispatcher-drop="complex"][data-equipment-id]')) {
            const id = Number(tile.dataset.equipmentId);
            if (excavators.has(id)) rows.push({excavator_id:id,truck_ids:Array.from(tile.querySelectorAll('[data-dispatcher-drag="truck"][data-equipment-id]'), n=>Number(n.dataset.equipmentId))});
        }
        return {live:plan({excavators:rows}),states,trucks:[...trucks],excavators:[...excavators]};
    }
    function contents(planValue) {
        const zones = new Set(), targets = new Map();
        for (const row of planValue.excavators) {
            zones.add(row.excavator_id);
            for (const truck of row.truck_ids) targets.set(truck,row.excavator_id);
        }
        return {zones,targets};
    }
    function changes(base, target) {
        const b = contents(base), t = contents(target);
        const zones = [...new Set([...b.zones,...t.zones])].filter(id=>b.zones.has(id)!==t.zones.has(id));
        const trucks = [...new Set([...b.targets.keys(),...t.targets.keys()])].filter(id=>b.targets.get(id)!==t.targets.get(id));
        return {b,t,zones,trucks};
    }
    function checkRevision(row, ctx) {
        if (!row || !Number.isSafeInteger(ctx?.revision) || row.revision !== ctx.revision)
            throw new Error("Черновик изменился. Откройте «Расстановку» заново.");
    }
    function validateInventory(p, snap) {
        const park = new Set(snap.excavators), haul = new Set(snap.trucks);
        if (p.excavators.some(row=>!park.has(row.excavator_id)||row.truck_ids.some(id=>!haul.has(id))))
            throw new Error("Часть техники отсутствует в парке планшета.");
    }
    async function load(live,ctx) {
        context = ctx;
        const doc = ctx?.document || global.document, snap=readBoard(doc), initial=plan(live);
        const row=await change(old=>{
            if (old) {
                // Once a receipt is confirmed, refresh assignment versions from a board that
                // actually shows that receipt. Do not replace a handover draft with stale HTML.
                if (old.activation?.status==="applied" && alreadyReached(old.activation,snap) && !old.dirty) {
                    if (!equal(old.payload,snap.live)) old.revision++;
                    old.base=snap.live; old.payload=snap.live; old.states=snap.states;
                }
                return old;
            }
            validateInventory(initial,snap);
            return {payload:initial,base:initial,states:snap.states,revision:1,dirty:false,activation:null,
                device:uid(),sequence:0,verified:null};
        });
        // First paint and all draft edits are local. Remember the server-issued login generation
        // for offline activation, but only after a real verification of this shift.
        if (ownerStamp(doc)) verifyForLater(doc).catch(()=>{});
        return row;
    }
    async function save(value,ctx) {
        const doc=ctx?.document || global.document;
        if (!own(doc)) throw new Error("Редактировать расстановку можно только в своей открытой смене.");
        const p=plan(value), snap=readBoard(doc);
        validateInventory(p,snap);
        return change(row=>{
            checkRevision(row,ctx);
            if (equal(row.payload,p)) return row;
            if (!row.dirty && row.activation?.status==="applied") {
                if (!alreadyReached(row.activation,snap))
                    throw new Error("Дождитесь обновления рабочего пульта после активации.");
                row.states=snap.states; row.base=snap.live;
            }
            row.revision++; row.payload=p; row.dirty=!equal(p,row.base);
            return row;
        });
    }
    async function request(path,options={}) {
        const controller=new AbortController(), timeout=setTimeout(()=>controller.abort(),6000);
        try {
            const transport=global.FieldTransport?.wireFetch||global.fetch.bind(global);
            const res=await transport(path,{cache:"no-store",credentials:"same-origin",redirect:"follow",...options,signal:controller.signal});
            if (res.status>=500) throw failure("queued",unavailable);
            return res;
        } catch(e) {
            if (e.status) throw e;
            throw failure("queued",unavailable);
        } finally { clearTimeout(timeout); }
    }
    async function role(doc) {
        const response=await request(ROLE), content=response.headers.get("Content-Type")||"";
        if(!response.ok || !content.includes("json"))throw failure("auth","Подтвердите личный вход в действующую смену.");
        const state=await response.json();
        if (!state.authenticated || !state.role_active || state.active_role_code!=="mining_master" ||
                state.session_role_code!=="mining_master" || state.role_app_code!=="mining_master" || !state.session_revision || !ownerStamp(doc))
            throw failure("auth","Активация доступна в своей открытой смене горного мастера.");
        return {generation:String(state.session_revision),stamp:ownerStamp(doc)};
    }
    async function verifyForLater(doc) {
        const stamp=ownerStamp(doc);
        if (!stamp || stamp===lastIdentity) return;
        const owner=await role(doc);
        if (ownerStamp(doc)!==owner.stamp) return;
        await change(row=>row && ({...row, verified:owner}));
        lastIdentity=stamp;
    }
    async function currentBoard() {
        const res=await request(ROOT+"?planning_read="+Date.now());
        if (!res.ok || new URL(res.url,global.location.href).pathname!==ROOT) throw failure("auth","Не удалось подтвердить личный пульт.");
        const doc=new DOMParser().parseFromString(await res.text(),"text/html");
        if (!ownerStamp(doc)) throw failure("auth","Для активации откройте свою смену.");
        return {doc,snap:readBoard(doc),owner:await role(doc)};
    }
    function checkBaseline(a, fresh) {
        const {b,t,zones,trucks}=changes(a.base,a.target), live=contents(fresh.live);
        for (const id of zones) {
            if (!fresh.excavators.includes(id)) throw failure("conflict","Экскаватор исключён из парка. Сверьте черновик с пультом.");
            if (!t.zones.has(id) && [...live.targets].some(([truck,source])=>source===id && !b.targets.has(truck)))
                throw failure("conflict","Состав расформируемого комплекса изменился. Сверьте его с пультом.");
        }
        for (const id of trucks) {
            const actual=live.targets.get(id), dest=t.targets.get(id);
            if (!fresh.trucks.includes(id) || (dest && !fresh.excavators.includes(dest))) throw failure("conflict","Техника исключена из парка.");
            if (actual===dest) continue;
            if (actual!==b.targets.get(id) || a.states[id]!==fresh.states[id] || !Number.isSafeInteger(fresh.states[id]))
                throw failure("conflict","Назначение самосвала изменилось на действующем пульте. Сверьте расстановку.");
        }
    }
    // The ordinary console has three indivisible commands. Never synthesize a batch by posting
    // them in sequence: any multi-command arrangement requires one transaction on the receiver.
    function ordinaryCommand(a,fresh) {
        const {b,t,zones,trucks}=changes(a.base,a.target);
        const live=contents(fresh.live), cmd=(path,data)=>({path,data:{...data,client_action_id:a.id}});
        if (!zones.length && trucks.length===1) {
            const id=trucks[0], dest=t.targets.get(id);
            if (dest && !live.zones.has(dest)) return null;
            if (!Number.isSafeInteger(fresh.states[id])) throw failure("conflict","Версия назначения самосвала недоступна.");
            return cmd(TRUCK,{action:dest?"assign":"release",truck_id:id,...(dest?{excavator_id:dest}:{}),expected_assignment_state_id:fresh.states[id]});
        }
        if (zones.length===1 && t.zones.has(zones[0]) && !trucks.length)
            return cmd(MOVE,{excavator_id:zones[0],expected_zone:"inactive",zone:"active"});
        const id = zones.length===1 && !t.zones.has(zones[0]) ? zones[0] : !zones.length && trucks.length ? b.targets.get(trucks[0]) : null;
        if (!id || trucks.some(truck=>b.targets.get(truck)!==id || t.targets.has(truck))) return null;
        if ([...t.targets.values()].includes(id)) return null;
        const now=[...live.targets].filter(([,target])=>target===id);
        if (now.some(([truck])=>b.targets.get(truck)!==id || !Number.isSafeInteger(fresh.states[truck])))
            throw failure("conflict","В комплексе появились другие самосвалы. Состав нужно сверить.");
        const expected_assignment_states=Object.fromEntries(now.map(([truck])=>[truck,fresh.states[truck]]));
        return zones.length ? cmd(MOVE,{excavator_id:id,expected_zone:"active",zone:"inactive",expected_assignment_states}) :
            cmd(TRUCK,{action:"release_complex",excavator_id:id,expected_assignment_states});
    }
    function alreadyReached(a,snap) {
        const {t,zones,trucks}=changes(a.base,a.target), live=contents(snap.live);
        return zones.every(id=>live.zones.has(id)===t.zones.has(id)) && trucks.every(id=>live.targets.get(id)===t.targets.get(id));
    }
    async function receiver(row,owner) {
        const res=await request(EXCHANGE+"?device_id="+encodeURIComponent(row.device));
        if (res.status===404 || res.status===405) throw failure("queued","Черновик сохранён. Сервер пока не принимает всю расстановку за одно действие.");
        if (!res.ok) throw failure("auth","Сервер не подтвердил права на активацию.");
        if (!(res.headers.get("Content-Type")||"").includes("json")) throw failure("queued","Черновик ожидает подключения атомарного обмена.");
        const cap=await res.json(), shift=cap.snapshot?.shift, actor=cap.snapshot?.actor;
        if (cap.protocol!=="master-field/1") throw failure("queued","Сервер пока не поддерживает атомарную активацию.");
        if (!cap.ticket || !actor?.verified || actor.role!=="mining_master" || actor.generation!==owner.generation ||
            !shift?.is_open || !shift.server_id || !shift.local_shift_id) throw failure("auth","Подтвердите личную смену заново.");
        return cap;
    }
    function eventFor(a,row,cap) {
        const {actor,shift}=cap.snapshot;
        return {id:a.id,sequence:a.sequence,device_id:row.device,format_version:1,occurred_at:a.time,
            type:"draft.apply",actor_id:actor.id,actor_key:actor.key,actor_access_id:actor.access_id,actor_generation:actor.generation,
            local_shift_id:shift.local_shift_id,server_shift_id:shift.server_id,base_payload:a.base,payload:a.target};
    }
    const csrf=doc=>doc.querySelector('input[name="csrfmiddlewaretoken"]')?.value || "";
    async function post(cmd,cap,doc) {
        const token=csrf(doc);
        if (!token) throw failure("auth","Не удалось подтвердить форму смены.");
        const body=cap ? {protocol:"master-field/1",ticket:cap.ticket,event:cmd.event} : cmd.data;
        const res=await request(cmd.path,{method:"POST",headers:{"Content-Type":"application/json","X-CSRFToken":token,"X-Requested-With":"XMLHttpRequest"},body:JSON.stringify(body)});
        const data=(res.headers.get("Content-Type")||"").includes("json") ? await res.json() : {};
        if (!res.ok || !(cap ? data.status==="applied" : data.ok===true)) {
            const status= res.status===403 || data.status==="auth" ? "auth" : res.status===409 || data.status==="conflict" ? "conflict" : "queued";
            throw failure(status,data.error || data.message || (status==="auth"?"Подтвердите личный вход мастера.":"Подтверждение активации не получено."));
        }
        if (cap ? data.id!==cmd.event.id || data.actor_key!==cmd.event.actor_key : data.client_action_id!==cmd.data.client_action_id)
            throw failure("queued","Ответ не подтвердил именно эту активацию.");
    }
    async function update(id,fn) {
        return change(row=>{
            if (row?.activation?.id!==id) throw failure("conflict","На планшете уже выбран другой черновик.");
            fn(row.activation,row); return row;
        });
    }
    function result(row) {
        const a=row.activation;
        return {status:a.status,message:a.message,completed:a.status==="applied"?1:0,total:1};
    }
    async function finish(id) {
        const row=await update(id,(a,row)=>{
            a.status="applied"; a.message="Расстановка подтверждена. Действующий пульт обновляется.";
            row.base=copy(a.target); row.states={}; row.dirty=!equal(row.base,row.payload);
        });
        global.dispatchEvent(new CustomEvent("copper-tablet-planning-applied"));
        return result(row);
    }
    async function drain(doc,manual) {
        const row=await change(), a=row?.activation;
        if (!a || a.status==="applied") return a?result(row):null;
        try {
            const fresh=await currentBoard();
            if (ownerStamp(doc)!==fresh.owner.stamp || a.stamp!==fresh.owner.stamp ||
                (a.owner && !equal(a.owner,fresh.owner)) || (!a.owner && !manual))
                throw failure("auth","План сохранён. Действующему мастеру нужно лично подтвердить активацию.");
            if (!a.owner) await update(a.id,p=>{p.owner=fresh.owner;});
            let cmd=a.command, cap=null;
            if (!cmd) {
                checkBaseline(a,fresh.snap);
                if (alreadyReached(a,fresh.snap)) return finish(a.id);
                cmd=ordinaryCommand(a,fresh.snap);
                if (!cmd) {
                    cap=await receiver(row,fresh.owner);
                    cmd={path:EXCHANGE,event:eventFor(a,row,cap)};
                }
                const selected=await update(a.id,p=>{p.command ||= cmd;});
                cmd=selected.activation.command;
            }
            if (cmd.path===EXCHANGE) {
                cap ||= await receiver(row,fresh.owner);
                if (cmd.event.actor_key!==cap.snapshot.actor.key || cmd.event.server_shift_id!==cap.snapshot.shift.server_id)
                    throw failure("auth","Смена, подтвердившая активацию, уже сменилась.");
            }
            // Persist immutable command before the POST. A dropped response replays this exact
            // id and payload, so neither server nor client can mistake it for a second action.
            await post(cmd,cap,fresh.doc);
            return finish(a.id);
        } catch(err) {
            const latest=await update(a.id,p=>{p.status=err.status || "queued";p.message=err.message || unavailable;});
            return result(latest);
        }
    }
    async function run(doc,manual) {
        if (!flushing) flushing=drain(doc,manual).finally(()=>{flushing=null;});
        return flushing;
    }
    async function activate(ctx) {
        const doc=ctx?.document || global.document;
        if (!ownerStamp(doc)) return {status:"auth",message:"Активация доступна только в своей открытой смене."};
        const stamp=ownerStamp(doc);
        await change(row=>{
            checkRevision(row,ctx);
            const old=row.activation;
            if (old && old.status!=="applied" && old.revision===row.revision && old.stamp===stamp &&
                    !(old.status==="auth" && !old.command)) return row;
            if (old?.command && !["applied","conflict"].includes(old.status))
                throw new Error("Предыдущая активация ожидает подтверждения. Её нельзя перезаписать.");
            row.sequence++;
            row.activation={id:uid(),sequence:row.sequence,revision:row.revision,stamp,owner:row.verified?.stamp===stamp?row.verified:null,
                base:copy(row.base),states:copy(row.states),target:copy(row.payload),time:new Date().toISOString(),
                status:"queued",message:"Запрос сохранён на планшете."};
            return row;
        });
        return run(doc,true);
    }
    async function resume(ctx) {
        const doc=ctx?.document || context?.document || global.document;
        if (!own(doc)) return null;
        const row=await change();
        if (!row?.activation?.owner || row.activation.status!=="queued") return null;
        return run(doc,false);
    }
    async function rebase(ctx) {
        const doc=ctx?.document || global.document, current=await currentBoard();
        if (ownerStamp(doc)!==current.owner.stamp) throw failure("auth","Откройте действующую смену.");
        return change(row=>{
            checkRevision(row,ctx);
            if (row.activation?.status!=="conflict") throw new Error("Черновик сейчас не требует сверки.");
            const {t,zones,trucks}=changes(row.base,row.payload);
            const snap=current.snap, fresh=contents(snap.live);
            for (const id of trucks) {
                if (!snap.trucks.includes(id)) continue;
                const dest=t.targets.get(id);
                if (dest && snap.excavators.includes(dest)) {
                    fresh.targets.set(id,dest); fresh.zones.add(dest);
                } else if (dest==null) fresh.targets.delete(id);
            }
            for (const id of zones) {
                if (t.zones.has(id) && snap.excavators.includes(id)) fresh.zones.add(id);
                // A newly assigned truck belongs to the live shift. Show it in the draft
                // for a separate decision rather than silently releasing it during rebase.
                else if (![...fresh.targets.values()].includes(id)) fresh.zones.delete(id);
            }
            row.payload=plan({excavators:[...fresh.zones].map(id=>({excavator_id:id,truck_ids:
                [...fresh.targets].filter(([,dest])=>dest===id).map(([truck])=>truck)}))});
            row.base=snap.live; row.states=snap.states; row.verified=current.owner;
            row.dirty=!equal(row.payload,row.base); row.revision++; row.activation=null;
            return row;
        });
    }
    const legacyAPI={load,save,activate,resume,rebase,current:()=>change()};
    function sharedRow(s,old){
        const pending=old?.activation && !["applied","migrated"].includes(old.activation.status);
        return {...s.draft,base:s.draft.sync_base||s.live,activation:pending?old.activation:s.last_activation||null};
    }
    async function sharedState(){
        const old=await change(),s=await global.FieldLedger.importDraft(old);
        return {old,s};
    }
    async function signedLoad(_live,ctx){
        context=ctx;
        const {old,s}=await sharedState();return sharedRow(s,old);
    }
    async function signedSave(p,ctx){
        const {old}=await sharedState();return sharedRow(await global.FieldLedger.saveDraft(p,ctx?.revision),old);
    }
    async function signedActivate(ctx){
        const {old,s}=await sharedState(),a=old?.activation;
        if(a && !["applied","migrated"].includes(a.status)){
            if(a.command){
                // The old server might already have committed this immutable command.
                // Recover its original receipt. Never substitute a new signed operation.
                return run(ctx?.document||global.document,true);
            }
            if(a.stamp!==ownerStamp(ctx?.document||global.document))
                return {status:"auth",message:"Предыдущую активацию должен подтвердить подготовивший её мастер."};
            if(!s.actor?.verified||s.locked||!s.shift.is_open)throw new Error("Сначала войдите в свою смену.");
            await update(a.id,current=>{current.status="migrating";current.owner=null;});
            await global.FieldLedger.applyDraft(ctx?.revision,a.id);
            await update(a.id,current=>{current.status="migrated";});
        }else{
            const last=s.last_activation;
            if(last && !["applied","conflict","auth"].includes(last.status) && equal(last.target,s.draft.payload)){
                global.FieldTransport?.wake();
                return {status:"queued",message:"Расстановка применена на планшете и ожидает подтверждения."};
            }
            if(last?.status==="conflict" || last?.status==="auth")return {status:last.status,message:last.message};
            await global.FieldLedger.applyDraft(ctx?.revision);
        }
        global.dispatchEvent(new Event("copper-tablet-planning-applied"));
        global.FieldTransport?.wake();
        return {status:"queued",message:"Расстановка применена на планшете. Ожидается подтверждение системы."};
    }
    async function signedResume(ctx){
        const {old}=await sharedState();
        if(old?.activation?.command && old.activation.status==="queued" && old.activation.owner)await resume(ctx);
        global.FieldTransport?.wake();
    }
    async function signedRebase(ctx){
        const {old,s}=await sharedState();
        if(old?.activation?.command && old.activation.status==="conflict"){
            const revised=await rebase(ctx);return sharedRow(await global.FieldLedger.saveDraft(revised.payload,ctx?.revision),null);
        }
        if(s.last_activation?.status!=="conflict")throw new Error("Нет подтверждённого конфликта для сверки.");
        await global.FieldLedger.resolveConflict(s.last_activation.id);
        global.FieldTransport?.wake();return signedLoad(null,ctx);
    }
    // Keep compatibility with older APK callers without a ledger. The new tablet never
    // dispatches a freshly-created unsigned operation through the old path.
    const api=()=>global.FieldLedger?{load:signedLoad,save:signedSave,activate:signedActivate,
        resume:signedResume,rebase:signedRebase,current:()=>signedLoad(null,context)}:legacyAPI;
    global.CopperTabletDraft=Object.fromEntries(Object.keys(legacyAPI).map(key=>[key,(...args)=>api()[key](...args)]));
    global.addEventListener("online",()=>global.CopperTabletDraft.resume().catch(()=>{}));
})(window);

